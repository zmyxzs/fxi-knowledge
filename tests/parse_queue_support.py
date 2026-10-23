"""Persist pre-window-release plans solely to exercise historical recovery."""

from contextlib import contextmanager
from unittest.mock import patch
from fxi_novel.knowledge.parse_submissions import KnowledgeParseSubmissionService


@contextmanager
def legacy_submission_planner():
    """Accept a historical plan before testing current replay/recovery."""
    original = KnowledgeParseSubmissionService._build_child_plan

    def build(*args, **kwargs):
        rows = original(*args, **kwargs)
        previous = {}
        for plan in rows:
            queue = plan["queue"]
            window = int(queue.get("chapter_concurrency") or 1)
            lane = previous.setdefault(queue["work_key"], [])
            queue["predecessor_client_batch_id"] = lane[-window] if len(lane) >= window else None
            if window == 1:
                queue["policy"] = "chapter_ordered"
                queue.pop("chapter_concurrency", None)
                queue.pop("submission_active_limit", None)
            lane.append(plan["client_batch_id"])
        return rows

    with patch.object(KnowledgeParseSubmissionService, "_build_child_plan", staticmethod(build)):
        yield
