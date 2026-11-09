# Deploy checklist

Что изменилось в репозитории:

- пакет `bot/` → `discord_bot/`;
- корневой `bot.py` → `main.py`;
- внутренний `discord_bot/discord_app/bot.py` → `discord_bot/discord_app/client.py`;
- `python -m bot` → `python -m discord_bot`;
- все импорты `bot.*` → `discord_bot.*`;
- новая память: `supabase/migrations/20261108000100_memory.sql`, пакет
  `discord_bot/memory/`, команды `!memory` и `!forget`;
- Dockerfile, `render.yaml`, CI и Makefile обновлены;
- переменные окружения **не изменились**; поведение памяти настраивается через
  `bot_settings`.

## 0. Git после перезаписи истории

История ветки `main` перезаписана, поэтому обычный push будет отклонён.

- [ ] Сохранить safety-ветку: `git push -u origin backup/pre-message-rewrite`.
- [ ] Отправить переписанный `main`:
      `git push --force-with-lease origin main`.
- [ ] Убедиться, что GitHub показывает новые commit messages.
- [ ] Не делать force-push, пока не проверен деплой на Render.

Локальные safety-ветки:

- `backup/pre-message-rewrite` — история до перезаписи сообщений;
- `production-ready` — коммит с переименованием и deploy-файлами.

## 1. Render

Открыть сервис → Settings:

- [ ] **Start Command**: `python main.py`.
- [ ] **Build Command**: оставить `pip install -r requirements.txt`.
- [ ] **Health Check Path**: `/healthz`.
- [ ] **Python version**: `3.11.9` через `.python-version` или env
      `PYTHON_VERSION=3.11.9`.
- [ ] **Instance count**: `1`.
- [ ] **Auto-Deploy**: включить, если используется Git-деплой.
- [ ] **PORT** не задавать вручную — Render передаёт его сам.
- [ ] **Max Shutdown Delay**: `30` секунд (уже в `render.yaml`).

Переменные окружения остаются прежними:

- [ ] `DISCORD_TOKEN`
- [ ] `SUPABASE_URL`
- [ ] `SUPABASE_KEY`
- [ ] ключи нужных LLM-провайдеров: `GEMINI_API_KEY`, `GROQ_API_KEY`,
      `OPENROUTER_API_KEY`, `OPENAI_API_KEY`
- [ ] `TAVILY_API_KEY`

Опционально:

- [ ] `ENABLE_HEALTH=true`
- [ ] `LOG_LEVEL=INFO`

Если используется Blueprint:

- [ ] Обновить/подключить `render.yaml` — в нём уже
      `startCommand: python main.py`.
- [ ] Секреты с `sync: false` заполнить в Dashboard.

## 2. Docker / Hugging Face Spaces

Если используется свой Dockerfile, обновить:

- [ ] `COPY bot ./bot` → `COPY discord_bot ./discord_bot`.
- [ ] `COPY bot.py ./` → `COPY main.py ./`.
- [ ] `CMD ["python", "bot.py"]` → `CMD ["python", "main.py"]`.

Готовый `Dockerfile` в репозитории уже обновлён:

```bash
docker build -t discord-ai-bot .
docker run --env-file .env -p 7860:7860 discord-ai-bot
```

- [ ] Проверить healthcheck: `http://127.0.0.1:$PORT/healthz`.
- [ ] Если `PORT` переопределяется, передать его через `-e PORT=...`.

Для Hugging Face Spaces:

- [ ] Обновить команду запуска/`app_file` на `main.py`.
- [ ] Пересобрать Space.
- [ ] Проверить, что health-сервер слушает `$PORT`.

## 3. UptimeRobot

- [ ] URL монитора: `https://discord-bot-nmc8.onrender.com/healthz`.
- [ ] Интервал: 5 минут.
- [ ] Ожидаемый код: 200.
- [ ] Алерты настроены.

`/` тоже отвечает 200, но `/healthz` возвращает явный JSON и лучше подходит для
проверки.

## 4. Supabase

- [ ] Открыть SQL Editor и применить
      `supabase/migrations/20261108000100_memory.sql`.
- [ ] Если схема уже применялась до фиксов, применить
      `20261108000200_fix_ingest_message.sql` и
      `20261108000300_fix_history_before.sql` (они заменяют функции и
      перезагружают кэш PostgREST).
- [ ] Проверить, что появились таблицы `users`, `user_personas`, `user_facts`,
      `conversations`, `conversation_sessions`, `conversation_summaries`,
      `messages`, `memory_jobs`.
- [ ] Проверить, что старые `user_profiles`, `chat_history`, `channel_context`
      остались на месте. Код их больше не использует; удалять можно вручную
      после успешной проверки.
- [ ] Убедиться, что используется ключ с доступом к новым таблицам
      (`service_role` подходит; RLS в миграции не включается).
- [ ] Задать два канала памяти:
      `!set allowed_channel_ids <id1>,<id2>`.
- [ ] При необходимости изменить `session_gap_minutes`,
      `memory_retention_days`, `max_facts_per_user`, `max_context_facts`,
      `max_history`, `worker_poll_seconds`, `worker_llm_interval_seconds`,
      `catch_up_limit` через `!set`.
- [ ] Старые ключи настроек (`provider`, `model`, `gemini_model`,
      `fast_model` и т.д.) продолжают работать.
- [ ] `!models` при первом использовании обновит `provider`, `chat_model`,
      `utility_model` на выбранную free-модель.

## 5. Локально / CI

- [ ] `pip install -r requirements.txt -r requirements-dev.txt`.
- [ ] `python main.py` или `python -m discord_bot`.
- [ ] `pytest -q`.
- [ ] `ruff check .`.
- [ ] `python -m compileall -q discord_bot main.py`.

CI в `.github/workflows/ci.yml` уже обновлён на новые пути.

## 6. Внешние скрипты

Если что-то импортирует пакет напрямую:

- [ ] `from bot...` → `from discord_bot...`.
- [ ] `python -m bot` → `python -m discord_bot`.
- [ ] `python bot.py` → `python main.py`.
- [ ] Любые ссылки на `bot/` в деплой-скриптах → `discord_bot/`.

## 7. Проверка после деплоя

- [ ] В логах есть `Запуск бота`, `Бот ... запущен` и `Memory worker запущен`.
- [ ] `/healthz` отвечает `{"status": "ok"}`.
- [ ] `!models` показывает кнопки free-моделей.
- [ ] Выбор модели пишет публичное `✅ Выбранная модель: ...`.
- [ ] `!settings` открывается в ЛС владельца.
- [ ] Обычный ответ в ЛС или по упоминанию работает.
- [ ] В ЛС `!memory` показывает факты, `!memory off` и `!memory on` работают.
- [ ] `!forget all` удаляет факты, после этого `!memory` показывает 0.
- [ ] В канале из `allowed_channel_ids` бот помнит контекст; в каналах вне
      списка не отвечает и не запоминает; ЛС работают всегда.
- [ ] После перезапуска Render в логах есть catch-up или его отсутствие, но
      бот не дублирует ответы.
- [ ] При ошибке модели в логах есть `provider=`, `model=`, `status=`,
      `kind=`, `base_url=`.

## Откат

Если нужно вернуться к старому коду:

- Render start command: `python bot.py`;
- вернуть пакет `bot/` и файл `bot.py` из предыдущего коммита;
- вернуть импорты `discord_bot.*` → `bot.*`;
- вернуть Dockerfile/CI/Makefile.

Новые таблицы памяти можно оставить: старый код их не использует. Env-переменные
при откате менять не нужно.
