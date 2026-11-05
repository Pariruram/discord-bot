"""Structured LLM errors with actionable user-facing messages."""

from __future__ import annotations

import json
import logging
import re
import uuid
from enum import StrEnum
from typing import Any

from bot.llm.catalog import suggest_model

logger = logging.getLogger(__name__)


class ErrorKind(StrEnum):
    model_not_found = "model_not_found"
    auth = "auth"
    rate_limit = "rate_limit"
    bad_request = "bad_request"
    provider_unavailable = "provider_unavailable"
    unknown = "unknown"


class LLMError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        status_code: int | None,
        kind: ErrorKind,
        detail: str,
        base_url: str = "",
        retry_after: int | None = None,
        suggestion: str | None = None,
        api_key_env: str = "",
        error_id: str | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.kind = kind
        self.detail = detail
        self.base_url = base_url
        self.retry_after = retry_after
        self.suggestion = suggestion
        self.api_key_env = api_key_env
        self.error_id = error_id
        super().__init__(self.__str__())

    def __str__(self) -> str:
        parts = [
            f"provider={self.provider}",
            f"model={self.model}",
            f"status={self.status_code}",
            f"kind={self.kind.value}",
        ]
        if self.base_url:
            parts.append(f"base_url={self.base_url}")
        if self.api_key_env:
            parts.append(f"api_key_env={self.api_key_env}")
        if self.retry_after is not None:
            parts.append(f"retry_after={self.retry_after}s")
        if self.suggestion:
            parts.append(f"suggestion={self.suggestion}")
        if self.error_id:
            parts.append(f"error_id={self.error_id}")
        parts.append(f"detail={self.detail}")
        return " ".join(parts)

    def user_message(self) -> str:
        if self.kind is ErrorKind.model_not_found:
            message = f"Модель `{self.provider}:{self.model}` не найдена."
            if self.suggestion:
                message += (
                    f" Возможно, вы имели в виду "
                    f"`{self.provider}:{self.suggestion}`."
                )
            return message + " Выбрать другую: `!models`."

        if self.kind is ErrorKind.auth:
            message = f"Проблема с API-ключом провайдера `{self.provider}`."
            if self.api_key_env:
                message += f" Проверьте `{self.api_key_env}`."
            return message + " Сообщите владельцу."

        if self.kind is ErrorKind.rate_limit:
            message = f"Лимит запросов у `{self.provider}` исчерпан."
            if self.retry_after:
                message += f" Попробуйте через {self.retry_after} сек."
            else:
                message += " Попробуйте позже."
            return message + " Или выберите другую модель: `!models`."

        if self.kind is ErrorKind.bad_request:
            return (
                f"Провайдер `{self.provider}` отклонил запрос. Возможно, модель "
                f"`{self.model}` не поддерживает инструменты или параметры. "
                "Попробуйте другую: `!models`."
            )

        if self.kind is ErrorKind.provider_unavailable:
            return (
                f"Провайдер `{self.provider}` временно недоступен. "
                "Попробуйте позже."
            )

        identifier = self.error_id or uuid.uuid4().hex[:8]
        return (
            f"Не удалось получить ответ от `{self.provider}:{self.model}`. "
            f"Попробуйте позже. (код: {identifier})"
        )


_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|key|token|authorization|bearer)"
    r"(\s*[=:]\s*|\s+)[^\s,&;\"']+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def sanitize_detail(value: Any, limit: int = 800) -> str:
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    elif value is None:
        text = ""
    else:
        text = str(value)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _SECRET_RE.sub(r"\1=<redacted>", text)
    text = _SK_RE.sub("sk-<redacted>", text)
    text = " ".join(text.split())
    return text[:limit]


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _detail(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if body:
        return sanitize_detail(body)
    response = getattr(exc, "response", None)
    text = getattr(response, "text", None) if response is not None else None
    return sanitize_detail(text or str(exc))


def _retry_after(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    value = getattr(exc, "retry_after", None)
    if value is None and headers is not None:
        try:
            value = headers.get("retry-after")
        except AttributeError:
            value = None
    if value is None:
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return type(exc).__name__ in {
        "APITimeoutError",
        "APIConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "RemoteProtocolError",
    }


def build_llm_error(
    exc: Exception,
    *,
    provider: str,
    model: str,
    base_url: str = "",
    api_key_env: str = "",
) -> LLMError:
    status = _status_code(exc)
    detail = _detail(exc)
    retry_after = _retry_after(exc)

    if status == 404:
        kind = ErrorKind.model_not_found
    elif status in {401, 403}:
        kind = ErrorKind.auth
    elif status == 429:
        kind = ErrorKind.rate_limit
    elif status in {400, 409, 422}:
        kind = ErrorKind.bad_request
    elif status is not None and status >= 500:
        kind = ErrorKind.provider_unavailable
    elif _is_transient(exc):
        kind = ErrorKind.provider_unavailable
    else:
        kind = ErrorKind.unknown

    suggestion = suggest_model(provider, model) if kind is ErrorKind.model_not_found else None
    error_id = uuid.uuid4().hex[:8] if kind is ErrorKind.unknown else None

    return LLMError(
        provider=provider,
        model=model,
        status_code=status,
        kind=kind,
        detail=detail,
        base_url=base_url,
        retry_after=retry_after,
        suggestion=suggestion,
        api_key_env=api_key_env,
        error_id=error_id,
    )


def missing_api_key_error(
    provider: str,
    model: str,
    api_key_env: str,
) -> LLMError:
    return LLMError(
        provider=provider,
        model=model,
        status_code=None,
        kind=ErrorKind.auth,
        detail=f"Не задан {api_key_env or 'API-ключ'}",
        api_key_env=api_key_env,
    )
