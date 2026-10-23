"""Lossless OpenAI-compatible tool-call and streaming protocol coverage."""

from __future__ import annotations

import json
import unittest

import httpx

from fxi_novel.models.gateway import GatewayConfig, ModelGateway, ModelRoute


def _route(model: str, host: str, *, enabled: bool = True) -> ModelRoute:
    return ModelRoute(
        enabled=enabled,
        base_url=f"http://{host}/v1",
        model=model,
        token_env="",
        timeout_seconds=2,
    )


class ModelToolProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, client: httpx.AsyncClient) -> ModelGateway:
        disabled = _route("", "disabled", enabled=False)
        return ModelGateway(
            GatewayConfig(
                chat=_route("primary.test", "primary.test"),
                embedding=disabled,
                chat_fallbacks=(_route("fallback.test", "fallback.test"),),
            ),
            client=client,
        )

    async def test_nonstream_tool_only_message_keeps_extensions_and_raw_usage(self) -> None:
        expected_usage = {
            "prompt_tokens": 23,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 2, "cached_tokens": 8},
            "vendor_metric": "opaque",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read())
            self.assertEqual(payload["tools"][0]["function"]["name"], "lookup")
            return httpx.Response(
                200,
                json={
                    "id": "resp-1",
                    "model": "provider-alias",
                    "provider_state": {"continuation": "state"},
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "native_choice_state": "opaque-choice",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                                    }
                                ],
                                "thought_signature": "opaque-message-state",
                            },
                        }
                    ],
                    "usage": expected_usage,
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await self._gateway(client).chat_turn(
            [{"role": "user", "content": "find x"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )
        await client.aclose()

        self.assertEqual(result.text, "")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.response_id, "resp-1")
        self.assertEqual(result.provider_model, "provider-alias")
        self.assertEqual(result.tool_calls[0]["id"], "call-1")
        self.assertEqual(result.message["thought_signature"], "opaque-message-state")
        self.assertEqual(result.raw_usage, expected_usage)
        self.assertEqual(result.response_extensions["provider_state"], {"continuation": "state"})
        self.assertEqual(result.choice_extensions["native_choice_state"], "opaque-choice")

    async def test_stream_assembles_fragmented_tool_call_and_accepts_stop_reason(self) -> None:
        chunks = [
            {"id": "resp-2", "model": "primary.test", "choices": [{"delta": {"role": "assistant", "reasoning_content": "think "}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-", "type": "function", "function": {"name": "look", "arguments": '{"q":'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "1", "function": {"name": "up", "arguments": '"x"}'}}]}, "finish_reason": "stop"}]},
            {"usage": {"prompt_tokens": 10, "completion_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 3}}, "choices": []},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        deltas: list[str] = []
        result = await self._gateway(client).chat_turn(
            [{"role": "user", "content": "find x"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
            on_delta=deltas.append,
        )
        await client.aclose()

        self.assertEqual(deltas, [])
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.message["reasoning_content"], "think ")
        call = result.tool_calls[0]
        self.assertEqual(call["id"], "call-1")
        self.assertEqual(call["function"]["name"], "lookup")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"q": "x"})
        self.assertEqual(result.raw_usage["completion_tokens_details"], {"reasoning_tokens": 3})

    async def test_stream_preserves_content_that_coexists_with_tool_call(self) -> None:
        chunks = [
            {"choices": [{"delta": {"content": "Checking.", "tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
            )
        )
        visible: list[str] = []
        result = await self._gateway(client).chat_turn(
            [{"role": "user", "content": "check"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
            on_delta=visible.append,
        )
        await client.aclose()
        self.assertEqual(result.text, "Checking.")
        self.assertEqual(result.tool_calls[0]["id"], "c1")
        self.assertEqual(visible, ["Checking."])

    async def test_empty_response_is_rejected(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"model": "primary.test", "choices": [{"message": {"role": "assistant", "content": " "}}]},
                )
            )
        )
        with self.assertRaisesRegex(ValueError, "neither text nor tool calls"):
            await self._gateway(client).chat_turn([{"role": "user", "content": "hello"}])
        await client.aclose()

    async def test_chat_turn_does_not_silently_use_fallback(self) -> None:
        models: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            models.append(json.loads(request.read())["model"])
            return httpx.Response(
                200,
                json={"model": "primary.test", "choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await self._gateway(client).chat_turn([{"role": "user", "content": "hello"}])
        await client.aclose()
        self.assertEqual(result.text, "ok")
        self.assertEqual(models, ["primary.test"])

    async def test_explicit_stateless_fallback_uses_configured_route_after_primary_http_error(self) -> None:
        models: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.read())["model"]
            models.append(model)
            if model == "primary.test":
                return httpx.Response(403)
            return httpx.Response(200, json={"model": model,
                "choices": [{"message": {"role": "assistant", "content": '{"task":"ok"}'}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = self._gateway(client)
        result = await gateway.chat_turn([{"role": "user", "content": "resolve"}],
            tool_choice="none", json_mode=True, allow_stateless_fallback=True)
        self.assertEqual(result.provider_model, "fallback.test")
        self.assertEqual(models, ["primary.test", "fallback.test"])
        models.clear()
        with self.assertRaises(httpx.HTTPStatusError):
            await gateway.chat_turn([{"role": "user", "content": "use tool"}],
                tools=[{"type": "function", "function": {"name": "lookup"}}], tool_choice="auto")
        self.assertEqual(models, ["primary.test"])
        await client.aclose()

    async def test_tool_continuation_falls_back_only_on_same_endpoint_when_enabled(self) -> None:
        models: list[str] = []
        transcript = [
            {"role": "user", "content": "find x"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "name": "lookup", "content": "found x"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read())
            self.assertEqual(payload["messages"], transcript)
            models.append(payload["model"])
            if payload["model"] == "primary.test":
                return httpx.Response(403)
            return httpx.Response(200, json={"model": payload["model"], "choices": [{
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call-2", "type": "function",
                    "function": {"name": "finish", "arguments": '{"answer":"x"}'},
                }]},
            }]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = ModelGateway(GatewayConfig(
            chat=_route("primary.test", "same.test"),
            embedding=_route("", "disabled", enabled=False),
            chat_fallbacks=(
                _route("fallback.test", "same.test"),
                _route("other.test", "other.test"),
            ),
        ), client=client)
        result = await gateway.chat_turn(transcript, tools=[{"type": "function", "function": {"name": "finish"}}],
            tool_choice="required", allow_same_endpoint_fallback=True)
        self.assertEqual(result.provider_model, "fallback.test")
        self.assertEqual(result.tool_calls[0]["id"], "call-2")
        self.assertEqual(models, ["primary.test", "fallback.test"])
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
