import json
import importlib
import sqlite3
from contextlib import contextmanager

import pytest

from fxi_novel.knowledge.parse_processing_units import (
    matching_saved_unit_attempts,
    processing_unit_failure_impact,
    processing_unit_fingerprint,
)
from fxi_novel.knowledge.graph_parser_identity_processing import (
    fail_identity_processing_unit,
    prepare_identity_processing_window,
)
from fxi_novel.knowledge.repository_parse_processing_units import (
    KnowledgeParseProcessingUnitRepositoryMixin,
)
from fxi_novel.knowledge.repository_parse_stage_audit import (
    KnowledgeParseStageAuditRepositoryMixin,
)
from fxi_novel.storage.migrations import apply_migration
from fxi_novel.storage.migrations_parse_processing_units import MIGRATIONS_PARSE_PROCESSING_UNITS


def _seed_processing_unit_connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE knowledge_parse_batch (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE knowledge_parse_batch_item (
            batch_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            source_id TEXT NOT NULL,
            source_title TEXT NOT NULL,
            folder_id TEXT,
            snapshot_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (batch_id, ordinal)
        );
    """)
    apply_migration(connection, MIGRATIONS_PARSE_PROCESSING_UNITS[0])
    connection.execute(
        "INSERT INTO knowledge_parse_batch(id, status, updated_at) VALUES ('batch', 'running', 't')"
    )
    connection.execute(
        """INSERT INTO knowledge_parse_batch_item(
               batch_id, ordinal, source_id, source_title, folder_id, snapshot_id,
               status, created_at, updated_at
           ) VALUES ('batch', 0, 'source', 'Chapter', NULL, 'snapshot', 'completed', 't', 't')"""
    )
    connection.execute(
        """INSERT INTO knowledge_parse_processing_unit(
               id, batch_id, phase, folder_id, group_ref, input_hash, prompt_hash,
               input_fingerprint, source_pins_json, candidate_refs_json,
               manifest_pin_json, execution_status, model_status, attempt_count,
               attempt_ids_json, outcome_json, created_at, updated_at
           ) VALUES (
               'unit', 'batch', 'identity_resolution', NULL, 'identity:window',
               ?, ?, ?, '[]', '[]', NULL, 'running', 'validated', 1,
               '[]', '{}', 't', 't'
           )""",
        ("a" * 64, "b" * 64, "c" * 64),
    )
    connection.commit()
    return connection


class _Database:
    def __init__(self, connection):
        self._connection = connection

    @contextmanager
    def transaction(self):
        try:
            yield self._connection
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    @contextmanager
    def connection(self):
        yield self._connection


class _Repository(KnowledgeParseProcessingUnitRepositoryMixin):
    def __init__(self, connection):
        self.database = _Database(connection)

    def _validate_stage_audit_manifest_pin(self, connection, batch, manifest_pin):
        assert manifest_pin is None

    def _validate_stage_audit_selected_pin(self, connection, batch, pin, *, require_check=False):
        assert pin["source_id"] == "source"
        assert pin["generation"] == 3
        assert require_check is True

    def _validate_stage_audit_target_ref(self, connection, batch, pins, ref):
        assert pins[ref["source_id"]]["snapshot_id"] == ref["snapshot_id"]

    def _validate_stage_audit_prior_ref(self, connection, batch, ref):
        assert ref["role"] == "prior_published"

    def _validate_stage_audit_prior_candidate_pin(self, connection, batch, ref):
        assert ref["role"] == "prior_published"
        self.prior_pins.append(ref)


def _processing_unit_request():
    return {
        "batch_id": "batch",
        "phase": "identity_resolution",
        "folder_id": None,
        "group_ref": "identity:source-window",
        "input_hash": "a" * 64,
        "prompt_hash": "b" * 64,
        "source_snapshot_pins": [{
            "source_id": "source", "snapshot_id": "snapshot",
            "receipt_id": "receipt", "generation": 3, "ordinal": 0,
        }],
        "candidate_refs": [{
            "role": "target", "item_id": "candidate", "version_id": "version-id",
            "version": 4, "source_id": "source", "snapshot_id": "snapshot",
        }],
        "manifest_pin": None,
    }


def test_processing_unit_fingerprint_changes_with_candidate_version_and_source_generation():
    common = {
        "phase": "identity_resolution",
        "folder_id": "folder",
        "group_ref": "identity:window",
        "input_hash": "a" * 64,
        "prompt_hash": "b" * 64,
        "source_snapshot_pins": [{
            "source_id": "source", "snapshot_id": "snapshot",
            "receipt_id": "receipt", "generation": 1,
        }],
        "candidate_refs": [{"item_id": "candidate", "version_id": "v1", "role": "target"}],
        "manifest_pin": None,
    }
    baseline = processing_unit_fingerprint(**common)
    changed_version = {
        **common,
        "candidate_refs": [{"item_id": "candidate", "version_id": "v2", "role": "target"}],
    }
    changed_generation = {
        **common,
        "source_snapshot_pins": [{
            "source_id": "source", "snapshot_id": "snapshot",
            "receipt_id": "receipt", "generation": 2,
        }],
    }
    assert processing_unit_fingerprint(**changed_version) != baseline
    assert processing_unit_fingerprint(**changed_generation) != baseline


def test_batch_terminal_transition_stales_inflight_processing_unit_and_blocks_reopen():
    connection = _seed_processing_unit_connection()
    connection.execute(
        "UPDATE knowledge_parse_batch SET status = 'failed', updated_at = 'later' WHERE id = 'batch'"
    )
    assert tuple(connection.execute(
        "SELECT execution_status, outcome_json FROM knowledge_parse_processing_unit WHERE id = 'unit'"
    ).fetchone()) == ("stale", '{"reason":"batch_terminal"}')
    with pytest.raises(sqlite3.IntegrityError, match="state transition"):
        connection.execute(
            "UPDATE knowledge_parse_processing_unit SET execution_status = 'running' WHERE id = 'unit'"
        )
    connection.close()


def test_processing_unit_scope_columns_are_immutable():
    connection = _seed_processing_unit_connection()
    with pytest.raises(sqlite3.IntegrityError, match="scope is immutable"):
        connection.execute(
            "UPDATE knowledge_parse_processing_unit SET candidate_refs_json = '[]' WHERE id = 'unit'"
        )
    connection.close()


def test_processing_scope_checks_published_prior_pin_without_request_evidence():
    connection = _seed_processing_unit_connection()
    repository = _Repository(connection)
    repository.prior_pins = []

    class _FolderConnection:
        def execute(self, _sql, _params):
            class _Result:
                def fetchone(self):
                    return {"folder_id": None}
            return _Result()

    pins = [{
        "source_id": "source", "snapshot_id": "snapshot",
        "receipt_id": "receipt", "generation": 3, "ordinal": 0,
    }]
    refs = [{
        "role": "target", "item_id": "current", "version_id": "current-v1",
        "source_id": "source", "snapshot_id": "snapshot", "evidence_refs": [],
    }, {
        "role": "prior_published", "item_id": "prior", "version_id": "prior-v7",
        "source_id": "prior-source", "snapshot_id": "prior-snapshot",
        "receipt_id": "prior-receipt", "generation": 7,
        "branch_id": None, "evidence_refs": [],
    }]
    repository._validate_parse_processing_scope_in_transaction(
        _FolderConnection(),
        batch={"id": "batch"},
        phase="identity_resolution",
        folder_id=None,
        source_snapshot_pins=pins,
        candidate_refs=refs,
        manifest_pin=None,
    )

    assert repository.prior_pins == [refs[1]]
    connection.close()


def test_model_attempt_prior_ref_still_requires_exact_evidence(monkeypatch):
    repository = KnowledgeParseStageAuditRepositoryMixin()
    checked_refs = []
    evidence_checks = []
    monkeypatch.setattr(
        repository,
        "_validate_stage_audit_prior_candidate_pin",
        lambda _connection, _batch, ref: checked_refs.append(ref),
    )
    monkeypatch.setattr(
        KnowledgeParseStageAuditRepositoryMixin,
        "_validate_stage_audit_evidence_refs",
        staticmethod(lambda _connection, _item, _version, refs, **_kwargs: evidence_checks.append(refs)),
    )
    ref = {
        "item_id": "prior", "version_id": "prior-v7", "evidence_refs": [],
    }
    with pytest.raises(ValueError, match="requires exact evidence refs"):
        repository._validate_stage_audit_prior_ref(None, {}, ref)
    ref["evidence_refs"] = [{"source_id": "source", "quote": "exact"}]
    repository._validate_stage_audit_prior_ref(None, {}, ref)

    assert checked_refs == [ref, ref]
    assert evidence_checks == [ref["evidence_refs"]]


def test_failure_projection_uses_fixed_unit_and_reports_saved_validated_response():
    impact = processing_unit_failure_impact(
        phase="identity_resolution",
        source_ids={"source-a", "source-b"},
        diagnostic="apply failed",
        units=[{
            "id": "unit-a",
            "execution_status": "failed",
            "model_status": "validated",
            "group_ref": "identity:window-a",
            "source_pins": [{"source_id": "source-a", "snapshot_id": "s1", "generation": 1}],
            "candidate_refs": [{"item_id": "candidate-a", "role": "target"}],
            "attempt_ids": ["attempt-a"],
            "outcome": {"diagnostic": "apply failed"},
        }, {
            "id": "unit-done",
            "execution_status": "completed",
            "model_status": "validated",
            "source_pins": [{"source_id": "source-b"}],
            "candidate_refs": [{"item_id": "candidate-b", "role": "target"}],
            "outcome": {"kind": "candidate_updates_applied"},
        }],
    )
    assert impact["failed_source_ids"] == ["source-a"]
    assert impact["blocked_source_ids"] == ["source-b"]
    assert impact["candidate_ids"] == ["candidate-a"]
    assert impact["preserved_model_results"] == ["unit-a:validated"]
    assert "无已保存模型响应" not in impact["diagnostic"]


def test_saved_attempt_is_joined_only_to_the_exact_window_pins_and_prompt_input():
    base_messages = [
        {"role": "system", "content": "identity rules"},
        {"role": "user", "content": "window input"},
    ]
    unit = {
        "group_ref": "identity:window",
        "source_pins": [{"source_id": "source", "snapshot_id": "snapshot", "generation": 2}],
        "candidate_refs": [{"item_id": "candidate", "version_id": "v4", "role": "target"}],
    }
    good = {
        "id": "attempt-good",
        "phase": "identity_resolution",
        "group_ref": "identity:window",
        "prompt_hash": "prompt",
        "source_pins": unit["source_pins"],
        "candidate_refs": [{
            **unit["candidate_refs"][0],
            "evidence_refs": [{"quote": "the exact evidence is in the saved attempt"}],
        }],
        "request": {"messages": [*base_messages, {"role": "user", "content": "repair note"}]},
        "response": json.dumps({"groups": []}),
        "created_at": "2026-10-10T01:00:00Z",
        "attempt": 2,
        "route_index": 0,
    }
    wrong_version = {**good, "id": "attempt-wrong", "candidate_refs": [{
        "item_id": "candidate", "version_id": "v3", "role": "target",
    }]}
    assert matching_saved_unit_attempts(
        unit=unit,
        attempts=[wrong_version, good],
        base_messages=base_messages,
        prompt_hash="prompt",
    ) == [good]


def test_repository_reuses_failed_exact_window_without_losing_saved_attempts():
    connection = _seed_processing_unit_connection()
    repository = _Repository(connection)
    first = repository.prepare_knowledge_parse_processing_unit(**_processing_unit_request())
    assert first["execution_status"] == "running"
    repository.update_knowledge_parse_processing_unit(
        first["id"], execution_status="running", model_status="response_saved",
        attempt_ids=["attempt-saved"], outcome={"kind": "response_received"},
    )
    failed = repository.update_knowledge_parse_processing_unit(
        first["id"], execution_status="failed", model_status="response_saved",
        outcome={"kind": "apply_failed", "diagnostic": "temporary write failure"},
    )
    assert failed["execution_status"] == "failed"
    assert failed["model_status"] == "response_saved"
    resumed = repository.prepare_knowledge_parse_processing_unit(**_processing_unit_request())
    assert resumed["id"] == first["id"]
    assert resumed["execution_status"] == "running"
    assert resumed["attempt_count"] == 2
    assert resumed["attempt_ids"] == ["attempt-saved"]
    completed = repository.update_knowledge_parse_processing_unit(
        first["id"], execution_status="completed", model_status="validated",
        attempt_ids=["attempt-replay"], outcome={"kind": "replayed_and_applied"},
    )
    assert completed["execution_status"] == "completed"
    assert completed["attempt_ids"] == ["attempt-saved", "attempt-replay"]
    completed_reuse = repository.prepare_knowledge_parse_processing_unit(**_processing_unit_request())
    assert completed_reuse["id"] == first["id"]
    assert completed_reuse["execution_status"] == "completed"
    assert completed_reuse["attempt_count"] == 2
    connection.close()


def test_repository_returns_superseded_split_scope_without_reopening_it():
    connection = _seed_processing_unit_connection()
    repository = _Repository(connection)
    first = repository.prepare_knowledge_parse_processing_unit(**_processing_unit_request())
    superseded = repository.update_knowledge_parse_processing_unit(
        first["id"], execution_status="superseded", model_status="response_saved",
        attempt_ids=["attempt-timeout"], outcome={
            "kind": "adaptive_split", "child_candidate_count": 4,
        },
    )
    assert superseded["execution_status"] == "superseded"
    resumed = repository.prepare_knowledge_parse_processing_unit(**_processing_unit_request())
    assert resumed["id"] == first["id"]
    assert resumed["execution_status"] == "superseded"
    assert resumed["outcome"]["kind"] == "adaptive_split"
    assert resumed["attempt_ids"] == ["attempt-timeout"]
    connection.close()


def test_identity_scope_is_saved_before_candidate_refresh_failure(monkeypatch):
    pin = {
        "source_id": "source", "snapshot_id": "snapshot",
        "receipt_id": "receipt", "generation": 3, "ordinal": 0,
    }
    row = {
        **pin, "item_id": "candidate", "version_id": "version-id", "version": 4,
        "content": {"entity_ref": "entity-ref"},
    }

    class Knowledge:
        def __init__(self):
            self.prepared = None
            self.updated = None

        def prepare_knowledge_parse_processing_unit(self, **request):
            self.prepared = request
            return {
                "id": "unit", "group_ref": request["group_ref"],
                "candidate_refs": request["candidate_refs"],
                "source_pins": request["source_snapshot_pins"],
                "execution_status": "running", "model_status": "not_started",
                "attempt_ids": [], "outcome": {},
            }

        def update_knowledge_parse_processing_unit(self, unit_id, **request):
            self.updated = {"id": unit_id, **request}
            return {
                **self.updated, "group_ref": self.prepared["group_ref"],
                "candidate_refs": self.prepared["candidate_refs"],
                "source_pins": self.prepared["source_snapshot_pins"],
            }

        def list_knowledge_parse_stage_attempts(self, *args, **kwargs):
            raise AssertionError("request preparation failure cannot claim a model attempt")

    knowledge = Knowledge()
    identity_processing = importlib.import_module(
        "fxi_novel.knowledge.graph_parser_identity_processing"
    )

    def fail_refresh(*args, **kwargs):
        assert knowledge.prepared is not None
        raise RuntimeError("candidate refresh interrupted")

    monkeypatch.setattr(identity_processing, "refresh_identity_audit_candidate_rows", fail_refresh)
    result = prepare_identity_processing_window(
        knowledge,
        parse_stage_audit_context={
            "batch_id": "batch", "source_snapshot_pins": [pin], "manifest_pin": None,
        },
        prompt="identity prompt", prompt_hash="b" * 64, folder_id=None,
        current_ids={"candidate"}, current_item_ids=["candidate"],
        window_entity_ids={"candidate"}, audit_candidate_ids={"candidate"},
        candidate_by_id={"candidate": row}, row_by_id={"candidate": row},
        prior_representatives={}, row_order=lambda _row: (0,), identity_item=lambda **_kwargs: {},
        current_group_states_by_item={}, facts_by_entity_ref={}, snapshot_hash_cache={},
        allowed_group_ids=None, repartition_targets={}, max_input_characters=1000,
        min_split_candidates=2,
    )

    assert result["action"] == "fail"
    assert result["progress"]["id"] == "unit"
    assert result["progress"]["execution_status"] == "failed"
    assert result["progress"]["model_status"] == "not_started"
    assert result["progress"]["attempt_ids"] == []
    assert knowledge.prepared["candidate_refs"][0]["item_id"] == "candidate"
    assert knowledge.prepared["candidate_refs"][0]["version_id"] == "version-id"
    assert knowledge.updated["outcome"]["error_type"] == "RuntimeError"


def test_identity_scope_expansion_failure_is_pinned_to_current_window():
    pin = {
        "source_id": "source", "snapshot_id": "snapshot",
        "receipt_id": "receipt", "generation": 3, "ordinal": 0,
    }
    row = {
        **pin, "item_id": "candidate", "version_id": "version-id", "version": 4,
        "content": {"entity_ref": "entity-ref"},
    }

    class Knowledge:
        def __init__(self):
            self.prepared = None
            self.updated = None

        def prepare_knowledge_parse_processing_unit(self, **request):
            self.prepared = request
            return {
                "id": "unit", "group_ref": request["group_ref"],
                "candidate_refs": request["candidate_refs"],
                "source_pins": request["source_snapshot_pins"],
                "execution_status": "running", "model_status": "not_started",
                "attempt_ids": [], "outcome": {},
            }

        def update_knowledge_parse_processing_unit(self, unit_id, **request):
            self.updated = {"id": unit_id, **request}
            return {
                **self.updated, "group_ref": self.prepared["group_ref"],
                "candidate_refs": self.prepared["candidate_refs"],
                "source_pins": self.prepared["source_snapshot_pins"],
            }

        def list_knowledge_parse_stage_attempts(self, *args, **kwargs):
            raise AssertionError("scope failure must be recorded before any model attempt")

    knowledge = Knowledge()
    result = prepare_identity_processing_window(
        knowledge,
        parse_stage_audit_context={
            "batch_id": "batch", "source_snapshot_pins": [pin], "manifest_pin": None,
        },
        prompt="identity prompt", prompt_hash="b" * 64, folder_id=None,
        current_ids={"candidate"}, current_item_ids=["candidate"],
        window_entity_ids={"candidate"}, audit_candidate_ids={"candidate", "missing-prior"},
        candidate_by_id={"candidate": row}, row_by_id={"candidate": row},
        prior_representatives={}, row_order=lambda _row: (0,), identity_item=lambda **_kwargs: {},
        current_group_states_by_item={}, facts_by_entity_ref={}, snapshot_hash_cache={},
        allowed_group_ids=None, repartition_targets={}, max_input_characters=1000,
        min_split_candidates=2,
    )

    assert result["action"] == "fail"
    assert result["progress"]["execution_status"] == "failed"
    assert result["progress"]["candidate_ids"] == ["candidate"]
    assert [ref["item_id"] for ref in knowledge.prepared["candidate_refs"]] == ["candidate"]
    assert knowledge.updated["model_status"] == "not_started"


def test_processing_scope_rejection_falls_back_to_current_window_attribution():
    pin = {
        "source_id": "source", "snapshot_id": "snapshot",
        "receipt_id": "receipt", "generation": 3, "ordinal": 0,
    }
    current = {
        **pin, "item_id": "current", "version_id": "current-v1", "version": 1,
        "content": {"entity_ref": "current-ref"},
    }
    prior = {
        "source_id": "prior-source", "snapshot_id": "prior-snapshot",
        "receipt_id": "prior-receipt", "generation": 7,
        "item_id": "prior", "version_id": "prior-v7", "version": 7,
        "branch_id": None, "content": {"entity_ref": "prior-ref"},
    }

    class Knowledge:
        def __init__(self):
            self.requests = []
            self.updated = None

        def prepare_knowledge_parse_processing_unit(self, **request):
            self.requests.append(request)
            if any(ref["role"] == "prior_published" for ref in request["candidate_refs"]):
                raise ValueError("prior support changed before processing-unit pin")
            return {
                "id": "unit", "group_ref": request["group_ref"],
                "candidate_refs": request["candidate_refs"],
                "source_pins": request["source_snapshot_pins"],
                "execution_status": "running", "model_status": "not_started",
                "attempt_ids": [], "outcome": {},
            }

        def update_knowledge_parse_processing_unit(self, unit_id, **request):
            self.updated = {"id": unit_id, **request}
            return {
                **self.updated, "group_ref": self.requests[-1]["group_ref"],
                "candidate_refs": self.requests[-1]["candidate_refs"],
                "source_pins": self.requests[-1]["source_snapshot_pins"],
            }

        def list_knowledge_parse_stage_attempts(self, *args, **kwargs):
            raise AssertionError("failed pre-request validation must not read model attempts")

    knowledge = Knowledge()
    result = prepare_identity_processing_window(
        knowledge,
        parse_stage_audit_context={
            "batch_id": "batch", "source_snapshot_pins": [pin], "manifest_pin": None,
        },
        prompt="identity prompt", prompt_hash="b" * 64, folder_id=None,
        current_ids={"current"}, current_item_ids=["current"],
        window_entity_ids={"current", "prior"}, audit_candidate_ids={"current", "prior"},
        candidate_by_id={"current": current, "prior": prior},
        row_by_id={"current": current, "prior": prior}, prior_representatives={},
        row_order=lambda _row: (0,), identity_item=lambda **_kwargs: {},
        current_group_states_by_item={}, facts_by_entity_ref={}, snapshot_hash_cache={},
        allowed_group_ids=None, repartition_targets={}, max_input_characters=1000,
        min_split_candidates=2,
    )

    assert result["action"] == "fail"
    assert result["progress"]["candidate_ids"] == ["current"]
    assert len(knowledge.requests) == 2
    assert [ref["item_id"] for ref in knowledge.requests[1]["candidate_refs"]] == ["current"]
    assert knowledge.updated["model_status"] == "not_started"


def test_failed_unit_keeps_validated_model_status_when_attempt_lookup_fails():
    class Knowledge:
        def __init__(self):
            self.update = None

        def list_knowledge_parse_stage_attempts(self, *args, **kwargs):
            raise RuntimeError("audit attempt store temporarily unavailable")

        def update_knowledge_parse_processing_unit(self, unit_id, **request):
            self.update = {"id": unit_id, **request}
            return {
                **self.update, "group_ref": "identity:window",
                "candidate_refs": [{"item_id": "candidate", "role": "target"}],
                "source_pins": [{"source_id": "source"}],
            }

    knowledge = Knowledge()
    progress = fail_identity_processing_unit(
        knowledge,
        unit={"id": "unit", "model_status": "validated"},
        batch_id="batch", group_ref="identity:window",
        candidate_refs=[{"item_id": "candidate", "role": "target"}],
        source_pins=[{"source_id": "source"}],
        base_messages=[{"role": "user", "content": "same input"}],
        prompt_hash="b" * 64, current_attempt_ids=["attempt"],
        error=RuntimeError("projection write failed"), diagnostic="projection write failed",
    )

    assert progress["model_status"] == "validated"
    assert progress["attempt_ids"] == ["attempt"]
    assert knowledge.update["model_status"] == "validated"
    assert knowledge.update["outcome"]["response_preserved"] is True
