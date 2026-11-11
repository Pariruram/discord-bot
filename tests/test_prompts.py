from __future__ import annotations

from datetime import UTC, datetime

from discord_bot.prompts import DEFAULT_SYSTEM_PROMPT, build_system_prompt


def test_prompt_includes_factual_last_actions() -> None:
    prompt = build_system_prompt(
        "Ты — тестовый ассистент.",
        author_name="Аня",
        is_dm=True,
        last_actions=(
            "Модель: gemini/gemini-3.5-flash\n"
            '- web_search(query="ai bubble") → ок (1200 симв.)\n'
            "- источников в ответе: 3"
        ),
    )

    assert "<bot_last_actions>" in prompt
    assert "Твои ФАКТИЧЕСКИЕ действия" in prompt
    assert "НЕ выдумывай действий" in prompt
    assert "цитируй дословно" in prompt
    assert 'web_search(query="ai bubble") → ок (1200 симв.)' in prompt


def test_prompt_without_last_actions_has_no_block() -> None:
    prompt = build_system_prompt(
        "Ты — тестовый ассистент.",
        author_name="Аня",
        is_dm=True,
    )

    assert "<bot_last_actions>" not in prompt


def test_clean_prompt_replaces_identity_and_drops_persona() -> None:
    prompt = build_system_prompt(
        "Ты — саркастичный кот.",
        author_name="Аня",
        author_id="101",
        persona=["- стиль: сарказм"],
        facts=["любит Python"],
        clean=True,
    )

    assert "<identity>" in prompt
    assert "саркастичный кот" not in prompt
    assert "Режим «чистого ответа»" in prompt
    assert "не подмену" in prompt
    assert "внутренних процессах" in prompt
    assert "<persona" not in prompt
    # Контекст остаётся: факты — это знания, а не тон.
    assert '<known_facts about="Аня">' in prompt


def test_normal_prompt_keeps_the_identity_and_persona() -> None:
    prompt = build_system_prompt(
        "Ты — саркастичный кот.",
        author_name="Аня",
        author_id="101",
        persona=["- стиль: сарказм"],
        clean=False,
    )

    assert "саркастичный кот" in prompt
    assert "<persona" in prompt


def test_prompt_includes_current_date_and_temporal_rules() -> None:
    prompt = build_system_prompt(
        "Ты — тестовый ассистент.",
        author_name="Аня",
        is_dm=True,
        now=datetime(2026, 11, 11, 18, 25, tzinfo=UTC),
    )

    assert "<current_datetime>" in prompt
    assert "2026-11-11 18:25 UTC" in prompt
    assert "Не полагайся на дату из своих знаний" in prompt
    assert "time_range" in prompt


def test_prompt_keeps_identity_and_author_separate_from_transcript() -> None:
    prompt = build_system_prompt(
        "Ты — тестовый ассистент.",
        author_name="Аня",
        author_id="101",
        persona=["- стиль: коротко"],
        facts=["живёт в Москве"],
        summary="Раньше обсуждали Python.",
        channel_context="Вася: привет\nБот: здравствуй",
        is_dm=False,
    )

    assert "<identity>\nТы — тестовый ассистент.\n</identity>" in prompt
    assert "<addressing>" in prompt
    assert "Сейчас к тебе обращается Аня (id=101)." in prompt
    assert "Не приписывай ему чужие сообщения" in prompt
    assert '<persona user="Аня">' in prompt
    assert "- стиль: коротко" in prompt
    assert '<known_facts about="Аня">' in prompt
    assert "- живёт в Москве" in prompt
    assert "<memory_summary>" in prompt
    assert "Раньше обсуждали Python." in prompt
    assert '<channel_transcript note="' in prompt
    assert "Это НЕ твоя личная история" in prompt
    assert "Твои прошлые ответы помечены именем Бот" in prompt
    assert "Вася: привет\nБот: здравствуй" in prompt


def test_dm_prompt_uses_dialogue_wording_and_has_no_transcript() -> None:
    prompt = build_system_prompt(
        "",
        author_name="Аня",
        author_id="101",
        is_dm=True,
    )

    assert DEFAULT_SYSTEM_PROMPT in prompt
    assert "личном диалоге" in prompt
    assert "<channel_transcript" not in prompt


def test_empty_optional_sections_are_omitted() -> None:
    prompt = build_system_prompt("base", is_dm=True)

    assert "<addressing>" not in prompt
    assert "<persona" not in prompt
    assert "<known_facts" not in prompt
    assert "<memory_summary>" not in prompt
    assert "<channel_transcript" not in prompt


def test_empty_fact_and_persona_values_are_skipped() -> None:
    prompt = build_system_prompt(
        "base",
        persona=["   ", "- ok"],
        facts=["", "  ", "полезный факт"],
        is_dm=True,
    )

    assert "- ok" in prompt
    assert "полезный факт" in prompt
    assert "- \n" not in prompt
