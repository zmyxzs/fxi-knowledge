"""Keep the shared query batch's local SQL reads in one bounded connection scope."""

from contextlib import nullcontext
from functools import wraps


def reuse_query_read_connections(operation):
    @wraps(operation)
    async def run(self, *args, **kwargs):
        database = getattr(getattr(self, "repository", None), "database", None)
        read_session = getattr(database, "read_session", None)
        # In-memory databases keep their existing serialized connection contract;
        # minimal repository doubles have no database/session implementation.
        with read_session() if callable(read_session) else nullcontext():
            return await operation(self, *args, **kwargs)

    return run


def consistent_query_read(operation):
    """Keep a synchronous graph enumeration/projection in one current snapshot."""
    @wraps(operation)
    def run(self, *args, **kwargs):
        repository = getattr(self, "repository", None)
        database = getattr(repository, "database", None)
        read_snapshot = getattr(database, "read_snapshot", None)
        # Finish any lazy projection migration before beginning the read. Normal
        # published mutations already maintain their projections atomically.
        ensure_index = getattr(repository, "ensure_knowledge_graph_index", None)
        if callable(read_snapshot) and callable(ensure_index):
            ensure_index()
        with read_snapshot() if callable(read_snapshot) else nullcontext():
            return operation(self, *args, **kwargs)

    return run
