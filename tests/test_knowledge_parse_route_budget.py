"""Source extraction has an explicit provider budget without changing writing routes."""

from fxi_novel.models.configuration import knowledge_parse_gateway_config, knowledge_query_gateway_config
from fxi_novel.models.gateway import GatewayConfig, ModelRoute


def route(model, *, enabled=True):
    return ModelRoute(enabled=enabled, base_url="https://provider.test/v1",
        model=model, token_env="", timeout_seconds=90, reasoning_effort="high")


def test_parse_uses_explicit_query_route_instead_of_legacy_batch_fallback():
    writer = route("writer")
    query = route("gemini-query")
    deepseek = route("deepseek-v4.1-flash")
    config = GatewayConfig(chat=writer, embedding=route("", enabled=False),
        knowledge_query=query, chat_fallbacks=(deepseek,))
    selected = knowledge_parse_gateway_config(config).chat
    assert selected.model == query.model
    assert selected.reasoning_effort == query.reasoning_effort
    assert selected.max_output_tokens == 8000
    assert config.chat is writer and writer.reasoning_effort == "high"
    assert deepseek.reasoning_effort == "high" and deepseek.max_output_tokens is None


def test_parse_without_legacy_batch_route_uses_configured_interactive_route():
    config = GatewayConfig(chat=route("writer"), embedding=route("", enabled=False))
    assert knowledge_parse_gateway_config(config).chat.enabled is True
    assert knowledge_parse_gateway_config(config).chat.model == "writer"


def test_query_output_budget_preserves_writer_configuration():
    writer = route("writer")
    fallback = route("query-fallback")
    config = GatewayConfig(chat=writer, embedding=route("", enabled=False), chat_fallbacks=(fallback,))
    selected = knowledge_query_gateway_config(config)
    assert selected.chat.max_output_tokens == 8000
    assert selected.chat_fallbacks[0].max_output_tokens == 8000
    assert writer.max_output_tokens is None and fallback.max_output_tokens is None
