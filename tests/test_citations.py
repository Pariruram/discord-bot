from bot.citations import Citation, format_sources


def test_empty_citations() -> None:
    assert format_sources("answer", []) == ""


def test_sources_footer_is_appended() -> None:
    footer = format_sources(
        "answer",
        [Citation(title="Example", url="https://example.com/a")],
    )
    assert footer == "\n\n**Источники:**\n- [Example](https://example.com/a)"


def test_duplicate_urls_are_collapsed() -> None:
    footer = format_sources(
        "answer",
        [
            Citation(title="One", url="https://example.com/a"),
            Citation(title="One again", url="https://example.com/a"),
        ],
    )
    assert footer.count("https://example.com/a") == 1


def test_urls_already_in_reply_are_skipped() -> None:
    footer = format_sources(
        "see https://example.com/a",
        [
            Citation(title="One", url="https://example.com/a"),
            Citation(title="Two", url="https://example.com/b"),
        ],
    )
    assert "https://example.com/a" not in footer
    assert "https://example.com/b" in footer


def test_all_urls_already_in_reply_gives_no_footer() -> None:
    footer = format_sources(
        "see https://example.com/a",
        [Citation(title="One", url="https://example.com/a")],
    )
    assert footer == ""


def test_limit_is_applied() -> None:
    citations = [
        Citation(title=f"Source {index}", url=f"https://example.com/{index}")
        for index in range(10)
    ]
    footer = format_sources("answer", citations, limit=3)
    assert footer.count("- [") == 3


def test_invalid_urls_are_ignored() -> None:
    footer = format_sources(
        "answer",
        [
            Citation(title="FTP", url="ftp://example.com/file"),
            Citation(title="Relative", url="/local"),
            Citation(title="OK", url="https://example.com"),
        ],
    )
    assert "ftp" not in footer
    assert "https://example.com" in footer


def test_empty_title_falls_back_to_url() -> None:
    footer = format_sources("answer", [Citation(title="", url="https://example.com")])
    assert "- [https://example.com](https://example.com)" in footer


def test_brackets_in_title_fall_back_to_url() -> None:
    footer = format_sources(
        "answer",
        [Citation(title="[broken]", url="https://example.com")],
    )
    assert "- [https://example.com](https://example.com)" in footer


def test_urls_with_parentheses_use_bare_link() -> None:
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"
    footer = format_sources(
        "answer",
        [Citation(title="Foo", url=url)],
    )
    assert f"- Foo: {url}" in footer
    assert f"[Foo]({url})" not in footer
