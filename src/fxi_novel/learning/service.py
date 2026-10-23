"""Public learning service composed from focused workflow modules."""

from __future__ import annotations

from .service_support import *
from .service_study import LearningStudyServiceMixin
from .service_planning import LearningPlanningServiceMixin
from .planning_synthesis_groups import LearningPlanningGroupsServiceMixin
from .service_style import LearningStyleServiceMixin
from .service_method_review import LearningMethodReviewServiceMixin
from .service_scope import LearningScopeServiceMixin
from .service_context import LearningContextServiceMixin


class LearningService(
    LearningStudyServiceMixin,
    LearningPlanningServiceMixin,
    LearningPlanningGroupsServiceMixin,
    LearningStyleServiceMixin,
    LearningMethodReviewServiceMixin,
    LearningScopeServiceMixin,
    LearningContextServiceMixin,
):
    """Learning source analysis and synthesis owner."""

    async def rebuild_style_card(self, item_id, *, expected_version, instructions="",
                                 completed_extractions=None, on_progress=None):
        from .service_style_rebuild import rebuild_style_card
        return await rebuild_style_card(self, item_id, expected_version=expected_version,
            instructions=instructions, completed_extractions=completed_extractions, on_progress=on_progress)

    def __init__(
        self,
        knowledge: KnowledgeService,
        chat_model: ChatModel,
        prompt_directory: str | Path,
        *,
        query_planner=None,
        query_assessor=None,
    ) -> None:
        self.knowledge = knowledge
        self.chat_model = chat_model
        self.prompt_directory = Path(prompt_directory)
        self.query_planner = query_planner
        self.query_assessor = query_assessor
