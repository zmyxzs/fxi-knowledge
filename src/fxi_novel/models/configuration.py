"""Local model-route configuration management for the settings API."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any

from fxi_novel.models.gateway import GatewayConfig, ModelGateway, ModelRoute


def interactive_gateway_config(config: GatewayConfig) -> GatewayConfig:
    """Honor the selected primary and explicitly enabled fallback routes."""
    return replace(
        config,
        chat_fallbacks=tuple(route for route in config.chat_fallbacks
                             if route.enabled and route.route_id != config.chat.route_id),
    )


def creative_gateway_config(config: GatewayConfig) -> GatewayConfig:
    """Use an explicit creative route, or the ordinary interactive routes."""
    if config.creative is None or not config.creative.enabled:
        return interactive_gateway_config(config)
    creative = config.creative
    # A separately configured creative provider remains isolated. When its
    # endpoint and credential are the same as an interactive fallback, that
    # configured fallback can recover an unanswered structured planning call.
    fallbacks = tuple(route for route in interactive_gateway_config(config).chat_fallbacks
        if (route.base_url.rstrip("/") == creative.base_url.rstrip("/")
            and route.token_env == creative.token_env
            and route.api_key == creative.api_key
            and route.model != creative.model))
    return GatewayConfig(chat=creative, embedding=config.embedding, chat_fallbacks=fallbacks)


def _chat_request_identity(route: ModelRoute) -> tuple[Any, ...]:
    return (
        route.base_url.rstrip("/"), route.model, route.token_env, route.api_key,
        route.reasoning_effort, route.max_output_tokens, route.temperature, route.top_p,
    )


def knowledge_query_gateway_config(config: GatewayConfig) -> GatewayConfig:
    """Derive the stable query routes used by each request from current settings."""
    interactive = interactive_gateway_config(config)
    query = config.knowledge_query
    if query is None:
        query = interactive.chat
    query = replace(query, max_output_tokens=query.max_output_tokens or 8_000)
    fallbacks = []
    for configured in interactive.chat_fallbacks:
        route = replace(configured, max_output_tokens=configured.max_output_tokens or 8_000)
        if query.enabled and route.enabled and _chat_request_identity(route) == _chat_request_identity(query):
            # The general chat fallback can be the selected knowledge model.
            # Keep its configured waiting allowance on the original request,
            # rather than cancelling that request and submitting it again.
            query = replace(query, timeout_seconds=max(query.timeout_seconds, route.timeout_seconds))
        else:
            fallbacks.append(route)
    return GatewayConfig(
        chat=query,
        embedding=config.embedding,
        chat_fallbacks=tuple(fallbacks),
    )


def knowledge_parse_gateway_config(config: GatewayConfig) -> GatewayConfig:
    """Use the configured knowledge route for source extraction and review."""
    return knowledge_query_gateway_config(config)


def prose_edit_gateway_config(config: GatewayConfig) -> GatewayConfig:
    """Select the first enabled, distinct editing model on the primary endpoint."""
    config = interactive_gateway_config(config)
    independent = [
        route for route in config.chat_fallbacks
        if route.enabled and route.model and route.model != config.chat.model
    ]
    route = next(
        (candidate for candidate in independent
         if candidate.base_url.rstrip("/") == config.chat.base_url.rstrip("/")),
        independent[0] if independent else config.chat,
    )
    return GatewayConfig(chat=route, embedding=config.embedding)


def prose_review_gateway_config(config: GatewayConfig) -> GatewayConfig:
    """Select one configured reviewer route distinct from the prose generator."""
    config = interactive_gateway_config(config)
    independent = [
        route for route in config.chat_fallbacks
        if route.enabled and route.model and route.model != config.chat.model
    ]
    route = next(
        (candidate for candidate in independent
         if candidate.base_url.rstrip("/") != config.chat.base_url.rstrip("/")),
        independent[0] if independent else config.chat,
    )
    return GatewayConfig(chat=route, embedding=config.embedding)


class ModelConfigurationManager:
    """Own the ignored local TOML settings and apply updates to future requests."""

    def __init__(
        self,
        path: str | Path,
        config: GatewayConfig,
        model_gateway: ModelGateway,
        query_gateway: ModelGateway,
        review_gateway: ModelGateway | None = None,
        edit_gateway: ModelGateway | None = None,
        parse_gateway: ModelGateway | None = None,
        creative_gateway: ModelGateway | None = None,
    ) -> None:
        self.path = Path(path)
        self._config = config
        self.model_gateway = model_gateway
        self.query_gateway = query_gateway
        self.review_gateway = review_gateway
        self.edit_gateway = edit_gateway
        self.parse_gateway = parse_gateway
        self.creative_gateway = creative_gateway
        self._lock = RLock()

    def public_config(self) -> dict[str, Any]:
        with self._lock:
            return {
                "chat": self._public_route(self._config.chat),
                "chat_fallbacks": [
                    self._public_route(route) for route in self._config.chat_fallbacks
                ],
                "embedding": self._public_route(self._config.embedding),
                "knowledge_query": (
                    self._public_route(self._config.knowledge_query)
                    if self._config.knowledge_query is not None
                    else None
                ),
                "creative": (
                    self._public_route(self._config.creative)
                    if self._config.creative is not None else None
                ),
            }

    def update(
        self,
        config: GatewayConfig,
        secret_updates: dict[str, tuple[str, str | None]] | None = None,
        *,
        preserve_creative: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if preserve_creative:
                config = replace(config, creative=self._config.creative)
            route_ids = [route.route_id for route in (
                config.chat, *config.chat_fallbacks, config.embedding,
                *((config.knowledge_query,) if config.knowledge_query is not None else ()),
                *((config.creative,) if config.creative is not None else ()),
            )]
            if len(route_ids) != len(set(route_ids)):
                raise ValueError("model route IDs must be unique")
            config = self._apply_secret_updates(config, secret_updates or {})
            self._write_config(config)
            self._config = config
            self.model_gateway.config = interactive_gateway_config(config)
            self.query_gateway.config = knowledge_query_gateway_config(config)
            if self.review_gateway is not None:
                self.review_gateway.config = prose_review_gateway_config(config)
            if self.edit_gateway is not None:
                self.edit_gateway.config = prose_edit_gateway_config(config)
            if self.parse_gateway is not None:
                self.parse_gateway.config = knowledge_parse_gateway_config(config)
            if self.creative_gateway is not None:
                self.creative_gateway.config = creative_gateway_config(config)
            return self.public_config()

    def resolve_probe_route(
        self,
        route: ModelRoute,
        action: str,
        submitted_key: str | None,
    ) -> ModelRoute:
        """Resolve a draft route's credential without exposing saved values."""
        with self._lock:
            return self._resolve_route_secret(route, action, submitted_key, self._config)

    def _apply_secret_updates(
        self,
        config: GatewayConfig,
        secret_updates: dict[str, tuple[str, str | None]],
    ) -> GatewayConfig:
        routes = {
            route.route_id: route
            for route in (
                self._config.chat,
                *self._config.chat_fallbacks,
                self._config.embedding,
                *((self._config.knowledge_query,) if self._config.knowledge_query is not None else ()),
                *((self._config.creative,) if self._config.creative is not None else ()),
            )
        }

        def resolve(route: ModelRoute) -> ModelRoute:
            action, submitted_key = secret_updates.get(route.route_id, ("keep", None))
            return self._resolve_route_secret(route, action, submitted_key, self._config, routes)

        return replace(
            config,
            chat=resolve(config.chat),
            chat_fallbacks=tuple(resolve(route) for route in config.chat_fallbacks),
            embedding=resolve(config.embedding),
            knowledge_query=(
                resolve(config.knowledge_query)
                if config.knowledge_query is not None
                else None
            ),
            creative=resolve(config.creative) if config.creative is not None else None,
        )

    @staticmethod
    def _resolve_route_secret(
        route: ModelRoute,
        action: str,
        submitted_key: str | None,
        current_config: GatewayConfig,
        known_routes: dict[str, ModelRoute] | None = None,
    ) -> ModelRoute:
        if action == "set":
            return replace(route, api_key=submitted_key or "")
        if action == "clear":
            return replace(route, api_key="")
        if known_routes is None:
            known_routes = {
                candidate.route_id: candidate
                for candidate in (
                    current_config.chat,
                    *current_config.chat_fallbacks,
                    current_config.embedding,
                    *((current_config.knowledge_query,) if current_config.knowledge_query is not None else ()),
                    *((current_config.creative,) if current_config.creative is not None else ()),
                )
            }
        existing = known_routes.get(route.route_id)
        return replace(route, api_key=existing.api_key if existing is not None else route.api_key)

    @staticmethod
    def _public_route(route: ModelRoute) -> dict[str, Any]:
        return {
            "enabled": route.enabled,
            "base_url": route.base_url,
            "model": route.model,
            "route_id": route.route_id,
            "token_env": route.token_env,
            "token_configured": bool(os.environ.get(route.token_env)) if route.token_env else False,
            "api_key_configured": bool(route.api_key),
            "timeout_seconds": route.timeout_seconds,
            "dimensions": route.dimensions,
            "query_prefix": route.query_prefix,
            "reasoning_effort": route.reasoning_effort,
            "temperature": route.temperature,
            "top_p": route.top_p,
            "max_output_tokens": route.max_output_tokens,
        }

    def _write_config(self, config: GatewayConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = [
            "# Managed by Fxi Novel. Direct API keys are stored locally in this gitignored file.",
            self._route_table("chat", config.chat),
        ]
        content.extend(
            self._route_table("chat_fallbacks", route, array=True)
            for route in config.chat_fallbacks
        )
        content.append(self._route_table("embedding", config.embedding))
        if config.knowledge_query is not None:
            content.append(self._route_table("knowledge_query", config.knowledge_query))
        if config.creative is not None:
            content.append(self._route_table("creative", config.creative))
        serialized = "\n\n".join(content) + "\n"

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _route_table(name: str, route: ModelRoute, *, array: bool = False) -> str:
        heading = f"[[{name}]]" if array else f"[{name}]"
        values = (
            ("enabled", "true" if route.enabled else "false"),
            ("base_url", json.dumps(route.base_url, ensure_ascii=False)),
            ("model", json.dumps(route.model, ensure_ascii=False)),
            ("route_id", json.dumps(route.route_id, ensure_ascii=False)),
            ("token_env", json.dumps(route.token_env, ensure_ascii=False)),
            ("timeout_seconds", repr(route.timeout_seconds)),
            ("dimensions", str(route.dimensions)),
            ("query_prefix", json.dumps(route.query_prefix, ensure_ascii=False)),
        )
        if route.reasoning_effort is not None:
            values += (("reasoning_effort", json.dumps(route.reasoning_effort)),)
        for name in ("temperature", "top_p", "max_output_tokens"):
            value = getattr(route, name)
            if value is not None:
                values += ((name, json.dumps(value, allow_nan=False)),)
        if route.api_key:
            values += (("api_key", json.dumps(route.api_key, ensure_ascii=False)),)
        return "\n".join([heading, *(f"{key} = {value}" for key, value in values)])
