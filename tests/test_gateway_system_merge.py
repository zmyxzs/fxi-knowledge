"""Gateway system-prompt folding for providers that ignore the system role."""

from __future__ import annotations

import asyncio
import json

import httpx

from fxi_novel.models.gateway import GatewayConfig, ModelGateway, ModelRoute


def _config(*, merge: bool) -> GatewayConfig:
    route = ModelRoute(
        enabled=True,
        base_url="https://provider.invalid/v1",
        model="test-model",
        token_env="",
        timeout_seconds=10,
        merge_system_prompt=merge,
    )
    return GatewayConfig(chat=route, embedding=route)


def test_system_prompt_is_folded_into_user_message_when_configured() -> None:
    async def run() -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen["roles"] = [message["role"] for message in payload["messages"]]
            seen["content"] = payload["messages"][0]["content"]
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(_config(merge=True), client=client)
            result = await gateway.chat(
                [
                    {"role": "system", "content": "SYSTEM RULES"},
                    {"role": "user", "content": "USER PAYLOAD"},
                ],
                json_mode=True,
            )
        assert result.text == '{"ok":true}'
        assert seen["roles"] == ["user"]
        assert "SYSTEM RULES" in seen["content"]
        assert "USER PAYLOAD" in seen["content"]

    asyncio.run(run())


def test_system_prompt_is_kept_when_not_configured() -> None:
    async def run() -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen["roles"] = [message["role"] for message in payload["messages"]]
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(_config(merge=False), client=client)
            await gateway.chat([
                {"role": "system", "content": "SYSTEM RULES"},
                {"role": "user", "content": "USER PAYLOAD"},
            ])
        assert seen["roles"] == ["system", "user"]

    asyncio.run(run())
