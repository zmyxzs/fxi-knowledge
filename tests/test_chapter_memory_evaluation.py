"""Chapter-memory golden-case evaluator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEXT = "杨凌走进山门。赵洵收他为徒。杨凌得到一柄灵剑。"


def _valid_output() -> dict:
    return {
        "chapter_index": 1,
        "scenes": [
            {
                "name": "入山",
                "causal_paragraph": "杨凌走进山门，想拜入青云山，却被守门弟子拦下盘问；他说明来意并出示信物，守门弟子确认后放行。",
                "participants": ["杨凌"],
                "evidence": "杨凌走进山门",
            },
            {
                "name": "拜师",
                "causal_paragraph": "赵洵见杨凌根骨尚可，决定收他为徒；杨凌跪下叩首，从此获得青云山弟子身份，也承担起师门任务。",
                "participants": ["杨凌", "赵洵"],
                "evidence": "赵洵收他为徒",
            },
            {
                "name": "得剑",
                "causal_paragraph": "赵洵赐给杨凌一柄灵剑，杨凌接过灵剑后获得第一件正式法器，但剑身有裂痕，后续需要修补。",
                "participants": ["杨凌", "赵洵"],
                "evidence": "杨凌得到一柄灵剑",
            },
        ],
        "cognition": [],
        "state_deltas": [],
        "unresolved": [],
        "snapshot": {},
    }


def _module():
    spec = importlib.util.spec_from_file_location(
        "evaluate_chapter_memory",
        ROOT / "scripts" / "evaluate_chapter_memory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_evaluate_cases_scores_expected_fields() -> None:
    module = _module()
    report = module.evaluate_cases([{
        "id": "one",
        "chapter_index": 1,
        "text": TEXT,
        "output": _valid_output(),
        "expected": {
            "scene_keywords": ["入山", "拜师"],
            "cognition_types": [],
            "state_categories": [],
            "thread_keywords": [],
        },
    }])
    assert report["expected_cases"] == 1
    assert report["scene_keyword_recall"] == 1.0
    assert report["results"][0]["expected"]["scene_hit"] == 2


def test_golden_fixture_loads() -> None:
    module = _module()
    cases = module._load_cases(
        ROOT / "tests" / "fixtures" / "chapter_memory_golden.jsonl",
    )
    assert len(cases) == 3
    assert all(case.get("expected") for case in cases)


def test_real_golden_fixture_loads() -> None:
    module = _module()
    path = ROOT / "tests" / "fixtures" / "chapter_memory_golden_real.jsonl"
    if not path.exists():
        pytest.skip("real chapter fixture is local-only")
    cases = module._load_cases(path)
    assert len(cases) == 5
    assert all(len(case["text"]) > 1_000 for case in cases)
    assert all(case.get("expected") for case in cases)


def test_evidence_normalization_separates_punctuation_from_missing() -> None:
    module = _module()
    output = _valid_output()
    output["scenes"][0]["evidence"] = "杨凌 走进 山门"
    report = module.evaluate_cases([{
        "chapter_index": 1, "text": TEXT, "output": output,
    }])
    assert report["evidence_normalized_only"] >= 1
    assert report["evidence_missing"] == 0


def test_blind_golden_fixture_loads() -> None:
    module = _module()
    path = ROOT / "tests" / "fixtures" / "chapter_memory_golden_blind.jsonl"
    if not path.exists():
        pytest.skip("blind chapter fixture is local-only")
    cases = module._load_cases(path)
    assert len(cases) == 10
    assert all(len(case["text"]) > 1_000 for case in cases)
    assert [case["chapter_index"] for case in cases] == list(range(6, 16))


def test_aggregate_runs_reports_mean_min_max() -> None:
    module = _module()
    runs = [
        {"valid_rate": 1.0, "state_category_recall": 0.8},
        {"valid_rate": 0.5, "state_category_recall": 0.6},
    ]
    report = module.aggregate_runs(runs)
    assert report["repeat"] == 2
    assert report["aggregate"]["valid_rate"] == {"mean": 0.75, "min": 0.5, "max": 1.0}
    assert report["aggregate"]["state_category_recall"]["mean"] == 0.7


def test_evaluate_cases_reports_validity_and_errors() -> None:
    module = _module()
    invalid = _valid_output()
    invalid["scenes"] = invalid["scenes"][:1]
    invalid["scenes"][0]["evidence"] = "正文没有这句话"
    report = module.evaluate_cases([
        {"chapter_index": 1, "text": TEXT, "output": _valid_output()},
        {"chapter_index": 1, "text": TEXT, "output": invalid},
    ])
    assert report["total"] == 2
    assert report["valid"] == 1
    assert report["valid_rate"] == 0.5
    assert report["scene_count_ok"] == 1
    assert report["evidence_errors"] >= 1
    assert report["issue_counts"]
