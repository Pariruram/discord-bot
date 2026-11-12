# AGENTS.md

Этот файл — карта проекта для кодовых агентов. Пользовательская документация — `README.md`.

## Проект

Discord-бот: Python 3.11, discord.py 2.x, OpenAI-совместимые LLM,
Supabase (память и `bot_settings`), Render free (512 MB, один процесс, без workers).
Основная модель — `gemini-3.5-flash-lite`; free-tier — около 15 запросов в минуту.

## Команды

- `python -m pytest -q`
- `python -m pytest tests/test_<area>.py -q`
- `python -m ruff check .`
- `python -m compileall -q discord_bot main.py tests`

Pytest: `asyncio_mode=auto`, тесты без сети и Discord. Все внешние вызовы — через fakes.

## Жёсткие правила

- Новые env-переменные — только если без них нельзя. Настройки — в `bot_settings` (`!set`).
- Миграции Supabase — только при реальной необходимости.
- Render free: 512 MB, без отдельных workers. Фоновые задачи — `MemoryWorker` внутри процесса.
- Один инстанс: heartbeat-ожидание 180 секунд и persistent login-gate. Не делать быстрые деплои подряд.
- Free-tier Gemini: без плотных retry. `503`/`429` не долбить; worker — 1 job/pass и интервал.
- Tool schemas и `DESCRIPTION` — единственный контракт для модели. Не дублировать их в отдельных md.
- После изменения поведения обновлять README/help и тесты.
- Перед коммитом: полный pytest, ruff, compileall, `git diff --check`.

## Карта

- `main.py` — конфиг, репозитории, registry, миграции, login-gate, health.
- `discord_app/client.py` — события; `on_message`, `!help`, `/help`.
- `discord_app/chat.py` — маркеры, ingest, контекст, tool loop, файлы, debug, prefetch.
- `llm/client.py` — запросы, таймауты, purpose/request-id логи.
- `llm/tool_loop.py` — раунды инструментов.
- `memory/` — Supabase repository, service, worker, context, extract.
- `tools/` — автоподключение через `create_tool()`; `base.py` — Tool/ToolContext/Registry.
- `palettes.py` — палитры в `bot_settings.pixel_palettes`.
- `assets/` — DejaVu fonts для resvg.

## Маркеры и файлы

- `/c` / `/clean` — чистый ответ; `/svg` — приложить SVG; `/pixel [16|32|64|128][:palette]`.
- Маркеры вырезаются до ingest.
- `ToolContext.files` — вложения ответа; максимум 3, порядок важен (128 первым).
- Reddit-ссылки: forced `read_thread`, `fetch_url` их не prefetch'ит.
- Запросы рисования: forced `draw_svg`; сырой SVG в чат не выводится.
- `read_thread` ограничен 500 комментариями / 80k символов; результат до 12k.
  При 503 — одна повторная попытка на часть, затем частичное резюме.

## Новый инструмент

1. `discord_bot/tools/foo.py` с `create_tool() -> Tool`.
2. Handler принимает `ToolContext`, возвращает строку, не бросает наружу.
3. Если нужен по умолчанию — добавить в `DEFAULT_ENABLED_TOOLS`.
4. Если у старых ботов есть явный `enabled_tools` — добавить одноразовую миграцию в `main.py`.
5. Для тяжёлых вызовов — `timeout`; для длинного результата — `max_result_chars`.
6. Тесты с fake LLM/fetch; обновить README/help.
7. Описание писать как промпт: когда использовать, когда нет, лимиты, формат.

## Состояние инструментов

- `fetch_url`: SSRF-защита, до 3 MB, текст до 6000; YouTube-субтитры.
- `read_thread`: Reddit RSS, `.json` отдаёт 403, адаптивное резюме.
- `draw_svg`: SVG через отдельный вызов, `defusedxml`, `resvg`; `pixel` — native SVG.
- `web_search`: Tavily, даты, 1–10 результатов.
- `current_time`: время/дата.

## Деплой

- push в `main` → Render deploy.
- `/healthz`: `waiting_previous` и `waiting_login` — нормальная защита, не ошибка.
- Не деплоить, пока `login_state=retrying`.
- Перед тестом дождаться `connected` и проверить `details.tools`.
- При 429 токен сбрасывается в Discord Portal; login-gate привязан к хешу токена.

## Ловушки

- `ToolRegistry` по умолчанию режет результат до 6000; для `read_thread` — 12k.
- `pixel_grid` только 16/32/64/128; первый PNG всегда 128×128, второй — нативная сетка.
- Палитры лежат в `bot_settings`, скрыты из `!settings`.
- SVG-исходник улучшения — только по `/svg` или явной просьбе.
- Ошибки worker: `503`/`429` → warning и retry через 5 минут, до 3 попыток.
- Не добавлять fallback-модели для рисования без явной просьбы.
