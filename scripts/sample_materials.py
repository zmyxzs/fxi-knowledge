"""Sample a few chapters from every material book and report ingestion issues.

Usage:
    PYTHONPATH=src python scripts/sample_materials.py \
        --root "D:\\文本\\素材" --per-book 2 \
        --base-url ... --api-key ... --model ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_server import KnowledgeServer  # noqa: E402

from fxi_novel.knowledge import archive_extraction  # noqa: E402


def _discover(root: Path) -> list[tuple[str, list[Path]]]:
    books = []
    for folder in sorted(root.iterdir()):
        raw = folder / "raw"
        if not raw.is_dir():
            continue
        files = sorted(raw.glob("*.md"))
        if files:
            books.append((folder.name, files))
    return books


def _chapter_number(path: Path) -> int:
    match = re.search(r"(\d+)", path.stem)
    return int(match.group(1)) if match else 1


def _select(files: list[Path], per_book: int) -> list[Path]:
    if len(files) <= per_book:
        return files
    indexes = {0, len(files) // 2, len(files) - 1}
    chosen = sorted(indexes)[:per_book]
    return [files[index] for index in chosen]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--per-book", type=int, default=2)
    parser.add_argument("--base-url", default=os.environ.get("FXI_KB_LLM_BASE_URL"))
    parser.add_argument("--api-key", default=os.environ.get("FXI_KB_LLM_API_KEY"))
    parser.add_argument("--model", default=os.environ.get("FXI_KB_LLM_MODEL"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not (args.base_url and args.api_key and args.model):
        parser.error("base URL, API key and model are required")

    server = KnowledgeServer(Path(tempfile.mkdtemp()) / "sample.sqlite3")
    report = {
        "root": str(args.root),
        "model": args.model,
        "books": 0,
        "chapters": 0,
        "valid": 0,
        "hard_failures": 0,
        "projection_failures": 0,
        "warnings_total": 0,
        "warning_types": {},
        "results": [],
    }
    warning_types: Counter[str] = Counter()
    for book, files in _discover(args.root):
        report["books"] += 1
        server.create_work(book)
        work_id = server.resolve_work(book)["id"]
        for path in _select(files, max(1, args.per_book)):
            text = path.read_text(encoding="utf-8")
            chapter_number = _chapter_number(path)
            report["chapters"] += 1
            try:
                output = asyncio.run(archive_extraction.extract_chapter_memory_with_openai(
                    text, chapter_number,
                    base_url=args.base_url, api_key=args.api_key, model=args.model,
                ))
                result = archive_extraction.apply_chapter_memory_output(
                    server.db, work_id, chapter_number, output,
                    text=text, prompt_version="4", model=args.model,
                )
                valid = bool(result.get("valid"))
                warnings = result.get("warnings") or []
                projection_error = result.get("projection_error")
            except Exception as exc:  # noqa: BLE001 - per-sample report
                valid = False
                warnings = [f"{type(exc).__name__}: {exc}"]
                projection_error = "extract failed"
                result = {}
            report["valid"] += int(valid)
            report["hard_failures"] += int(not valid)
            report["projection_failures"] += int(bool(projection_error))
            report["warnings_total"] += len(warnings)
            for warning in warnings:
                warning_types[warning.split("：")[0].split(" ")[0]] += 1
            report["results"].append({
                "book": book,
                "file": path.name,
                "chapter": chapter_number,
                "valid": valid,
                "issues": result.get("issues") or [],
                "warnings": warnings[:8],
                "projection": result.get("projection"),
                "projection_error": projection_error,
            })
            print(f"{book} {path.name}: valid={valid} warnings={len(warnings)}")
    report["warning_types"] = dict(warning_types)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
