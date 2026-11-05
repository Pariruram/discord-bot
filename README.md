# Discord AI Bot

Discord-бот с LLM-чатом, историей пользователя, контекстом канала,
веб-поиском и подключаемыми инструментами.

## Возможности

- ответы в ЛС и по упоминанию в каналах;
- история диалога и профиль предпочтений в Supabase;
- краткосрочная память канала с периодическим сжатием в резюме;
- смена LLM-провайдера и модели командами, без рестарта;
- `!models` — кнопки с free-моделями, доступна всем;
- произвольные OpenAI-совместимые провайдеры;
- инструменты (tools) с включением/отключением из чата;
- автоматические ссылки на источники при веб-поиске;
- понятные ошибки LLM с подсказками по модели и ключу;
- health-check сервер для Render / Hugging Face Spaces.

## Структура

```
bot/
  main.py              запуск приложения
  config.py            env и валидация
  services.py          контейнер зависимостей
  health.py            Flask health-check
  prompts.py           system prompt
  utils.py             split_message и парсинг
  llm/
    providers.py       встроенные и кастомные провайдеры
    models.py          разрешение модели + backward compatibility
    catalog.py         free-каталог для !models и подсказки моделей
    model_switcher.py  глобальное переключение модели
    errors.py          классификация ошибок LLM
    client.py          AsyncOpenAI, таймауты, ретраи
    tool_loop.py       безопасный multi-turn tool loop
    utility.py         саммари и извлечение предпочтений
  tools/
    base.py            Tool, ToolContext, ToolRegistry
    web_search.py      Tavily
    current_time.py    текущее время (пример инструмента)
  storage/
    repository.py      Supabase через asyncio.to_thread
    cache.py           TTL-кэш настроек
    channel_memory.py  память каналов
  discord_app/
    bot.py             клиент и события
    chat.py            пайплайн сообщения
    cogs/admin.py      owner-команды
    cogs/models.py     !models с кнопками free-моделей
```

## Установка

Требуется Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env
```

Заполни `.env`:

| Переменная | Обязательно | Назначение |
|---|---|---|
| `DISCORD_TOKEN` | да | токен бота |
| `SUPABASE_URL` | да | URL проекта Supabase |
| `SUPABASE_KEY` | да | service/anon key с доступом к таблицам |
| `GEMINI_API_KEY` | для Gemini | ключ провайдера |
| `GROQ_API_KEY` | для Groq | ключ провайдера |
| `OPENAI_API_KEY` | для OpenAI | ключ провайдера |
| `OPENROUTER_API_KEY` | для OpenRouter | ключ провайдера |
| `TAVILY_API_KEY` | для веб-поиска | ключ Tavily |
| `PORT` | нет | порт health-сервера (по умолчанию 7860) |
| `ENABLE_HEALTH` | нет | `false` отключает health-сервер |
| `LOG_LEVEL` | нет | уровень логирования |

Запуск:

```bash
python bot.py
```

## Таблицы Supabase

Используются те же таблицы, что и раньше:

- `bot_settings(key, value, updated_at)`
- `user_profiles(user_id, preferences, updated_at)`
- `chat_history(user_id, role, content, created_at)`
- `channel_context(channel_id, summary, messages, updated_at)`

Для производительности желательно иметь индексы:

```sql
create index if not exists chat_history_user_created_idx
  on chat_history (user_id, created_at desc);
```

## Команды

### Для всех

| Команда | Описание |
|---|---|
| `!models` | кнопки с доступными free-моделями; выбор глобальный для всех |

`!models` показывает только free-модели, для которых у провайдера настроен
API-ключ (или ключ не требуется). Дубликаты убираются. Выбор меняет модель
**для всех участников** — это удобно для небольшого чата.

### Только ЛС владельца

| Команда | Описание |
|---|---|
| `!owners` | список владельцев |
| `!owner add <id>` / `!owner remove <id>` | управление владельцами |
| `!settings` | все настройки |
| `!set <key> <value>` | изменить настройку |
| `!system [set <text>\|reset]` | system prompt |
| `!model` | текущая модель и провайдеры |
| `!model provider:model` | глобально переключить провайдера и модель |
| `!model model` | сменить модель у текущего провайдера |
| `!provider list` | список провайдеров |
| `!provider add <name> <base_url> <KEY_ENV\|-> <model> [utility_model]` | добавить OpenAI-совместимый провайдер |
| `!provider remove <name>` | удалить кастомный провайдер |
| `!tools` | список инструментов |
| `!tools enable <name>` / `!tools disable <name>` | включить/отключить инструмент |

Примеры:

```
!models
!model groq:openai/gpt-oss-120b
!model gemini:gemini-3.8-flash
!provider add local http://localhost:11434/v1 - llama3.1
!model local:llama3.1
```

`chat_model` — основная модель для ответов и tool-calling.
`utility_model` — быстрая модель для саммари каналов и извлечения предпочтений;
она должна принадлежать тому же провайдеру, что и `chat_model`.

При смене модели через `!models` обе настройки обновляются автоматически.
`!model` у владельца остаётся аварийным способом поставить произвольную модель.

Владельцы хранятся в `bot_settings.owners` (JSON-список). Первого владельца
нужно добавить вручную в БД.

По умолчанию автоизвлечение предпочтений выключено (`auto_preferences=false`),
чтобы не делать лишний LLM-вызов на каждое сообщение. Включить:
`!set auto_preferences true`. Инструменты по умолчанию: `web_search`.

При веб-поиске бот добавляет в ответ блок **«Источники»** с реальными URL из
результатов Tavily. Ссылки, которые модель уже указала в тексте, повторно не
добавляются; embed-превью подавляются.

## Ошибки модели

Ошибки провайдеров классифицируются:

- `404` — модель не найдена; бот подсказывает ближайший ID и предлагает `!models`;
- `401/403` — проблема с API-ключом;
- `429` — исчерпан лимит free-тарифа, показывается `Retry-After`;
- `400/422` — провайдер отклонил запрос (например, модель не поддерживает tools);
- `5xx`/timeout — провайдер временно недоступен;
- неизвестные — короткий `error_id` для поиска по логам.

В консоль пишутся провайдер, модель, статус, тип ошибки, `base_url` и
безопасно обрезанное описание без API-ключей.

## Как добавить инструмент

1. Создай модуль в `bot/tools/`, например `weather.py`.
2. Экспортируй `create_tool()`, возвращающий `Tool`:

```python
from bot.tools.base import Tool, ToolContext

def create_tool() -> Tool:
    async def handler(context: ToolContext, city: str) -> str:
        return f"Погода в {city}: ..."

    return Tool(
        name="weather",
        description="Возвращает погоду в городе.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        handler=handler,
    )
```

3. Перезапусти бота — модуль загрузится автоматически.
4. Включи его командой `!tools enable weather`.

## Проверка

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
```

Тесты не требуют сети, Discord или Supabase: storage и LLM подменяются
лёгкими фейками.

## Деплой

`bot.py` остаётся точкой входа, поэтому текущая команда запуска на Render /
Hugging Face продолжает работать. Health-сервер слушает `0.0.0.0:$PORT`.
