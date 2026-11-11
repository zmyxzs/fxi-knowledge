"""Thin OpenAI-compatible chat and embedding adapters with separate routes."""

from __future__ import annotations

import asyncio
import math
import json
import logging
import os
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Iterator, Literal, Protocol, TypeVar, TypedDict
from uuid import uuid4

import httpx

from fxi_novel.models.call_budget import begin_provider_attempt
from fxi_novel.models.validated_audit import (
    receive_validated_model_response,
    record_validated_model_attempt,
    start_validated_model_attempt,
)
from fxi_novel.models.protocol import ChatTurnResult, ToolChoice, ToolDefinition
from fxi_novel.models.gateway_results import (
    ChatResult as ChatResult,
    EmbeddingBatch as EmbeddingBatch,
    ModelProbeErrorCategory as ModelProbeErrorCategory,
    ModelProbeResult as ModelProbeResult,
    read_chat_response,
)
from fxi_novel.models.gateway_support import (
    _merge_stream_value,
    _provider_error_type,
    _raw_usage,
    _usage_counts,
)

logger = logging.getLogger(__name__)
TValidated = TypeVar("TValidated")


class ChatMessage(TypedDict):
    role: str
    content: str


class ModelUnavailable(RuntimeError):
    """Raised when a model route is disabled or its endpoint is incomplete."""


class ModelOutputRejected(ValueError):
    """Raised when every configured route returns output outside its contract."""


class ChatModel(Protocol):
    async def chat_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatTurnResult: ...

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> ChatResult: ...

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        on_delta: Callable[[str], None],
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> ChatResult: ...


class ValidatedChatModel(Protocol):
    """A chat route chain that can retry when deterministic output checks fail."""

    async def chat_validated(
        self,
        messages: list[ChatMessage],
        *,
        validator: Callable[[ChatResult], TValidated],
        temperature: float | None = None,
        json_mode: bool = False,
        response_schema: dict[str, Any] | None = None,
        primary_timeout_seconds: float | None = None,
        retry_invalid_output: bool = False,
    ) -> tuple[ChatResult, TValidated]: ...


class EmbeddingModel(Protocol):
    async def embed_documents(self, texts: list[str]) -> EmbeddingBatch: ...

    async def embed_query(self, text: str) -> EmbeddingBatch: ...


@dataclass(frozen=True)
class ModelRoute:
    enabled: bool
    base_url: str
    model: str
    token_env: str
    timeout_seconds: float
    dimensions: int = 0
    query_prefix: str = ""
    route_id: str = field(default_factory=lambda: str(uuid4()))
    api_key: str = field(default="", repr=False, compare=False)
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    merge_system_prompt: bool = False

    def request_url(self, operation: str) -> str:
        if not self.enabled:
            raise ModelUnavailable(f"{operation} route is disabled")
        if not self.base_url or not self.model:
            raise ModelUnavailable(f"{operation} route requires base_url and model")
        return f"{self.base_url.rstrip('/')}/{operation}"

    def headers(self) -> dict[str, str]:
        token = self.api_key or (os.environ.get(self.token_env, "") if self.token_env else "")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}


def _merge_system_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Fold system prompts into the first user message for providers that ignore system."""
    system_parts = [
        str(message.get("content") or "")
        for message in messages if message.get("role") == "system"
    ]
    others = [message for message in messages if message.get("role") != "system"]
    if not system_parts or not others:
        return messages
    merged: list[ChatMessage] = [{
        "role": "user",
        "content": "\n\n".join(system_parts) + "\n\n" + str(others[0].get("content") or ""),
    }]
    merged.extend(others[1:])
    return merged


def _route_reasoning_parameters(route: ModelRoute) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for name, maximum in (("temperature", 2), ("top_p", 1)):
        value = getattr(route, name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ModelUnavailable(f"{name} must be finite and between zero and {maximum}")
            parameters[name] = value
    deepseek = "deepseek" in f"{route.model} {route.base_url}".casefold()
    if route.max_output_tokens is not None:
        if route.max_output_tokens < 1:
            raise ModelUnavailable("model output budget must be positive")
        parameters["max_tokens"] = route.max_output_tokens
    if route.reasoning_effort is not None and not (deepseek and route.reasoning_effort == "none"):
        parameters["reasoning_effort"] = route.reasoning_effort
    if deepseek:
        if route.reasoning_effort == "none":
            parameters["thinking"] = {"type": "disabled"}
        elif route.reasoning_effort is not None:
            parameters["thinking"] = {"type": "enabled"}
    return parameters


@dataclass(frozen=True)
class GatewayConfig:
    chat: ModelRoute
    embedding: ModelRoute
    chat_fallbacks: tuple[ModelRoute, ...] = ()
    knowledge_query: ModelRoute | None = None
    creative: ModelRoute | None = None

    @classmethod
    def from_toml(cls, path: Path) -> GatewayConfig:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        chat = _route_from_mapping(raw.get("chat", {}))
        embedding = _route_from_mapping(raw.get("embedding", {}))
        fallback_rows = raw.get("chat_fallbacks", [])
        if not isinstance(fallback_rows, list) or any(
            not isinstance(item, dict) for item in fallback_rows
        ):
            raise ValueError("chat_fallbacks must be an array of route tables")
        fallbacks = tuple(_route_from_mapping(item) for item in fallback_rows)
        query_row = raw.get("knowledge_query")
        if query_row is not None and not isinstance(query_row, dict):
            raise ValueError("knowledge_query must be a route table")
        creative_row = raw.get("creative")
        if creative_row is not None and not isinstance(creative_row, dict):
            raise ValueError("creative must be a route table")
        return cls(
            chat=chat,
            embedding=embedding,
            chat_fallbacks=fallbacks,
            knowledge_query=_route_from_mapping(query_row) if query_row is not None else None,
            creative=_route_from_mapping(creative_row) if creative_row is not None else None,
        )


def _route_from_mapping(raw: dict[str, Any]) -> ModelRoute:
    return ModelRoute(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url", "")),
        model=str(raw.get("model", "")),
        token_env=str(raw.get("token_env", "")),
        timeout_seconds=float(raw.get("timeout_seconds", 90)),
        dimensions=int(raw.get("dimensions", 0)),
        query_prefix=str(raw.get("query_prefix", "")),
        route_id=str(raw.get("route_id") or uuid4()),
        api_key=str(raw.get("api_key", "")),
        reasoning_effort=(
            str(raw["reasoning_effort"]).strip()
            if raw.get("reasoning_effort") is not None
            else None
        ),
        max_output_tokens=raw.get("max_output_tokens"),
        temperature=raw.get("temperature"),
        top_p=raw.get("top_p"),
    )


def _model_probe_error_summary(error: Exception) -> tuple[ModelProbeErrorCategory, str]:
    causes: list[BaseException] = []
    pending: BaseException | None = error
    seen: set[int] = set()
    while pending is not None and id(pending) not in seen:
        seen.add(id(pending))
        causes.append(pending)
        pending = pending.__cause__ or pending.__context__

    if any(isinstance(cause, (TimeoutError, httpx.TimeoutException)) for cause in causes):
        return "timeout", "模型服务请求超时，请检查服务负载或超时设置。"
    status_error = next(
        (cause for cause in causes if isinstance(cause, httpx.HTTPStatusError)),
        None,
    )
    if status_error is not None:
        status_code = status_error.response.status_code
        if status_code in {401, 403}:
            diagnostic = f"模型服务鉴权失败（HTTP {status_code}），请检查鉴权设置。"
        elif status_code == 429:
            diagnostic = "模型服务当前限流（HTTP 429），请稍后再试或检查配额。"
        else:
            diagnostic = f"模型服务返回 HTTP {status_code}。"
        return "http_error", diagnostic
    if any(isinstance(cause, httpx.TransportError) for cause in causes):
        return "connection", "无法连接模型服务，请检查服务地址、进程和网络。"
    if any(isinstance(cause, (ValueError, TypeError, KeyError)) for cause in causes):
        return "invalid_response", "模型服务返回了不符合协议的响应。"
    if any(isinstance(cause, ModelUnavailable) for cause in causes):
        return "unavailable", "当前聊天路由不可用，请检查路由配置。"
    if any(isinstance(cause, httpx.HTTPError) for cause in causes):
        return "connection", "模型服务请求未能完成，请检查服务状态和网络。"
    return "internal_error", "模型探测失败，请检查后端服务日志。"


class ModelGateway:
    """One HTTP client, two explicitly separate model capabilities."""

    _probe_timeout_seconds = 15.0

    def __init__(
        self,
        config: GatewayConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_config = config
        self._request_config: ContextVar[GatewayConfig | None] = ContextVar(
            f"fxi_model_config_{id(self)}",
            default=None,
        )
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient()

    @property
    def config(self) -> GatewayConfig:
        request_config = self._request_config.get()
        return request_config if request_config is not None else self._base_config

    @config.setter
    def config(self, value: GatewayConfig) -> None:
        self._base_config = value

    @contextmanager
    def pin_config(self) -> Iterator[None]:
        """Keep one HTTP request and its child tasks on the same model routes."""
        token = self._request_config.set(self._base_config)
        try:
            yield
        finally:
            self._request_config.reset(token)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _with_provider_attempt(
        self,
        route: ModelRoute,
        operation: str,
        call: Callable[[], Awaitable[Any]],
        *,
        fallback_index: int = 0,
        fallback_reason: str | None = None,
    ) -> Any:
        receipt = begin_provider_attempt(
            operation=operation,
            route_id=route.route_id,
            configured_model=route.model,
            fallback_index=fallback_index,
            fallback_reason=fallback_reason,
        )
        try:
            result = await call()
        except BaseException as exc:
            if receipt is not None:
                receipt.fail(exc)
            raise
        if receipt is not None:
            usage = getattr(result, "raw_usage", None)
            if usage is None:
                # An empty numeric projection does not establish provider usage.
                # A present raw {} above remains distinct from a missing field.
                usage = getattr(result, "usage", None) or None
            receipt.complete(
                provider_model=str(getattr(result, "provider_model", route.model)),
                usage=usage if isinstance(usage, dict) else None,
            )
        return result

    def request_parameters(self) -> dict[str, Any]:
        """Effective route defaults for the pinned request, without credentials."""
        return _route_reasoning_parameters(self.config.chat)

    async def chat_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        on_delta: Callable[[str], None] | None = None,
        allow_stateless_fallback: bool = False,
        allow_same_endpoint_fallback: bool = False,
    ) -> ChatTurnResult:
        """Make a lossless assistant-protocol call on the selected route.

        Streams remain pinned to one route. A caller may explicitly allow
        fallback for a failed non-streaming call on the same endpoint, including
        an OpenAI-compatible tool transcript. Completed tool calls are not
        replayed; only the unanswered model request is retried.
        """
        route = self.config.chat
        if not route.enabled:
            raise ModelUnavailable("primary chat route is disabled")
        if not route.base_url or not route.model:
            raise ModelUnavailable("primary chat route requires base_url and model")

        if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
            raise ValueError("max_tokens must be a positive integer")

        def route_payload(selected: ModelRoute) -> dict[str, Any]:
            payload: dict[str, Any] = {"model": selected.model, "messages": messages}
            payload.update(_route_reasoning_parameters(selected))
            if tools:
                payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            return payload

        if allow_stateless_fallback and allow_same_endpoint_fallback:
            raise ValueError("select one chat-turn fallback policy")
        if allow_stateless_fallback or allow_same_endpoint_fallback:
            if tools or tool_choice not in (None, "none") or on_delta is not None:
                if not allow_same_endpoint_fallback or on_delta is not None:
                    raise ValueError("stateless fallback requires a non-streaming call without tools")
            routes = self._chat_routes()
            if allow_same_endpoint_fallback:
                routes = tuple(selected for selected in routes if (
                    selected.base_url.rstrip("/") == route.base_url.rstrip("/")
                    and selected.token_env == route.token_env
                    and selected.api_key == route.api_key
                ))
            fallback_reason: str | None = None
            for index, selected in enumerate(routes):
                try:
                    return await self._with_provider_attempt(
                        selected, "chat_turn",
                        lambda: self._chat_turn_once(selected, route_payload(selected)),
                        fallback_index=index, fallback_reason=fallback_reason,
                    )
                except (ModelUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                    if index + 1 == len(routes):
                        raise
                    self._log_chat_fallback(selected, routes[index + 1], exc)
                    fallback_reason = _provider_error_type(exc)
            raise ModelUnavailable("no chat route is enabled")

        payload = route_payload(route)

        if on_delta is None:
            return await self._with_provider_attempt(
                route,
                "chat_turn",
                lambda: self._chat_turn_once(route, payload),
            )
        payload["stream"] = True
        return await self._with_provider_attempt(
            route,
            "chat_turn_stream",
            lambda: self._chat_turn_stream(route, payload, on_delta=on_delta),
        )

    async def _chat_turn_once(
        self,
        route: ModelRoute,
        payload: dict[str, Any],
    ) -> ChatTurnResult:
        try:
            async with asyncio.timeout(route.timeout_seconds):
                response = await self.client.post(
                    route.request_url("chat/completions"),
                    headers=route.headers(),
                    json=payload,
                    timeout=route.timeout_seconds,
                )
        except TimeoutError as exc:
            raise ModelUnavailable(
                f"chat model '{route.model}' exceeded its {route.timeout_seconds:g}s total deadline"
            ) from exc
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("chat provider returned a non-object response")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("chat provider returned no choices")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("chat provider returned an invalid assistant message")
        calls = message.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            raise ValueError("chat provider returned invalid tool calls")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("chat provider returned unsupported non-text assistant content")
        if not (isinstance(content, str) and content.strip()) and not calls:
            raise ValueError("chat provider returned neither text nor tool calls")
        return ChatTurnResult(
            message=dict(message),
            provider_model=str(data.get("model") or route.model),
            finish_reason=(
                str(choice["finish_reason"])
                if choice.get("finish_reason") is not None
                else None
            ),
            raw_usage=_raw_usage(data.get("usage")),
            response_id=str(data["id"]) if data.get("id") is not None else None,
            response_extensions={
                key: value
                for key, value in data.items()
                if key not in {"id", "model", "choices", "usage"}
            },
            choice_extensions={
                key: value for key, value in choice.items() if key not in {"message", "finish_reason"}
            },
        )

    async def _chat_turn_stream(
        self,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        on_delta: Callable[[str], None],
    ) -> ChatTurnResult:
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        other_message_fields: dict[str, Any] = {}
        provider_model = route.model
        response_id: str | None = None
        response_extensions: dict[str, Any] = {}
        choice_extensions: dict[str, Any] = {}
        raw_usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        saw_chunk = False
        try:
            async with asyncio.timeout(route.timeout_seconds):
                async with self.client.stream(
                    "POST",
                    route.request_url("chat/completions"),
                    headers=route.headers(),
                    json=payload,
                    timeout=route.timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw_line = line[5:].strip()
                        if not raw_line or raw_line == "[DONE]":
                            continue
                        try:
                            data = json.loads(raw_line)
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            raise ValueError("chat provider returned a non-object stream chunk")
                        saw_chunk = True
                        if data.get("id") is not None:
                            response_id = str(data["id"])
                        if isinstance(data.get("model"), str):
                            provider_model = data["model"]
                        if data.get("usage") is not None:
                            raw_usage = _raw_usage(data["usage"])
                        for key, value in data.items():
                            if key not in {"id", "model", "choices", "usage", "object", "created"}:
                                _merge_stream_value(response_extensions, key, value)
                        choices = data.get("choices") or []
                        if not isinstance(choices, list) or not choices:
                            continue
                        if not isinstance(choices[0], dict):
                            raise ValueError("chat provider returned invalid stream choices")
                        choice = choices[0]
                        if choice.get("finish_reason") is not None:
                            finish_reason = str(choice["finish_reason"])
                        for key, value in choice.items():
                            if key not in {"delta", "finish_reason", "index"}:
                                _merge_stream_value(choice_extensions, key, value)
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            raise ValueError("chat provider returned invalid stream delta")
                        if delta.get("role") is not None:
                            other_message_fields["role"] = delta["role"]
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            content_parts.append(content)
                            on_delta(content)
                        elif content is not None and not isinstance(content, str):
                            raise ValueError("chat provider returned unsupported streamed content")
                        streamed_calls = delta.get("tool_calls")
                        if streamed_calls is not None:
                            if not isinstance(streamed_calls, list):
                                raise ValueError("chat provider returned invalid streamed tool calls")
                            for call_delta in streamed_calls:
                                if not isinstance(call_delta, dict):
                                    raise ValueError("chat provider returned invalid streamed tool call")
                                index = call_delta.get("index")
                                if not isinstance(index, int) or index < 0:
                                    raise ValueError("chat provider omitted streamed tool call index")
                                target = tool_calls.setdefault(index, {})
                                for key, value in call_delta.items():
                                    if key == "index":
                                        continue
                                    if key == "function" and isinstance(value, dict):
                                        function = target.setdefault("function", {})
                                        if not isinstance(function, dict):
                                            raise ValueError("chat provider streamed an invalid function")
                                        for function_key, function_value in value.items():
                                            _merge_stream_value(function, function_key, function_value)
                                    else:
                                        _merge_stream_value(target, key, value)
                        for key, value in delta.items():
                            if key not in {"role", "content", "tool_calls"}:
                                _merge_stream_value(other_message_fields, key, value)
        except TimeoutError as exc:
            raise ModelUnavailable(
                f"chat model '{route.model}' exceeded its {route.timeout_seconds:g}s total deadline"
            ) from exc

        calls = [tool_calls[index] for index in sorted(tool_calls)]
        message: dict[str, Any] = {"role": other_message_fields.pop("role", "assistant")}
        message.update(other_message_fields)
        if content_parts:
            message["content"] = "".join(content_parts)
        elif "content" in other_message_fields:
            message["content"] = other_message_fields.pop("content")
        if calls:
            message["tool_calls"] = calls
        if not saw_chunk or (not (message.get("content") or "").strip() and not calls):
            raise ValueError("chat provider returned neither streamed text nor tool calls")
        return ChatTurnResult(
            message=message,
            provider_model=provider_model,
            finish_reason=finish_reason,
            raw_usage=raw_usage,
            response_id=response_id,
            response_extensions=response_extensions,
            choice_extensions=choice_extensions,
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        routes = self._chat_routes()
        fallback_reason: str | None = None
        for index, route in enumerate(routes):
            try:
                return await self._chat_with_route(
                    route,
                    messages,
                    temperature=temperature,
                    json_mode=json_mode,
                    fallback_index=index,
                    fallback_reason=fallback_reason,
                )
            except (ModelUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                if index + 1 == len(routes):
                    raise ModelUnavailable("all configured chat routes failed") from exc
                self._log_chat_fallback(route, routes[index + 1], exc)
                fallback_reason = _provider_error_type(exc)
        raise ModelUnavailable("no chat route is enabled")

    async def chat_validated(
        self,
        messages: list[ChatMessage],
        *,
        validator: Callable[[ChatResult], TValidated],
        temperature: float | None = None,
        json_mode: bool = False,
        response_schema: dict[str, Any] | None = None,
        primary_timeout_seconds: float | None = None,
        retry_invalid_output: bool = False,
    ) -> tuple[ChatResult, TValidated]:
        """Try the configured route chain until a provider response passes validation.

        This lets a caller use its configured fallback when the primary model returns
        malformed or unusable structured output, without making transport retries a
        domain-specific concern.
        """
        routes = self._chat_routes()
        if not routes:
            raise ModelUnavailable("no chat route is enabled")

        rejected_outputs: list[Exception] = []
        fallback_reason: str | None = None
        for index, route in enumerate(routes):
            effective_route = (
                replace(route, timeout_seconds=primary_timeout_seconds)
                if index == 0 and primary_timeout_seconds is not None
                else route
            )
            attempt_messages = messages
            route_failed = False
            for attempt in range(2 if retry_invalid_output else 1):
                # Domain audit sinks can wait for SQLite's writer. Keep that wait
                # off the event loop so other concurrent parser tasks can finish.
                await asyncio.to_thread(
                    start_validated_model_attempt,
                    messages=attempt_messages, model=route.model, route_index=index,
                )
                try:
                    result = await self._chat_with_route(
                        effective_route,
                        attempt_messages,
                        temperature=temperature,
                        json_mode=json_mode,
                        response_schema=response_schema,
                        fallback_index=index,
                        fallback_reason=fallback_reason,
                    )
                except (ModelUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                    received = getattr(exc, "result", None)
                    if isinstance(received, ChatResult):
                        await asyncio.to_thread(receive_validated_model_response,
                            messages=attempt_messages, model=route.model, route_index=index, result=received,
                        )
                        rejected_outputs.append(exc)
                    else:
                        received = None
                    await asyncio.to_thread(record_validated_model_attempt,
                        messages=attempt_messages, model=route.model, route_index=index, result=received, error=exc,
                    )
                    if index + 1 == len(routes):
                        if rejected_outputs:
                            raise ModelOutputRejected(
                                "configured routes did not return contract-valid output"
                            ) from rejected_outputs[-1]
                        raise ModelUnavailable("all configured chat routes failed") from exc
                    self._log_chat_fallback(route, routes[index + 1], exc)
                    fallback_reason = _provider_error_type(exc)
                    route_failed = True
                    break

                await asyncio.to_thread(receive_validated_model_response,
                    messages=attempt_messages, model=route.model, route_index=index, result=result,
                )
                try:
                    validated = validator(result)
                except (ValueError, TypeError, KeyError) as exc:
                    await asyncio.to_thread(record_validated_model_attempt,
                        messages=attempt_messages, model=route.model, route_index=index, result=result, error=exc,
                    )
                    rejected_outputs.append(exc)
                    if attempt == 0:
                        attempt_messages = [
                            *messages,
                            {
                                "role": "user",
                                "content": (
                                    "上一次响应未通过结构校验："
                                    f"{str(exc)[:300]}。"
                                    "请只输出符合要求的 JSON 对象，不要解释，也不要 Markdown 代码围栏。"
                                ),
                            },
                        ]
                        continue
                    if index + 1 == len(routes):
                        raise ModelOutputRejected(
                            "all configured chat routes returned invalid output"
                        ) from exc
                    logger.warning(
                        "Chat route '%s' output failed validation; trying configured fallback '%s' (%s)",
                        route.model,
                        routes[index + 1].model,
                        type(exc).__name__,
                    )
                    fallback_reason = f"output_validation:{_provider_error_type(exc)}"
                    route_failed = True
                    break
                await asyncio.to_thread(record_validated_model_attempt,
                    messages=attempt_messages, model=route.model, route_index=index, result=result,
                )
                return result, validated
            if route_failed:
                continue

        raise ModelUnavailable("all configured chat routes failed")

    def _chat_routes(self) -> tuple[ModelRoute, ...]:
        configured = (self.config.chat, *self.config.chat_fallbacks)
        return tuple(route for route in configured if route.enabled)

    async def probe(
        self,
        route_type: Literal["chat", "embedding"],
    ) -> ModelProbeResult:
        """Make one explicit, bounded probe through the existing gateway path."""
        config = self.config
        started_at = perf_counter()

        def result(
            available: bool,
            error_category: ModelProbeErrorCategory | None,
            diagnostic: str,
        ) -> ModelProbeResult:
            if self._base_config is not config:
                available = False
                error_category = "configuration_changed"
                diagnostic = "模型配置在检测期间已更新，请重新检测。"
            return ModelProbeResult(
                route_type=route_type,
                available=available,
                error_category=error_category,
                diagnostic=diagnostic,
                elapsed_ms=max(0, round((perf_counter() - started_at) * 1000)),
                checked_at=datetime.now(timezone.utc),
            )

        if route_type == "chat":
            routes = tuple(route for route in (config.chat, *config.chat_fallbacks) if route.enabled)
            if not routes:
                return result(False, "disabled", "没有启用的聊天路由。")
            if not any(route.base_url.strip() and route.model.strip() for route in routes):
                return result(False, "misconfigured", "已启用的聊天路由缺少服务地址或模型 ID。")
        else:
            route = config.embedding
            if not route.enabled:
                return result(False, "disabled", "Embedding 路由未启用。")
            if not route.base_url.strip() or not route.model.strip():
                return result(False, "misconfigured", "Embedding 路由缺少服务地址或模型 ID。")

        try:
            async with asyncio.timeout(self._probe_timeout_seconds):
                if route_type == "chat":
                    await self.chat(
                        [{"role": "user", "content": "Reply with the single token OK."}],
                    )
                else:
                    await self.embed_query("FXI Novel connectivity probe")
        except Exception as exc:
            error_category, diagnostic = _model_probe_error_summary(exc)
            return result(False, error_category, diagnostic)

        diagnostic = (
            "聊天路由可用。"
            if route_type == "chat"
            else "Embedding 端点可用；当前尚未接入向量检索。"
        )
        return result(True, None, diagnostic)

    async def _chat_with_route(
        self,
        route: ModelRoute,
        messages: list[ChatMessage],
        *,
        temperature: float | None,
        json_mode: bool,
        response_schema: dict[str, Any] | None = None,
        fallback_index: int = 0,
        fallback_reason: str | None = None,
    ) -> ChatResult:
        return await self._with_provider_attempt(
            route,
            "chat",
            lambda: self._chat_with_route_once(
                route,
                messages,
                temperature=temperature,
                json_mode=json_mode,
                response_schema=response_schema,
            ),
            fallback_index=fallback_index,
            fallback_reason=fallback_reason,
        )

    async def _chat_with_route_once(
        self,
        route: ModelRoute,
        messages: list[ChatMessage],
        *,
        temperature: float | None,
        json_mode: bool,
        response_schema: dict[str, Any] | None = None,
    ) -> ChatResult:
        if route.merge_system_prompt:
            messages = _merge_system_messages(messages)
        payload: dict[str, Any] = {"model": route.model, "messages": messages}
        payload.update(_route_reasoning_parameters(route))
        if temperature is not None:
            payload["temperature"] = temperature
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": False,
                    "schema": response_schema,
                },
            }
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with asyncio.timeout(route.timeout_seconds):
                response = await self.client.post(
                    route.request_url("chat/completions"),
                    headers=route.headers(),
                    json=payload,
                    timeout=route.timeout_seconds,
                )
        except TimeoutError as exc:
            raise ModelUnavailable(
                f"chat model '{route.model}' exceeded its {route.timeout_seconds:g}s total deadline"
            ) from exc
        response.raise_for_status()
        return read_chat_response(response.json(), route.model)

    @staticmethod
    def _log_chat_fallback(
        failed_route: ModelRoute,
        fallback_route: ModelRoute,
        error: Exception,
    ) -> None:
        logger.warning(
            "Chat route '%s' failed; trying configured fallback '%s' (%s)",
            failed_route.model,
            fallback_route.model,
            type(error).__name__,
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        on_delta: Callable[[str], None],
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        """Read OpenAI-compatible SSE chunks while retaining the regular ChatResult contract."""
        routes = self._chat_routes()
        fallback_reason: str | None = None
        for index, route in enumerate(routes):
            payload: dict[str, Any] = {
                "model": route.model,
                "messages": messages,
                "stream": True,
            }
            payload.update(_route_reasoning_parameters(route))
            if temperature is not None:
                payload["temperature"] = temperature
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            text_parts: list[str] = []
            provider_model = route.model
            usage: dict[str, int] = {}
            raw_usage: dict[str, Any] | None = None
            finish_reason: str | None = None
            attempt = begin_provider_attempt(
                operation="chat_stream",
                route_id=route.route_id,
                configured_model=route.model,
                fallback_index=index,
                fallback_reason=fallback_reason,
            )
            try:
                async with asyncio.timeout(route.timeout_seconds):
                    async with self.client.stream(
                        "POST",
                        route.request_url("chat/completions"),
                        headers=route.headers(),
                        json=payload,
                        timeout=route.timeout_seconds,
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_line = line[5:].strip()
                            if not data_line or data_line == "[DONE]":
                                continue
                            try:
                                data = json.loads(data_line)
                            except ValueError:
                                continue
                            if not isinstance(data, dict):
                                raise ValueError("chat provider returned a non-object stream chunk")
                            if isinstance(data.get("model"), str):
                                provider_model = data["model"]
                            if data.get("usage") is not None:
                                usage = _usage_counts(data["usage"])
                                raw_usage = _raw_usage(data["usage"])
                            choices = data.get("choices") or []
                            if not choices:
                                continue
                            if not isinstance(choices, list) or not isinstance(choices[0], dict):
                                raise ValueError("chat provider returned invalid stream choices")
                            if choices[0].get("finish_reason") is not None:
                                finish_reason = str(choices[0]["finish_reason"])
                            delta_data = choices[0].get("delta") or {}
                            if not isinstance(delta_data, dict):
                                raise ValueError("chat provider returned invalid stream delta")
                            delta = delta_data.get("content")
                            if not isinstance(delta, str) or not delta:
                                continue
                            text_parts.append(delta)
                            on_delta(delta)
                if not text_parts:
                    raise ValueError("chat provider returned an empty stream")
                result = ChatResult(
                    text="".join(text_parts),
                    provider_model=provider_model,
                    usage=usage,
                    finish_reason=finish_reason,
                    raw_usage=raw_usage,
                )
            except (ModelUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError) as exc:
                if attempt is not None:
                    attempt.fail(exc)
                if text_parts:
                    raise ModelUnavailable(
                        "chat stream was interrupted after partial output; fallback was suppressed"
                    ) from exc
                if index + 1 == len(routes):
                    if isinstance(exc, TimeoutError):
                        raise ModelUnavailable(
                            f"chat model '{route.model}' exceeded its {route.timeout_seconds:g}s total deadline"
                        ) from exc
                    raise ModelUnavailable("all configured chat routes failed") from exc
                self._log_chat_fallback(route, routes[index + 1], exc)
                fallback_reason = _provider_error_type(exc)
                continue
            except BaseException as exc:
                if attempt is not None:
                    attempt.fail(exc)
                raise
            if attempt is not None:
                attempt.complete(
                    provider_model=provider_model,
                    usage=raw_usage,
                )
            return result
        raise ModelUnavailable("no chat route is enabled")

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        return await self._embed(texts)

    async def embed_documents_for_route(
        self, texts: list[str], route: ModelRoute
    ) -> EmbeddingBatch:
        """Use the route captured by a durable background job."""
        return await self._embed(texts, route_override=route)

    async def embed_query(self, text: str) -> EmbeddingBatch:
        route = self.config.embedding
        query = f"{route.query_prefix}{text}"
        return await self._embed([query], route_override=route)

    async def _embed(
        self, texts: list[str], *, route_override: ModelRoute | None = None
    ) -> EmbeddingBatch:
        route = route_override or self.config.embedding
        if not texts:
            return EmbeddingBatch([], route.model, route.dimensions)
        return await self._with_provider_attempt(
            route,
            "embedding",
            lambda: self._embed_once(texts, route),
        )

    async def _embed_once(self, texts: list[str], route: ModelRoute) -> EmbeddingBatch:
        payload: dict[str, Any] = {"model": route.model, "input": texts}
        if route.dimensions > 0:
            payload["dimensions"] = route.dimensions
        response = await self.client.post(
            route.request_url("embeddings"),
            headers=route.headers(),
            json=payload,
            timeout=route.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        rows = data.get("data") or []
        if len(rows) != len(texts):
            raise ValueError("embedding provider returned a different vector count")
        ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
        vectors = [[float(value) for value in row.get("embedding", [])] for row in ordered]
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or not vectors or not next(iter(dimensions)):
            raise ValueError("embedding provider returned inconsistent or empty dimensions")
        dimension = next(iter(dimensions))
        if route.dimensions > 0 and dimension != route.dimensions:
            raise ValueError(f"embedding dimension mismatch: expected {route.dimensions}, got {dimension}")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise ValueError("embedding provider returned a non-finite number")
        return EmbeddingBatch(
            vectors=vectors,
            provider_model=str(data.get("model") or route.model),
            dimension=dimension,
            usage=_raw_usage(data.get("usage")),
        )
