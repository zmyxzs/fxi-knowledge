"""Explicit historical selector fixtures; never enables a live product path."""
import copy
import json

from fxi_novel.knowledge.story_register import register_catalog
from fxi_novel.writing.chapter_planning import ChapterPlanningService, _draft_view


class ArchivedSelectorPlanner(ChapterPlanningService):
    """The first fake transport response represents an archived selection.

    Recovery tests still run the real input/binding validators. They no longer
    create an old selection by starting its retired prompt as a new task.
    """

    async def generate(self, **kwargs):
        if not getattr(self, "_fixture_started", False) and not kwargs.get("replay_requests"):
            task = {"request": kwargs["request"], "facts": copy.deepcopy(kwargs["facts"])}
            if kwargs.get("author_context"):
                task["author_context"] = copy.deepcopy(kwargs["author_context"])
            if kwargs.get("plan_context"):
                task["related_plan"] = copy.deepcopy(kwargs["plan_context"])
            if kwargs.get("draft"):
                task["editable_draft"] = _draft_view(kwargs["draft"])
            task["method_catalog"] = copy.deepcopy(kwargs["catalog"])
            if (kwargs.get("register") or {}).get("records"):
                task["register_catalog"] = register_catalog(kwargs["register"])
            kwargs["replay_requests"] = {"chapter_method_selection": [
                {"role": "system", "content": "Archived selector fixture; not current instructions."},
                {"role": "user", "content": json.dumps(task, ensure_ascii=False, separators=(",", ":"))},
            ]}
        self._fixture_started = True
        return await super().generate(**kwargs)
