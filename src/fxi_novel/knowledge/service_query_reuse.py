"""Knowledge facade for disposable query reuse and current source navigation."""

from .query_pagination import query_scope_hash


class KnowledgeQueryReuseServiceMixin:
    def get_query_reuse_revision(self):
        return self.repository.get_query_reuse_revision()

    def query_reuse_scope(self, scope, *, extra_scope_pins=()):
        pins = self.resolve_query_evidence_scopes(scope, extra_scope_pins=extra_scope_pins)
        return query_scope_hash({
            "scope": scope.model_dump(mode="json"),
            "pins": [pin.model_dump(mode="json") for pin in pins],
        }), pins

    def read_query_result_cache(self, key, kind):
        return self.repository.read_query_result_cache(key, kind)

    def save_query_result_cache(self, key, kind, revision, payload):
        return self.repository.save_query_result_cache(key, kind, revision, payload)

    def read_query_navigation(self, scope_hash, subjects):
        return self.repository.read_query_navigation(scope_hash, subjects)

    def save_query_navigation(self, entries, revision):
        return self.repository.save_query_navigation(entries, revision)
