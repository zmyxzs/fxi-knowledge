"""Evaluate chapter-memory JSON against the archive schema.

Usage:
    python scripts/evaluate_chapter_memory.py --cases cases.jsonl
    python scripts/evaluate_chapter_memory.py --cases cases.jsonl --live \
        --base-url https://... --api-key ... --model ...

Each line:
    {"chapter_index": 1, "text": "正文", "output": {...}, "expected": {...}}
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import unicodedata
from pathlib import Path

from fxi_novel.knowledge.archive_extraction import (
    extract_chapter_memory_with_openai,
    validate_chapter_memory,
)


def _load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _score_expected(output: dict, expected: dict) -> dict:
    scenes = [item for item in (output.get("scenes") or []) if isinstance(item, dict)]
    scene_text = " ".join(
        f"{item.get('name') or ''} {item.get('causal_paragraph') or ''}"
        for item in scenes
    )
    keywords = [str(item) for item in expected.get("scene_keywords") or []]
    scene_hits = [keyword for keyword in keywords if keyword in scene_text]
    cognition_types = {
        str(item.get("type"))
        for item in (output.get("cognition") or [])
        if isinstance(item, dict)
    }
    expected_cognition = {
        str(item) for item in expected.get("cognition_types") or []
    }
    state_categories = {
        str(item.get("category"))
        for item in (output.get("state_deltas") or [])
        if isinstance(item, dict)
    }
    expected_states = {
        str(item) for item in expected.get("state_categories") or []
    }
    thread_text = " ".join(
        str(item.get("thread") or "")
        for item in (output.get("unresolved") or [])
        if isinstance(item, dict)
    )
    thread_keywords = [str(item) for item in expected.get("thread_keywords") or []]
    thread_hits = [keyword for keyword in thread_keywords if keyword in thread_text]
    return {
        "scene_hit": len(scene_hits),
        "scene_total": len(keywords),
        "cognition_hit": len(cognition_types & expected_cognition),
        "cognition_total": len(expected_cognition),
        "state_hit": len(state_categories & expected_states),
        "state_total": len(expected_states),
        "thread_hit": len(thread_hits),
        "thread_total": len(thread_keywords),
    }


def _normalize_text(value: str) -> str:
    return "".join(
        char for char in value
        if not (
            char.isspace()
            or unicodedata.category(char)[0] in {"P", "Z", "S", "C"}
        )
    )


def _evidence_audit(output: dict, text: str) -> dict:
    quotes = []
    for key in ("scenes", "cognition", "state_deltas", "unresolved"):
        for item in output.get(key) or []:
            if isinstance(item, dict) and item.get("evidence"):
                quotes.append(str(item["evidence"]))
    normalized_text = _normalize_text(text)
    exact = 0
    normalized_only = 0
    missing = 0
    for quote in quotes:
        if quote in text:
            exact += 1
        elif _normalize_text(quote) in normalized_text:
            normalized_only += 1
        else:
            missing += 1
    return {
        "quotes": len(quotes),
        "exact": exact,
        "normalized_only": normalized_only,
        "missing": missing,
    }


def evaluate_cases(cases: list[dict]) -> dict:
    total = len(cases)
    valid = 0
    scene_count_ok = 0
    evidence_errors = 0
    cognition_errors = 0
    state_errors = 0
    thread_errors = 0
    chapter_errors = 0
    issue_counts: dict[str, int] = {}
    evidence_normalized_only = 0
    evidence_missing = 0
    evidence_quotes = 0
    expected_totals = {
        "scene_hit": 0, "scene_total": 0,
        "cognition_hit": 0, "cognition_total": 0,
        "state_hit": 0, "state_total": 0,
        "thread_hit": 0, "thread_total": 0,
    }
    expected_cases = 0
    results = []
    for case in cases:
        output = case.get("output") or {}
        text = str(case.get("text") or "")
        chapter_index = int(case.get("chapter_index") or 0)
        issues = validate_chapter_memory(output, text, chapter_index)
        if not issues:
            valid += 1
        scenes = output.get("scenes") if isinstance(output.get("scenes"), list) else []
        if 3 <= len(scenes) <= 8:
            scene_count_ok += 1
        evidence = _evidence_audit(output, text)
        evidence_quotes += evidence["quotes"]
        evidence_normalized_only += evidence["normalized_only"]
        evidence_missing += evidence["missing"]
        for issue in issues:
            key = issue.split("：")[0].split(" ")[0]
            issue_counts[key] = issue_counts.get(key, 0) + 1
            if "evidence" in issue:
                evidence_errors += 1
            if issue.startswith("cognition"):
                cognition_errors += 1
            if issue.startswith("state_deltas"):
                state_errors += 1
            if issue.startswith("unresolved"):
                thread_errors += 1
            if issue.startswith("chapter_index"):
                chapter_errors += 1
        expected = case.get("expected") or {}
        expected_score = None
        if expected:
            expected_cases += 1
            expected_score = _score_expected(output, expected)
            for key, value in expected_score.items():
                expected_totals[key] += value
        results.append({
            "id": case.get("id"),
            "chapter_index": chapter_index,
            "valid": not issues,
            "scenes": len(scenes),
            "issues": issues,
            "cognition_types": sorted({
                str(item.get("type"))
                for item in (output.get("cognition") or [])
                if isinstance(item, dict)
            }),
            "state_categories": sorted({
                str(item.get("category"))
                for item in (output.get("state_deltas") or [])
                if isinstance(item, dict)
            }),
            "threads": [
                str(item.get("thread"))
                for item in (output.get("unresolved") or [])
                if isinstance(item, dict)
            ],
            "expected": expected_score,
        })
    def recall(hit: str, total_key: str) -> float:
        denominator = expected_totals[total_key]
        return round(expected_totals[hit] / denominator, 3) if denominator else 0.0

    return {
        "total": total,
        "valid": valid,
        "valid_rate": round(valid / total, 3) if total else 0.0,
        "scene_count_ok": scene_count_ok,
        "evidence_errors": evidence_errors,
        "evidence_quotes": evidence_quotes,
        "evidence_normalized_only": evidence_normalized_only,
        "evidence_missing": evidence_missing,
        "cognition_errors": cognition_errors,
        "state_errors": state_errors,
        "thread_errors": thread_errors,
        "chapter_errors": chapter_errors,
        "issue_counts": issue_counts,
        "expected_cases": expected_cases,
        "scene_keyword_recall": recall("scene_hit", "scene_total"),
        "cognition_type_recall": recall("cognition_hit", "cognition_total"),
        "state_category_recall": recall("state_hit", "state_total"),
        "thread_keyword_recall": recall("thread_hit", "thread_total"),
        "results": results,
    }


async def _live_output(
    case: dict, *, base_url: str, api_key: str, model: str,
) -> dict:
    return await extract_chapter_memory_with_openai(
        str(case.get("text") or ""),
        int(case.get("chapter_index") or 0),
        base_url=base_url,
        api_key=api_key,
        model=model,
    )


SUMMARY_KEYS = (
    "valid_rate",
    "evidence_errors",
    "evidence_normalized_only",
    "evidence_missing",
    "scene_keyword_recall",
    "cognition_type_recall",
    "state_category_recall",
    "thread_keyword_recall",
)


def aggregate_runs(runs: list[dict]) -> dict:
    summaries = [
        {key: run.get(key) for key in SUMMARY_KEYS}
        for run in runs
    ]
    aggregate = {}
    for key in SUMMARY_KEYS:
        values = [float(run.get(key) or 0) for run in runs]
        aggregate[key] = {
            "mean": round(sum(values) / len(values), 3),
            "min": min(values),
            "max": max(values),
        }
    return {
        "repeat": len(runs),
        "runs": summaries,
        "aggregate": aggregate,
        "results": runs[-1].get("results", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--base-url", default=os.environ.get("FXI_KB_LLM_BASE_URL"),
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("FXI_KB_LLM_API_KEY"),
    )
    parser.add_argument(
        "--model", default=os.environ.get("FXI_KB_LLM_MODEL"),
    )
    args = parser.parse_args()
    cases = _load_cases(args.cases)
    if args.live:
        if not (args.base_url and args.api_key and args.model):
            parser.error("--live requires base URL, API key and model")
        runs = []
        errors = []
        for _ in range(max(1, int(args.repeat))):
            run_cases = copy.deepcopy(cases)
            for case in run_cases:
                case.pop("output", None)
                case.pop("error", None)
                try:
                    case["output"] = asyncio.run(_live_output(
                        case,
                        base_url=args.base_url,
                        api_key=args.api_key,
                        model=args.model,
                    ))
                except Exception as exc:  # noqa: BLE001 - report per case
                    case["output"] = {}
                    case["error"] = f"{type(exc).__name__}: {exc}"
                    errors.append({
                        "id": case.get("id"),
                        "error": case["error"],
                    })
            runs.append(evaluate_cases(run_cases))
        report = aggregate_runs(runs) if len(runs) > 1 else runs[0]
        report["live"] = True
        report["errors"] = errors
    else:
        report = evaluate_cases(cases)
        report["live"] = False
        report["errors"] = []
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
