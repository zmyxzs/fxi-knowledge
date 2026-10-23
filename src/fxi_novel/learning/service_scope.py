"""Learning service methods for scope."""

from __future__ import annotations

from .service_support import *


class LearningScopeServiceMixin:
    """Scope behavior for learning workflows."""

    def _validate_source_scope(
        self,
        sources: list[dict[str, Any]],
        *,
        space: str | None,
        work_id: str | None,
        folder_ids: list[str],
    ) -> tuple[str, str | None, list[str], dict[str, str]]:
        scopes = {(source["space"], source["work_id"]) for source in sources}
        if len(scopes) != 1:
            raise ValueError("sources must belong to the same project and knowledge space")
        scope_space, scope_work_id = next(iter(scopes))
        if space is not None:
            validate_scope(space, work_id)
            if (scope_space, scope_work_id) != (space, work_id):
                # Registered learning works can own existing library folders
                # whose physical work_id remains null. Resolve through the same
                # query owner; never rewrite source ownership to make it match.
                from fxi_novel.knowledge.models_base import KnowledgeQueryScope

                authorized = (self.knowledge.resolve_query_evidence_scopes(KnowledgeQueryScope(
                    space="learning", work_id=work_id, purpose="canon_fact",
                    source_ids=[str(source["id"]) for source in sources], include_linked_scopes=False,
                )) if space == "learning" and work_id is not None else [])
                allowed = {identity for pin in authorized
                           if pin.space == scope_space and pin.work_id == scope_work_id
                           for identity in pin.source_ids or []}
                if any(str(source["id"]) not in allowed for source in sources):
                    raise ValueError(
                        "selected sources do not belong to the requested project and knowledge space"
                    )
        folders = self.knowledge.list_folders(str(scope_space), scope_work_id)
        folder_paths = {str(folder["id"]): str(folder["path"]) for folder in folders}
        source_folder_paths = {
            str(source["id"]): folder_paths.get(str(source.get("folder_id")))
            for source in sources
        }
        if folder_ids:
            if set(folder_ids) - set(folder_paths):
                raise ValueError(
                    "folder scope must belong to the selected project and knowledge space"
                )
            allowed_paths = [folder_paths[folder_id] for folder_id in folder_ids]
            for source in sources:
                source_path = source_folder_paths[str(source["id"])]
                if source_path is None or not any(
                    source_path == allowed
                    or source_path.startswith(f"{allowed}/")
                    for allowed in allowed_paths
                ):
                    raise ValueError(
                        "all selected sources must be inside the selected folder scope"
                    )
            return str(scope_space), scope_work_id, list(folder_ids), folder_paths

        source_folder_ids = {source.get("folder_id") for source in sources}
        if scope_work_id is None and len(sources) > 1:
            if None in source_folder_ids or len(source_folder_ids) != 1:
                raise ValueError(
                    "select one folder scope before studying multiple unprojected sources"
                )
        inferred_folder_ids = (
            [str(folder_id) for folder_id in source_folder_ids if folder_id is not None]
            if scope_work_id is None and len(source_folder_ids) == 1
            else []
        )
        return str(scope_space), scope_work_id, inferred_folder_ids, folder_paths

