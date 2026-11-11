"""Async OpenAI-compatible client with timeouts, retries and usage logging."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from discord_bot.llm.errors import (
    ErrorKind,
    LLMError,
    build_llm_error,
    missing_api_key_error,
    retry_after_seconds,
)
from discord_bot.llm.providers import ProviderSpec

logger = logging.getLogger(__name__)

__all__ = ["LLMClient", "LLMError"]

_RETRYABLE_STATUS = {408, 409, 500, 502, 503, 504}


class LLMClient:
    def __init__(self, *, request_timeout: float = 60.0, max_attempts: int = 3) -> None:
        self._request_timeout = request_timeout
        self._max_attempts = max(1, max_attempts)
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}

    def _get_client(self, spec: ProviderSpec, model: str) -> AsyncOpenAI:
        key = spec.api_key()
        if spec.requires_api_key and not key:
            error = missing_api_key_error(spec.name, model, spec.api_key_env)
            logger.error("LLM config error: %s", error)
            raise error

        cache_key = (spec.base_url, key or "")
        client = self._clients.get(cache_key)
        if client is None:
            client = AsyncOpenAI(
                api_key=key or "not-needed",
                base_url=spec.base_url,
                timeout=self._request_timeout,
                max_retries=0,
            )
            self._clients[cache_key] = client
        return client

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in _RETRYABLE_STATUS:
            return True
        return isinstance(exc, (APITimeoutError, APIConnectionError, asyncio.TimeoutError))

    async def complete(
        self,
        spec: ProviderSpec,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.75,
        max_tokens: int = 1200,
        tool_choice: str | dict[str, Any] | None = "auto",
        max_attempts: int | None = None,
    ) -> Any:
        client = self._get_client(spec, model)
        attempts = self._max_attempts if max_attempts is None else max(1, max_attempts)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            if spec.supports_tool_choice and tool_choice:
                kwargs["tool_choice"] = tool_choice

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = await client.chat.completions.create(**kwargs)
                elapsed_ms = (time.perf_counter() - started) * 1000
                usage = getattr(response, "usage", None)
                logger.info(
                    "LLM %s/%s %.0fms in=%s out=%s",
                    spec.name,
                    model,
                    elapsed_ms,
                    getattr(usage, "prompt_tokens", "?"),
                    getattr(usage, "completion_tokens", "?"),
                )
                return response
            except Exception as exc:  # noqa: BLE001 - provider SDK raises many types
                last_error = exc
                if attempt >= attempts or not self._is_retryable(exc):
                    if getattr(exc, "status_code", None) == 429:
                        logger.warning(
                            "LLM %s/%s rate limited, retry after %ss",
                            spec.name,
                            model,
                            retry_after_seconds(exc) or "?",
                        )
                    break
                delay = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.3)
                logger.warning(
                    "LLM %s/%s попытка %s/%s не удалась (%s), повтор через %.1fs",
                    spec.name,
                    model,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        if last_error is None:  # pragma: no cover - defensive, loop always runs
            last_error = RuntimeError("LLM request failed without an exception")

        error = build_llm_error(
            last_error,
            provider=spec.name,
            model=model,
            base_url=spec.base_url,
            api_key_env=spec.api_key_env,
        )
        if error.kind in {
            ErrorKind.model_not_found,
            ErrorKind.auth,
            ErrorKind.bad_request,
        }:
            logger.error("LLM error: %s", error)
        elif error.kind is ErrorKind.unknown:
            logger.error("LLM error: %s", error, exc_info=last_error)
        else:
            logger.warning("LLM error: %s", error)
        raise error from last_error

    async def complete_text(
        self,
        spec: ProviderSpec,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        response = await self.complete(spec, model, messages, **kwargs)
        return (response.choices[0].message.content or "").strip()

    async def aclose(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                logger.debug("Ошибка закрытия LLM-клиента", exc_info=True)
        self._clients.clear()
