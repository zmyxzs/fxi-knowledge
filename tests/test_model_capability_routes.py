import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from fxi_novel.models.configuration import (
    interactive_gateway_config,
    creative_gateway_config,
    knowledge_parse_gateway_config,
    knowledge_query_gateway_config,
    prose_edit_gateway_config,
    prose_review_gateway_config,
)
from fxi_novel.models.gateway import GatewayConfig, ModelGateway, ModelRoute


def test_explicit_fallbacks_are_retained_without_replacing_selected_primary():
    gemini = ModelRoute(enabled=True, base_url="https://gemini.invalid", model="gemini-low", route_id="gemini", token_env="", timeout_seconds=90)
    high = ModelRoute(enabled=True, base_url=gemini.base_url, model="gemini-high", route_id="high", token_env="", timeout_seconds=90)
    deepseek = ModelRoute(enabled=True, base_url="https://deepseek.invalid", model="deepseek-flash", route_id="batch", token_env="", timeout_seconds=90)
    config = GatewayConfig(chat=gemini, embedding=gemini, chat_fallbacks=(high, deepseek), knowledge_query=gemini)
    for derive in (
        interactive_gateway_config, knowledge_query_gateway_config,
    ):
        effective = derive(config)
        assert effective.chat.model.startswith("gemini")
        assert [route.model for route in effective.chat_fallbacks] == [high.model, deepseek.model]
    parse = knowledge_parse_gateway_config(config)
    assert parse.chat.model == gemini.model
    assert parse.chat.max_output_tokens == 8_000
    assert [route.model for route in parse.chat_fallbacks] == [high.model, deepseek.model]
    assert prose_edit_gateway_config(config).chat.model == high.model
    assert prose_review_gateway_config(config).chat.model == deepseek.model


@pytest.mark.parametrize("derive", [interactive_gateway_config, creative_gateway_config,
                                  knowledge_query_gateway_config, knowledge_parse_gateway_config])
def test_selected_deepseek_is_not_silently_replaced_or_disabled(derive):
    batch = ModelRoute(enabled=True, base_url="https://deepseek.invalid", model="deepseek-flash", route_id="batch", token_env="", timeout_seconds=90)
    config = GatewayConfig(chat=batch, embedding=batch, knowledge_query=batch)
    assert derive(config).chat.enabled
    assert derive(config).chat.model == batch.model
    gemini = replace(batch, model="gemini", route_id="fallback")
    assert derive(replace(config, chat_fallbacks=(gemini,))).chat.model == batch.model


def test_disabled_deepseek_fallback_stays_disabled_and_explicit_query_is_honored():
    primary = ModelRoute(enabled=True, base_url="https://models.invalid", model="chat", route_id="chat",
                         token_env="", timeout_seconds=90)
    query = replace(primary, model="deepseek-flash", route_id="query")
    config = GatewayConfig(chat=primary, embedding=primary, knowledge_query=query,
                           chat_fallbacks=(replace(query, enabled=False),))
    assert interactive_gateway_config(config).chat_fallbacks == ()
    assert knowledge_query_gateway_config(config).chat.model == query.model


@pytest.mark.parametrize("derive", [interactive_gateway_config, creative_gateway_config])
def test_deepseek_primary_reaches_transport_before_gemini_fallback(derive):
    async def run():
        selected = ModelRoute(enabled=True, base_url="https://models.invalid/v1",
            model="global:deepseek-v4.1-flash", route_id="selected", token_env="", timeout_seconds=90,
            temperature=1.0, top_p=0.95, max_output_tokens=16000)
        fallback = replace(selected, model="gemini-high", route_id="fallback")
        calls = []

        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"model": selected.model,
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = ModelGateway(derive(GatewayConfig(chat=selected, embedding=selected,
                chat_fallbacks=(fallback,))), client=client)
            result = await gateway.chat_turn([{"role": "user", "content": "fixed task"}])
        assert result.text == "ok"
        assert [call["model"] for call in calls] == [selected.model]
        assert calls[0]["temperature"] == 1.0 and calls[0]["top_p"] == 0.95
        assert calls[0]["max_tokens"] == 16000

    asyncio.run(run())


def _knowledge_config(**fallback_changes):
    query = ModelRoute(enabled=True, base_url="https://knowledge.invalid/v1", model="gemini-low",
                       route_id="knowledge", token_env="", timeout_seconds=60)
    fallback = replace(query, route_id="chat-fallback", timeout_seconds=180, **fallback_changes)
    return GatewayConfig(chat=replace(query, model="gemini-high", route_id="chat"),
                         embedding=query, knowledge_query=query, chat_fallbacks=(fallback,))


@pytest.mark.parametrize("derive", [knowledge_query_gateway_config, knowledge_parse_gateway_config])
def test_inherited_same_request_waits_once_using_existing_longer_allowance(derive):
    original = _knowledge_config(base_url="https://knowledge.invalid/v1/")
    effective = derive(original)
    assert effective.chat.timeout_seconds == 180
    assert effective.chat.route_id == "knowledge"
    assert effective.chat_fallbacks == ()
    assert original.knowledge_query.timeout_seconds == 60
    assert len(original.chat_fallbacks) == 1
    assert interactive_gateway_config(original).chat_fallbacks == original.chat_fallbacks


@pytest.mark.parametrize("difference", [
    {"base_url": "https://other.invalid/v1"}, {"model": "gemini-high"},
    {"token_env": "OTHER_CREDENTIAL"}, {"api_key": "different-test-key"},
    {"reasoning_effort": "high"},
])
def test_different_request_or_credentials_remain_a_real_fallback(difference):
    effective = knowledge_query_gateway_config(_knowledge_config(**difference))
    assert effective.chat.timeout_seconds == 60
    assert len(effective.chat_fallbacks) == 1


def test_disabled_duplicate_does_not_extend_waiting_and_longer_primary_is_preserved():
    config = _knowledge_config(enabled=False)
    assert knowledge_query_gateway_config(config).chat.timeout_seconds == 60
    config = _knowledge_config()
    config = replace(config, knowledge_query=replace(config.knowledge_query, timeout_seconds=240))
    assert knowledge_parse_gateway_config(config).chat.timeout_seconds == 240


def test_slow_response_survives_shorter_duplicate_deadline_without_resubmission():
    async def run():
        original = _knowledge_config()
        # Scale the real 60/180 boundary down; exercise the actual gateway timer.
        original = replace(original,
                           knowledge_query=replace(original.knowledge_query, timeout_seconds=0.02),
                           chat_fallbacks=(replace(original.chat_fallbacks[0], timeout_seconds=1),))
        calls = []

        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0.06)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(knowledge_parse_gateway_config(original), client=client)
            response, parsed = await gateway.chat_validated(
                [{"role": "user", "content": "check saved material"}],
                validator=lambda result: json.loads(result.text), json_mode=True,
            )
        assert parsed == {"ok": True}
        assert response.usage["total_tokens"] == 2
        assert len(calls) == 1
        assert calls[0].extensions["timeout"]["read"] == 1

    asyncio.run(run())


def test_invalid_output_still_uses_distinct_fallback_after_duplicate_is_removed():
    async def run():
        original = _knowledge_config()
        independent = replace(original.chat_fallbacks[0], base_url="https://other.invalid/v1", route_id="independent")
        original = replace(original, chat_fallbacks=(*original.chat_fallbacks, independent))
        called_hosts = []

        def handler(request):
            called_hosts.append(request.url.host)
            content = "invalid JSON" if request.url.host == "knowledge.invalid" else '{"ok":true}'
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(knowledge_query_gateway_config(original), client=client)
            _, parsed = await gateway.chat_validated(
                [{"role": "user", "content": "check saved material"}],
                validator=lambda result: json.loads(result.text), json_mode=True,
            )
        assert parsed == {"ok": True}
        assert called_hosts == ["knowledge.invalid", "other.invalid"]

    asyncio.run(run())
