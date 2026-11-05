import os
import json
import asyncio
import logging
from datetime import datetime

import discord
from discord.ext import commands
from groq import Groq
from supabase import create_client, Client
from dotenv import load_dotenv
from flask import Flask
import threading

load_dotenv()

# === Только секреты из .env ===
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OWNER_ID = os.getenv("OWNER_ID")

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True
)
logger = logging.getLogger("bot")

# === Flask для Render ===
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

threading.Thread(target=run_flask, daemon=True).start()

# === Discord + Groq + Supabase ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

groq_client = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ====================== РАБОТА С НАСТРОЙКАМИ ======================

def get_setting(key: str, default: str = "") -> str:
    try:
        res = supabase.table("bot_settings").select("value").eq("key", key).execute()
        if res.data:
            return res.data[0]["value"]
    except Exception as e:
        logger.error(f"get_setting({key}): {e}")
    return default

def set_setting(key: str, value: str):
    try:
        supabase.table("bot_settings").upsert({
            "key": key,
            "value": value,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"set_setting({key}): {e}")

def get_all_settings() -> dict:
    try:
        res = supabase.table("bot_settings").select("key, value").execute()
        return {row["key"]: row["value"] for row in res.data}
    except Exception as e:
        logger.error(f"get_all_settings: {e}")
        return {}

# ====================== ПРОФИЛИ ПОЛЬЗОВАТЕЛЕЙ ======================

def get_user_profile(user_id: str) -> dict:
    try:
        res = supabase.table("user_profiles").select("*").eq("user_id", user_id).execute()
        if res.data:
            return res.data[0]
        supabase.table("user_profiles").insert({
            "user_id": user_id,
            "preferences": {}
        }).execute()
        return {"user_id": user_id, "preferences": {}}
    except Exception as e:
        logger.error(f"get_user_profile: {e}")
        return {"user_id": user_id, "preferences": {}}

def update_preferences(user_id: str, prefs: dict):
    try:
        supabase.table("user_profiles").upsert({
            "user_id": user_id,
            "preferences": prefs,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"update_preferences: {e}")

# ====================== ИСТОРИЯ ======================

def fetch_recent_history(user_id: str, limit: int) -> list:
    try:
        res = (
            supabase.table("chat_history")
            .select("role, content")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        history = [{"role": r["role"], "content": r["content"]} for r in res.data]
        return list(reversed(history))
    except Exception as e:
        logger.error(f"fetch_recent_history: {e}")
        return []

def save_message(user_id: str, role: str, content: str):
    try:
        supabase.table("chat_history").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        logger.error(f"save_message: {e}")

# ====================== ПРЕДПОЧТЕНИЯ СТИЛЯ ======================

def extract_preferences(user_message: str, current_prefs: dict, fast_model: str) -> dict:
    prompt = f"""
Ты — анализатор предпочтений стиля общения.
Текущие предпочтения: {json.dumps(current_prefs, ensure_ascii=False)}

Сообщение пользователя: "{user_message}"

Если пользователь ЯВНО просит изменить стиль, формат, тон, длину или язык ответа — верни ТОЛЬКО новый JSON с обновлениями.
Если ничего не меняет — верни {{}}.

Примеры:
"пиши на бинарном" → {{"format": "binary"}}
"отвечай очень кратко" → {{"length": "very short"}}
"говори как пират" → {{"tone": "pirate"}}
"теперь пиши нормально" → {{"format": "normal", "tone": "neutral"}}

Только валидный JSON, без пояснений.
"""
    try:
        resp = groq_client.chat.completions.create(
            model=fast_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        text = resp.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        new_prefs = json.loads(text)
        if isinstance(new_prefs, dict) and new_prefs:
            return {**current_prefs, **new_prefs}
    except Exception as e:
        logger.error(f"extract_preferences: {e}")
    return current_prefs

def build_system_prompt(global_prompt: str, preferences: dict) -> str:
    if not preferences:
        return global_prompt
    rules = "\n".join(f"- {k}: {v}" for k, v in preferences.items())
    return f"""{global_prompt}

Строго соблюдай следующие предпочтения пользователя:
{rules}
"""

# ====================== РАЗБИЕНИЕ ДЛИННЫХ СООБЩЕНИЙ ======================

def split_message(text: str, max_len: int = 1900) -> list[str]:
    if len(text) <= max_len:
        return [text]

    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break

        split_at = text.rfind("\n\n", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            split_at = text.rfind("\n", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            split_at = text.rfind(". ", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len

        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()

    return parts

# ====================== КОМАНДЫ ВЛАДЕЛЬЦА ======================

def is_owner(ctx) -> bool:
    return str(ctx.author.id) == str(OWNER_ID)

@bot.command(name="settings")
async def settings_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return await ctx.send("Только в личных сообщениях.")
    if not is_owner(ctx):
        return await ctx.send("Нет доступа.")

    settings = get_all_settings()
    if not settings:
        return await ctx.send("Настройки пусты.")

    lines = [f"**{k}** = `{v}`" for k, v in settings.items()]
    await ctx.send("**Текущие настройки:**\n" + "\n".join(lines))

@bot.command(name="set")
async def set_cmd(ctx, key: str = None, *, value: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return await ctx.send("Только в личных сообщениях.")
    if not is_owner(ctx):
        return await ctx.send("Нет доступа.")

    allowed = {"model", "fast_model", "max_history", "max_message_length"}
    if key not in allowed:
        return await ctx.send(f"Доступные ключи: {', '.join(allowed)}")

    if not value:
        return await ctx.send(f"Использование: `!set {key} <значение>`")

    set_setting(key, value.strip())
    await ctx.send(f"`{key}` → `{value.strip()}`")

@bot.command(name="system")
async def system_cmd(ctx, *, arg: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return await ctx.send("Только в личных сообщениях.")
    if not is_owner(ctx):
        return await ctx.send("Нет доступа.")

    if not arg:
        current = get_setting("global_system_prompt")
        return await ctx.send(f"**Глобальный System Prompt:**\n```\n{current}\n```")

    if arg.lower().startswith("set "):
        new_prompt = arg[4:].strip()
        if not new_prompt:
            return await ctx.send("Укажи текст после `set`.")
        set_setting("global_system_prompt", new_prompt)
        return await ctx.send("Глобальный System Prompt обновлён.")

    if arg.lower() == "reset":
        set_setting("global_system_prompt", "Ты — полезный ассистент. Отвечай на языке пользователя.")
        return await ctx.send("System Prompt сброшен.")

    await ctx.send("Использование:\n`!system`\n`!system set <текст>`\n`!system reset`")

# ====================== ОСНОВНОЙ ОБРАБОТЧИК ======================

@bot.event
async def on_ready():
    logger.info(f"Бот {bot.user} запущен | Owner: {OWNER_ID}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # Только упоминания и личные сообщения
    if not (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        return

    # Игнорируем команды
    if message.content.startswith("!"):
        return

    async with message.channel.typing():
        try:
            user_id = str(message.author.id)
            clean = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
            if not clean:
                clean = "Привет"

            loop = asyncio.get_running_loop()

            # Загружаем настройки
            settings = await loop.run_in_executor(None, get_all_settings)
            model = settings.get("model", "openai/gpt-oss-120b")
            fast_model = settings.get("fast_model", "openai/gpt-oss-20b")
            max_history = int(settings.get("max_history", 12))
            max_len = int(settings.get("max_message_length", 1900))
            global_prompt = settings.get("global_system_prompt", "Ты — полезный ассистент.")

            # Профиль и предпочтения
            profile = await loop.run_in_executor(None, get_user_profile, user_id)
            current_prefs = profile.get("preferences") or {}

            updated_prefs = await loop.run_in_executor(
                None, extract_preferences, clean, current_prefs, fast_model
            )
            if updated_prefs != current_prefs:
                await loop.run_in_executor(None, update_preferences, user_id, updated_prefs)
                current_prefs = updated_prefs
                logger.info(f"Prefs updated for {user_id}: {current_prefs}")

            # Сохраняем сообщение пользователя
            await loop.run_in_executor(None, save_message, user_id, "user", clean)

            # История
            history = await loop.run_in_executor(None, fetch_recent_history, user_id, max_history)

            # Промпт
            system_prompt = build_system_prompt(global_prompt, current_prefs)
            messages = [{"role": "system", "content": system_prompt}] + history

            # Запрос к модели
            response = await loop.run_in_executor(
                None,
                lambda: groq_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=1200,
                    temperature=0.75,
                )
            )

            reply = response.choices[0].message.content.strip()

            # Сохраняем полный ответ
            await loop.run_in_executor(None, save_message, user_id, "assistant", reply)

            # Отправляем частями
            chunks = split_message(reply, max_len=max_len)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)

        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
            await message.reply("Произошла ошибка, попробуй ещё раз.")

bot.run(DISCORD_TOKEN)