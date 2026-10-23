"""Legacy text calls retain provider usage without changing numeric accounting."""

from __future__ import annotations

import json
import unittest

import httpx

from fxi_novel.models.call_budget import provider_call_budget
from fxi_novel.models.gateway import ChatResult, GatewayConfig, ModelGateway, ModelRoute
from fxi_novel.models.gateway_results import read_chat_response
from fxi_novel.models.validated_audit import validated_model_audit


class LegacyModelUsageTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, *, fallback: bool = False) -> GatewayConfig:
        route = ModelRoute(True, "https://primary.invalid/v1", "primary", "", 2)
        return GatewayConfig(
            chat=route,
            embedding=ModelRoute(False, "", "", "", 2),
            chat_fallbacks=(ModelRoute(True, "https://fallback.invalid/v1", "fallback", "", 2),)
            if fallback else (),
        )

    def _usage(self, reasoning_tokens: int = 388) -> dict:
        return {
            "prompt_tokens": 3610,
            "completion_tokens": 382,
            "total_tokens": 3992 + reasoning_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "provider_metric": {"label": "opaque", "values": [1, "unknown"]},
        }

    async def test_chat_preserves_raw_usage_in_result_and_provider_receipt(self) -> None:
        usage = self._usage()

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(json.loads(request.read()), {
                "model": "primary", "messages": [{"role": "user", "content": "fixture"}],
            })
            return httpx.Response(200, json={
                "model": "provider-alias", "choices": [{"message": {"content": "saved text"}}],
                "usage": usage,
            })

        receipts = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(self._config(), client=client)
            with provider_call_budget(None, scope_type="service_operation", scope_id="legacy-chat",
                                      receipt_sink=receipts.append):
                result = await gateway.chat([{"role": "user", "content": "fixture"}])

        self.assertEqual(result.raw_usage, usage)
        self.assertEqual(result.usage, {"prompt_tokens": 3610, "completion_tokens": 382, "total_tokens": 4380})
        self.assertEqual(receipts[0]["usage"], usage)
        self.assertEqual(receipts[0]["provider_model"], "provider-alias")
        result.raw_usage["completion_tokens_details"]["reasoning_tokens"] = 999
        self.assertEqual(receipts[0]["usage"]["completion_tokens_details"]["reasoning_tokens"], 388)

    async def test_validated_fallback_retains_each_provider_usage(self) -> None:
        usages = [self._usage(12), self._usage(388)]

        def handler(request: httpx.Request) -> httpx.Response:
            primary = request.url.host == "primary.invalid"
            return httpx.Response(200, json={
                "model": "primary" if primary else "fallback",
                "choices": [{"message": {"content": "bad JSON" if primary else '{"ok":true}'}}],
                "usage": usages[0 if primary else 1],
            })

        receipts, audit = [], []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(self._config(fallback=True), client=client)
            with provider_call_budget(None, scope_type="service_operation", scope_id="legacy-validated",
                                      receipt_sink=receipts.append), validated_model_audit(audit.append):
                result, parsed = await gateway.chat_validated(
                    [{"role": "user", "content": "fixture"}],
                    validator=lambda response: json.loads(response.text), json_mode=True,
                )

        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(result.raw_usage, usages[1])
        self.assertEqual([row["usage"] for row in receipts], usages)
        self.assertEqual([row["fallback_index"] for row in receipts], [0, 1])
        self.assertEqual(result.usage["total_tokens"], 4380)
        self.assertEqual([row["usage"] for row in audit if row["phase"] == "response_received"], usages)
        self.assertEqual([row["usage"] for row in audit if row["phase"] == "finished"], usages)
        result.raw_usage["completion_tokens_details"]["reasoning_tokens"] = 999
        self.assertEqual(audit[-1]["usage"]["completion_tokens_details"]["reasoning_tokens"], 388)

    async def test_legacy_stream_preserves_usage_only_chunk(self) -> None:
        usage = self._usage()
        chunks = [
            {"model": "provider-alias", "choices": [{"delta": {"content": "saved text"}, "finish_reason": "stop"}]},
            {"usage": usage, "choices": []},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        receipts, deltas = [], []
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body),
        )) as client:
            gateway = ModelGateway(self._config(), client=client)
            with provider_call_budget(None, scope_type="service_operation", scope_id="legacy-stream",
                                      receipt_sink=receipts.append):
                result = await gateway.chat_stream(
                    [{"role": "user", "content": "fixture"}], on_delta=deltas.append,
                )

        self.assertEqual(deltas, ["saved text"])
        self.assertEqual(result.raw_usage, usage)
        self.assertEqual(result.usage, {"prompt_tokens": 3610, "completion_tokens": 382, "total_tokens": 4380})
        self.assertEqual(receipts[0]["usage"], usage)
        self.assertEqual(result.finish_reason, "stop")

    async def test_missing_usage_remains_unknown_and_old_construction_works(self) -> None:
        legacy = ChatResult("text", "fixture", {"total_tokens": 7}, "stop")
        self.assertIsNone(legacy.raw_usage)
        receipts = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "text"}}]}),
        )) as client:
            gateway = ModelGateway(self._config(), client=client)
            with provider_call_budget(None, scope_type="service_operation", scope_id="legacy-unknown",
                                      receipt_sink=receipts.append):
                result = await gateway.chat([{"role": "user", "content": "fixture"}])

        self.assertIsNone(result.raw_usage)
        self.assertEqual(result.usage, {})
        self.assertIsNone(receipts[0]["usage"])
        self.assertEqual(receipts[0]["usage_provenance"], "unknown")

    async def test_usage_presence_is_distinct_from_empty_and_zero_counts(self) -> None:
        for mode in ("chat", "stream"):
            for usage in (None, {}, {"total_tokens": 0, "completion_tokens_details": {"reasoning_tokens": 0}}):
                with self.subTest(mode=mode, usage=usage):
                    def handler(_request: httpx.Request) -> httpx.Response:
                        if mode == "stream":
                            chunks = [
                                {"choices": [{"delta": {"content": "text"}}]},
                                {"usage": usage, "choices": []},
                            ]
                            body = "".join(f"data: {json.dumps(row)}\n\n" for row in chunks)
                            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
                        return httpx.Response(200, json={
                            "choices": [{"message": {"content": "text"}}], "usage": usage,
                        })

                    receipts = []
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        gateway = ModelGateway(self._config(), client=client)
                        with provider_call_budget(None, scope_type="service_operation", scope_id="known-usage",
                                                  receipt_sink=receipts.append):
                            if mode == "stream":
                                result = await gateway.chat_stream(
                                    [{"role": "user", "content": "fixture"}], on_delta=lambda _text: None,
                                )
                            else:
                                result = await gateway.chat([{"role": "user", "content": "fixture"}])
                    self.assertEqual(result.raw_usage, usage)
                    self.assertEqual(receipts[0]["usage"], usage)
                    self.assertEqual(receipts[0]["usage_provenance"],
                                     "provider" if usage is not None else "unknown")

    def test_response_snapshot_does_not_share_nested_usage_with_provider(self) -> None:
        usage = self._usage()
        result = read_chat_response({
            "choices": [{"message": {"content": "text"}}], "usage": usage,
        }, "fixture")
        usage["completion_tokens_details"]["reasoning_tokens"] = 999
        self.assertEqual(result.raw_usage["completion_tokens_details"]["reasoning_tokens"], 388)

    def test_validated_audit_uses_present_raw_usage_and_legacy_fallback(self) -> None:
        from fxi_novel.models.validated_audit import (
            receive_validated_model_response, record_validated_model_attempt,
        )

        for raw_usage, expected in (({}, {}), ({"total_tokens": 0}, {"total_tokens": 0}),
                                    (None, {"total_tokens": 7})):
            with self.subTest(raw_usage=raw_usage):
                result = ChatResult("text", "fixture", {"total_tokens": 7}, raw_usage=raw_usage)
                events = []
                with validated_model_audit(events.append):
                    receive_validated_model_response(messages=[], model="fixture", route_index=0, result=result)
                    record_validated_model_attempt(messages=[], model="fixture", route_index=0, result=result)
                self.assertEqual([row["usage"] for row in events], [expected, expected])


if __name__ == "__main__":
    unittest.main()
