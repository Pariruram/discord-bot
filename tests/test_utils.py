from bot.utils import as_bool, safe_int, split_message


def test_empty_message() -> None:
    assert split_message("") == []


def test_short_message_is_unchanged() -> None:
    assert split_message("hello", max_len=100) == ["hello"]


def test_long_line_is_split_within_limit() -> None:
    text = "x" * 1000
    chunks = split_message(text, max_len=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == text


def test_code_fences_are_balanced() -> None:
    body = "\n".join(f"line {index}" for index in range(200))
    text = f"intro\n```python\n{body}\n```\noutro"
    chunks = split_message(text, max_len=300)

    assert len(chunks) > 1
    assert all(len(chunk) <= 304 for chunk in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_safe_int_falls_back_and_clamps() -> None:
    assert safe_int("42", 1) == 42
    assert safe_int("oops", 7) == 7
    assert safe_int("999", 1, maximum=10) == 10
    assert safe_int("-5", 1, minimum=0) == 0


def test_as_bool() -> None:
    assert as_bool("true")
    assert as_bool("1")
    assert not as_bool("false")
    assert as_bool(None, default=True)
