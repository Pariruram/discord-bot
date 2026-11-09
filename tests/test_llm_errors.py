from discord_bot.llm.errors import (
    ErrorKind,
    build_llm_error,
    missing_api_key_error,
)


class FakeResponse:
    def __init__(self, status: int, text: str = "", headers=None) -> None:
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class FakeProviderError(Exception):
    def __init__(self, status: int, body=None, text: str = "", headers=None) -> None:
        self.status_code = status
        self.body = body
        self.response = FakeResponse(status, text=text, headers=headers)


def make_error(exc: Exception, model: str = "gemini-3.5-lite"):
    return build_llm_error(
        exc,
        provider="gemini",
        model=model,
        base_url="https://gemini.example/v1",
        api_key_env="GEMINI_API_KEY",
    )


def test_404_is_model_not_found_with_suggestion() -> None:
    error = make_error(FakeProviderError(404, body={"error": "not found"}))

    assert error.kind is ErrorKind.model_not_found
    assert error.status_code == 404
    assert error.suggestion == "gemini-3.5-flash-lite"
    assert "gemini-3.5-flash-lite" in error.user_message()
    assert "!models" in error.user_message()
    assert "status=404" in str(error)


def test_401_is_auth_and_names_env_variable() -> None:
    error = make_error(FakeProviderError(401))

    assert error.kind is ErrorKind.auth
    assert "GEMINI_API_KEY" in error.user_message()
    assert "Сообщите владельцу" in error.user_message()


def test_429_has_retry_after() -> None:
    error = make_error(
        FakeProviderError(429, headers={"retry-after": "30"})
    )

    assert error.kind is ErrorKind.rate_limit
    assert error.retry_after == 30
    assert "30 сек" in error.user_message()


def test_400_is_bad_request() -> None:
    error = make_error(FakeProviderError(400, body={"error": "bad tools"}))
    assert error.kind is ErrorKind.bad_request
    assert "инструменты" in error.user_message()


def test_503_is_provider_unavailable() -> None:
    error = make_error(FakeProviderError(503))
    assert error.kind is ErrorKind.provider_unavailable
    assert "временно недоступен" in error.user_message()


def test_timeout_is_provider_unavailable() -> None:
    error = make_error(TimeoutError("timed out"))
    assert error.kind is ErrorKind.provider_unavailable


def test_unknown_error_has_error_id() -> None:
    error = make_error(ValueError("something strange"), model="some-model")
    assert error.kind is ErrorKind.unknown
    assert error.error_id
    assert error.error_id in error.user_message()


def test_secrets_are_redacted() -> None:
    error = make_error(
        FakeProviderError(
            500,
            text="request failed key=secret123&x=1 Bearer sk-abcdefghijkl",
        )
    )
    assert "secret123" not in error.detail
    assert "sk-abcdefghijkl" not in error.detail
    assert "<redacted>" in error.detail


def test_bearer_token_is_redacted() -> None:
    error = make_error(
        FakeProviderError(500, text="Authorization: Bearer abc.def.ghi")
    )
    assert "abc.def.ghi" not in error.detail
    assert "<redacted>" in error.detail


def test_missing_api_key_error() -> None:
    error = missing_api_key_error("gemini", "gemini-3.8-flash", "GEMINI_API_KEY")
    assert error.kind is ErrorKind.auth
    assert "GEMINI_API_KEY" in error.user_message()


def test_dictionary_body_is_serialized() -> None:
    error = make_error(FakeProviderError(400, body={"message": "invalid model"}))
    assert "invalid model" in error.detail


def test_retry_after_from_exception_attribute() -> None:
    exc = FakeProviderError(429)
    exc.retry_after = "12"
    error = make_error(exc)
    assert error.retry_after == 12


def test_retry_after_from_provider_message() -> None:
    exc = FakeProviderError(429)
    exc.args = ("Quota exceeded. Please retry in 43.2s.",)

    error = make_error(exc)

    assert error.retry_after == 43
