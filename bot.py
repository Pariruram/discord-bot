import os
import json
import asyncio
import logging
from datetime import datetime
from collections import defaultdict
from functools import lru_cache

import discord
from discord.ext import commands
from groq import AsyncGroq  # <--- Асинхронный клиент Groq
from supabase import create_client, Client
from dotenv import load_dotenv
from flask import Flask
import threading
from duckduckgo_search import DDGS

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True
)
logger = logging.getLogger("bot")

# === Flask для Render / Hugging Face ===
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

threading.Thread(target=run_flask, daemon=True).start()

# === Discord + API ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === Память каналов в RAM (кэш поверх Supabase) ===
channel_memory = defaultdict(lambda: {"messages": [], "summary": ""})

RECENT_LIMIT = 15          
COMPACT_EVERY = 20         

# ====================== ИНСТРУМЕНТ ПОИСКА (С КЭШЕМ) ======================

@lru_cache(maxsize=100)
def cached_web_search(query: str) -> str:
    """Выполняет поиск в интернете с кэшированием результатов."""
    try:
        with DDGS() as ddgs:
            results = [r.get('body', '') for r in ddgs.text(query, max_results=3)]
            if not results:
                return "Поиск не дал результатов."
            return "\n---\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка веб-поиска: {e}")
        return "Ошибка доступа к поисковым системам."

async def web_search(query: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, cached_web_search, query)

tools = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Ищет актуальную информацию в интернете. Используй этот инструмент всегда, когда запрашиваемые данные выходят за рамки твоей обучающей выборки или требуют проверки актуального состояния на текущий момент.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Оптимальный поисковый запрос на русском или английском языке.",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

# ====================== НАСТРОЙКИ И БАЗА ДАННЫХ ======================

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

def get_owners() -> list[str]:
    try:
        raw = get_setting("owners", "[]")
        owners = json.loads(raw)
        if isinstance(owners, list):
            return [str(o) for o in owners]
    except Exception as e:
        logger.error(f"get_owners: {e}")
    return []

def save_owners(owners: list[str]):
    set_setting("owners", json.dumps(owners))

def is_owner(user_id: str | int) -> bool:
    return str(user_id) in get_owners()

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

# ====================== ПАМЯТЬ КАНАЛОВ С SUPABASE ======================

def load_channel_context_from_db(channel_id: str) -> dict:
    try:
        res = supabase.table("channel_context").select("summary, messages").eq("channel_id", channel_id).execute()
        if res.data:
            row = res.data[0]
            return {
                "summary": row.get("summary") or "",
                "messages": row.get("messages") or []
            }
    except Exception as e:
        logger.error(f"load_channel_context_from_db: {e}")
    return {"summary": "", "messages": []}

def save_channel_context_to_db(channel_id: str, summary: str, messages: list):
    try:
        supabase.table("channel_context").upsert({
            "channel_id": channel_id,
            "summary": summary,
            "messages": messages,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"save_channel_context_to_db: {e}")

async def make_compact(old_summary: str, messages: list, fast_model: str) -> str:
    lines = [f"{m['author']}: {m['content']}" for m in messages]
    text = "\n".join(lines)

    prompt = f"""Ты сжимаешь историю чата Discord в краткое резюме.
Предыдущее резюме:
{old_summary or "(пусто)"}

Новые сообщения:
{text}

Сделай обновлённое краткое резюме (5–10 предложений): суть, темы, договорённости. Только текст резюме."""

    try:
        resp = await groq_client.chat.completions.create(
            model=fast_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"make_compact: {e}")
        return old_summary

async def add_to_channel_memory(channel_id: str, author: str, content: str, fast_model: str):
    mem = channel_memory[channel_id]
    
    # Ленивая загрузка из Supabase при первом обращении в сессии
    if not mem["messages"] and not mem["summary"]:
        db_data = load_channel_context_from_db(channel_id)
        mem["summary"] = db_data["summary"]
        mem["messages"] = db_data["messages"]

    mem["messages"].append({
        "author": author,
        "content": content[:500]  
    })

    if len(mem["messages"]) > RECENT_LIMIT + COMPACT_EVERY:
        to_compact = mem["messages"][:-RECENT_LIMIT]
        mem["messages"] = mem["messages"][-RECENT_LIMIT:]
        mem["summary"] = await make_compact(mem["summary"], to_compact, fast_model)
        logger.info(f"Compact channel {channel_id}")

    # Сохраняем актуальный срез и саммари в Supabase
    save_channel_context_to_db(channel_id, mem["summary"], mem["messages"])

def get_channel_context(channel_id: str) -> str:
    mem = channel_memory[channel_id]
    parts = []

    if mem["summary"]:
        parts.append(f"Краткое резюме предыдущего общения в канале:\n{mem['summary']}")

    if mem["messages"]:
        lines = [f"{m['author']}: {m['content']}" for m in mem["messages"]]
        parts.append("Последние сообщения в канале:\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else ""

# ====================== ПРЕДПОЧТЕНИЯ ======================

async def extract_preferences(user_message: str, current_prefs: dict, fast_model: str) -> dict:
    prompt = f"""
Ты — анализатор предпочтений стиля общения.
Текущие предпочтения: {json.dumps(current_prefs, ensure_ascii=False)}
Сообщение пользователя: "{user_message}"
Если пользователь ЯВНО просит изменить стиль/формат — верни ТОЛЬКО новый JSON с обновлениями, иначе {{}}. Только валидный JSON.
"""
    try:
        resp = await groq_client.chat.completions.create(
            model=fast_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        text = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        new_prefs = json.loads(text)
        if isinstance(new_prefs, dict) and new_prefs:
            return {**current_prefs, **new_prefs}
    except Exception as e:
        logger.error(f"extract_preferences: {e}")
    return current_prefs

def build_system_prompt(global_prompt: str, preferences: dict, channel_context: str = "") -> str:
    parts = [global_prompt]
    if preferences:
        rules = "\n".join(f"- {k}: {v}" for k, v in preferences.items())
        parts.append(f"Строго соблюдай предпочтения пользователя:\n{rules}")
    if channel_context:
        parts.append(f"Контекст Discord-канала:\n{channel_context}")
    return "\n\n".join(parts)

def split_message(text: str, max_len: int = 1900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        split_at = text.rfind("\n\n", 0, max_len)
        if split_at == -1: split_at = text.rfind("\n", 0, max_len)
        if split_at == -1: split_at = max_len
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return parts

# ====================== КОМАНДЫ ======================

@bot.command(name="owners")
async def owners_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel) or not is_owner(ctx.author.id):
        return await ctx.send("Нет доступа.")
    owners = get_owners()
    await ctx.send(f"**Владельцы:**\n" + "\n".join(f"- `{o}`" for o in owners))

@bot.command(name="owner")
async def owner_cmd(ctx, action: str = None, user_id: str = None):
    if not isinstance(ctx.channel, discord.DMChannel) or not is_owner(ctx.author.id):
        return await ctx.send("Нет доступа.")
    owners = get_owners()
    if action == "add" and user_id and user_id.isdigit() and user_id not in owners:
        owners.append(user_id)
        save_owners(owners)
        return await ctx.send(f"Добавлен: `{user_id}`")
    elif action == "remove" and user_id in owners and len(owners) > 1:
        owners.remove(user_id)
        save_owners(owners)
        return await ctx.send(f"Удален: `{user_id}`")
    await ctx.send("Использование: `!owner add <id>` или `!owner remove <id>`")

@bot.command(name="settings")
async def settings_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel) or not is_owner(ctx.author.id):
        return await ctx.send("Нет доступа.")
    settings = get_all_settings()
    await ctx.send("**Настройки:**\n" + "\n".join(f"**{k}** = `{v}`" for k, v in settings.items()))

@bot.command(name="set")
async def set_cmd(ctx, key: str = None, *, value: str = None):
    if not isinstance(ctx.channel, discord.DMChannel) or not is_owner(ctx.author.id) or key not in {"model", "fast_model", "max_history", "max_message_length"}:
        return await ctx.send("Неверный ключ или нет доступа.")
    set_setting(key, value.strip())
    await ctx.send(f"`{key}` изменен.")

@bot.command(name="system")
async def system_cmd(ctx, *, arg: str = None):
    if not isinstance(ctx.channel, discord.DMChannel) or not is_owner(ctx.author.id):
        return await ctx.send("Нет доступа.")
    if not arg:
        return await ctx.send(f"```\n{get_setting('global_system_prompt', 'Ты — ассистент.')}\n```")
    if arg.lower().startswith("set "):
        set_setting("global_system_prompt", arg[4:].strip())
        return await ctx.send("Prompt обновлен.")
    if arg.lower() == "reset":
        set_setting("global_system_prompt", "Ты — полезный ассистент.")
        return await ctx.send("Сброшено.")

# ====================== ОБРАБОТЧИК СООБЩЕНИЙ ======================

@bot.event
async def on_ready():
    logger.info(f"Бот {bot.user} запущен")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)
    if message.content.startswith("!"):
        return

    settings = get_all_settings()
    fast_model = settings.get("fast_model", "openai/gpt-oss-20b")
    model = settings.get("model", "openai/gpt-oss-120b")
    max_history = int(settings.get("max_history", 12))
    max_len = int(settings.get("max_message_length", 1900))
    global_prompt = settings.get("global_system_prompt", "Ты — полезный ассистент.")

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user.mentioned_in(message)

    if not is_dm:
        await add_to_channel_memory(str(message.channel.id), message.author.display_name, message.content.strip(), fast_model)

    if not (is_mentioned or is_dm):
        return

    async with message.channel.typing():
        try:
            user_id = str(message.author.id)
            clean = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip() or "Привет"

            profile = get_user_profile(user_id)
            current_prefs = profile.get("preferences") or {}
            updated_prefs = await extract_preferences(clean, current_prefs, fast_model)
            if updated_prefs != current_prefs:
                update_preferences(user_id, updated_prefs)
                current_prefs = updated_prefs

            save_message(user_id, "user", clean)
            history = fetch_recent_history(user_id, max_history)
            channel_context = get_channel_context(str(message.channel.id)) if not is_dm else ""

            system_prompt = build_system_prompt(global_prompt, current_prefs, channel_context)
            messages = [{"role": "system", "content": system_prompt}] + history

            # === Multi-turn Tool Use цикл (до 3 шагов поиска) ===
            for _ in range(3):
                response = await groq_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=1200,
                    temperature=0.75,
                )
                response_message = response.choices[0].message

                if not response_message.tool_calls:
                    reply = response_message.content.strip()
                    break

                messages.append(response_message)

                for tool_call in response_message.tool_calls:
                    if tool_call.function.name == "web_search":
                        try:
                            args = json.loads(tool_call.function.arguments)
                            query = args.get("query", "")
                            logger.info(f"🤖 Поиск: {query}")
                            search_result = await web_search(query)
                        except Exception as ex:
                            logger.error(f"Ошибка парсинга аргументов поиска: {ex}")
                            search_result = "Ошибка выполнения поиска."

                        messages.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": "web_search",
                            "content": search_result,
                        })
            else:
                reply = "Извини, я сделал слишком много поисковых запросов и запутался."

            save_message(user_id, "assistant", reply)

            for i, chunk in enumerate(split_message(reply, max_len=max_len)):
                if i == 0:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)

        except Exception as e:
            logger.error(f"Ошибка обработки: {e}", exc_info=True)
            await message.reply("Произошла ошибка, попробуй ещё раз.")

bot.run(DISCORD_TOKEN)