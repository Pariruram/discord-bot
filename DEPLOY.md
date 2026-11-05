# Deploy checklist after the rename

Что изменилось в репозитории:

- пакет `bot/` → `discord_bot/`;
- корневой `bot.py` → `main.py`;
- внутренний `discord_bot/discord_app/bot.py` → `discord_bot/discord_app/client.py`;
- `python -m bot` → `python -m discord_bot`;
- все импорты `bot.*` → `discord_bot.*`;
- Dockerfile, `render.yaml`, CI и Makefile обновлены;
- схема Supabase и переменные окружения **не изменились**.

## 1. Render

Открыть сервис → Settings:

- [ ] **Start Command**: заменить `python bot.py` на `python main.py`.
- [ ] **Build Command**: оставить `pip install -r requirements.txt`.
- [ ] **Health Check Path**: `/healthz` (или оставить `/`).
- [ ] **Python version**: `3.11.9` через `.python-version` или env
      `PYTHON_VERSION=3.11.9`.
- [ ] **Instance count**: `1`.
- [ ] **Auto-Deploy**: включить, если используется Git-деплой.
- [ ] **PORT** не задавать вручную — Render передаёт его сам.

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

- [ ] URL монитора: `https://<service>.onrender.com/healthz`.
- [ ] Интервал: 5 минут.
- [ ] Ожидаемый код: 200.
- [ ] Алерты настроены.

Менять URL не обязательно, если уже стоит `/`; `/` продолжает работать.

## 4. Supabase

- [ ] Миграции **не нужны**.
- [ ] Таблицы `bot_settings`, `user_profiles`, `chat_history`,
      `channel_context` остаются прежними.
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

- [ ] В логах есть `Запуск бота` и `Бот ... запущен`.
- [ ] `/healthz` отвечает `{"status": "ok"}`.
- [ ] `!models` показывает кнопки free-моделей.
- [ ] Выбор модели пишет публичное `✅ Выбранная модель: ...`.
- [ ] `!settings` открывается в ЛС владельца.
- [ ] Обычный ответ в ЛС или по упоминанию работает.
- [ ] При ошибке модели в логах есть `provider=`, `model=`, `status=`,
      `kind=`, `base_url=`.

## Откат

Если нужно вернуться:

- Render start command: `python bot.py`;
- вернуть пакет `bot/` и файл `bot.py` из предыдущего коммита;
- вернуть импорты `discord_bot.*` → `bot.*`;
- вернуть Dockerfile/CI/Makefile.

Supabase и env-переменные при откате менять не нужно.
