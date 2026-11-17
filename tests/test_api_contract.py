"""Contract checks for HTTP routes, OpenAPI, and the Python client."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "/api/work_types",
    "/api/work_profile",
    "/api/story_record",
    "/api/canonical",
    "/api/canonical/apply",
    "/api/canonical/duplicates",
    "/api/canonical/dedupe",
    "/api/reconcile",
    "/api/archive_changes",
    "/api/compile_archive",
    "/api/documents",
    "/api/documents/review",
    "/api/documents/reproject",
    "/api/documents/search",
    "/api/context",
}


def _server_module():
    spec = importlib.util.spec_from_file_location(
        "kb_server_contract", ROOT / "scripts" / "kb_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_embedded_openapi_covers_artifact_routes() -> None:
    module = _server_module()
    paths = set(module.OPENAPI_SPEC["paths"])
    assert REQUIRED <= paths


def test_docs_openapi_covers_artifact_routes() -> None:
    import yaml

    document = yaml.safe_load(
        (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    )
    assert REQUIRED <= set(document["paths"])


def test_client_exposes_artifact_methods() -> None:
    spec = importlib.util.spec_from_file_location(
        "kb_client_contract", ROOT / "examples" / "kb_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    client = module.KnowledgeClient
    for method in (
        "work_types", "work_profile", "set_work_profile", "story_records",
        "compile_archive", "documents", "save_document", "review_document",
        "reproject_document", "search_documents", "context", "audit_draft",
        "canonical", "apply_canonical", "reconcile", "archive_changes",
        "canonical_duplicates", "dedupe_canonical",
    ):
        assert hasattr(client, method), method
