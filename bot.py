import os
import json
import asyncio
import discord
from discord.ext import commands
from groq import Groq
from supabase import create_client, Client
from dotenv import load_dotenv
from flask import Flask
import threading
from datetime import datetime
import logging

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OWNER_ID = os.getenv("OWNER_ID")  # Discord User ID владельца

# === Flask для Render ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

threading.Thread(target=run_flask, daemon=True).start()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True
)
logger = logging.getLogger(__name__)
logger.info("Бот запущен")
# === Бот ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

groq_client = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

MAX_HISTORY = 12
MODEL = "openai/gpt-oss-120b"
DEFAULT_SYSTEM = "Ты — полезный ассистент. Отвечай на языке пользователя."

# ====================== БАЗА ДАННЫХ ======================

def get_global_system_prompt() -> str:
    try:
        res = supabase.table("bot_settings").select("value").eq("key", "global_system_prompt").execute()
        if res.data:
            return res.data[0]["value"]
    except Exception as e:
        print(f"Ошибка получения global prompt: {e}")
    return DEFAULT_SYSTEM

def set_global_system_prompt(text: str):
    try:
        supabase.table("bot_settings").upsert({
            "key": "global_system_prompt",
            "value": text,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"Ошибка сохранения global prompt: {e}")

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
        print(f"Ошибка профиля: {e}")
        return {"user_id": user_id, "preferences": {}}

def update_preferences(user_id: str, prefs: dict):
    try:
        supabase.table("user_profiles").upsert({
            "user_id": user_id,
            "preferences": prefs,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"Ошибка обновления preferences: {e}")

def fetch_recent_history(user_id: str, limit: int = MAX_HISTORY) -> list:
    try:
        res = supabase.table("chat_history")\
            .select("role, content")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        history = [{"role": r["role"], "content": r["content"]} for r in res.data]
        return list(reversed(history))
    except Exception as e:
        print(f"Ошибка истории: {e}")
        return []

def save_message(user_id: str, role: str, content: str):
    try:
        supabase.table("chat_history").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print(f"Ошибка сохранения: {e}")

# ====================== ПРЕДПОЧТЕНИЯ ======================

def extract_preferences(user_message: str, current_prefs: dict) -> dict:
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
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.8,
        )
        text = resp.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        new_prefs = json.loads(text)
        if isinstance(new_prefs, dict) and new_prefs:
            return {**current_prefs, **new_prefs}
    except Exception as e:
        print(f"Ошибка extract_preferences: {e}")
    return current_prefs

def build_system_prompt(global_prompt: str, preferences: dict) -> str:
    if not preferences:
        return global_prompt

    rules = "\n".join([f"- {k}: {v}" for k, v in preferences.items()])
    return f"""{global_prompt}

Строго соблюдай следующие предпочтения пользователя:
{rules}
"""

# ====================== КОМАНДЫ ВЛАДЕЛЬЦА ======================

@bot.command(name="system")
async def system_command(ctx, *, arg: str = None):
    # Только в личке и только владельцу
    if not isinstance(ctx.channel, discord.DMChannel):
        return await ctx.send("Эта команда работает только в личных сообщениях.")
    
    if str(ctx.author.id) != str(OWNER_ID):
        return await ctx.send("Нет доступа.")

    if arg is None or arg.strip() == "":
        current = get_global_system_prompt()
        return await ctx.send(f"**Текущий глобальный System Prompt:**\n```\n{current}\n```")

    if arg.lower().startswith("set "):
        new_prompt = arg[4:].strip()
        if not new_prompt:
            return await ctx.send("Укажи текст промпта после `set`.")
        set_global_system_prompt(new_prompt)
        return await ctx.send("Глобальный System Prompt обновлён.")

    if arg.lower() == "reset":
        set_global_system_prompt(DEFAULT_SYSTEM)
        return await ctx.send("Глобальный System Prompt сброшен на стандартный.")

    await ctx.send("Использование:\n`!system` — показать\n`!system set <текст>` — установить\n`!system reset` — сбросить")

# ====================== ОСНОВНОЙ ОБРАБОТЧИК ======================

@bot.event
async def on_ready():
    print(f"Бот {bot.user} запущен | Owner ID: {OWNER_ID}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Сначала обрабатываем команды
    await bot.process_commands(message)

    # Дальше только упоминания и ЛС
    if not (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        return

    # Игнорируем сообщения, которые являются командами
    if message.content.startswith("!"):
        return

    async with message.channel.typing():
        try:
            user_id = str(message.author.id)
            clean_content = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
            if not clean_content:
                clean_content = "Привет"

            loop = asyncio.get_running_loop()

            # 1. Глобальный промпт + профиль
            global_prompt = await loop.run_in_executor(None, get_global_system_prompt)
            profile = await loop.run_in_executor(None, get_user_profile, user_id)
            current_prefs = profile.get("preferences", {})

            # 2. Обновление предпочтений (если пользователь просит)
            updated_prefs = await loop.run_in_executor(None, extract_preferences, clean_content, current_prefs)
            if updated_prefs != current_prefs:
                await loop.run_in_executor(None, update_preferences, user_id, updated_prefs)
                current_prefs = updated_prefs
                print(f"[Prefs] {user_id} → {current_prefs}")

            # 3. Сохраняем сообщение пользователя
            await loop.run_in_executor(None, save_message, user_id, "user", clean_content)

            # 4. История
            history = await loop.run_in_executor(None, fetch_recent_history, user_id)

            # 5. Собираем финальный промпт
            system_prompt = build_system_prompt(global_prompt, current_prefs)
            messages = [{"role": "system", "content": system_prompt}] + history

            # 6. Запрос к модели
            response = await loop.run_in_executor(
                None,
                lambda: groq_client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    max_tokens=600,
                    temperature=0.75,
                )
            )

            reply = response.choices[0].message.content.strip()
            if len(reply) > 1900:
                reply = reply[:1900] + "..."

            # 7. Сохраняем ответ
            await loop.run_in_executor(None, save_message, user_id, "assistant", reply)

            await message.reply(reply)

        except Exception as e:
            print(f"Ошибка: {e}")
            logger.error(f"Ошибка: {e}")
            await message.reply(f"Произошла ошибка, попробуй ещё раз. {e}")

bot.run(DISCORD_TOKEN)