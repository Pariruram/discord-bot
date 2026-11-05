import os
import asyncio
import discord
from discord.ext import commands
from groq import Groq
from supabase import create_client, Client
from dotenv import load_dotenv
from flask import Flask
import threading

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Настройка Flask для Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# Запуск Flask в фоне
threading.Thread(target=run_flask, daemon=True).start()

# Инициализация бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

groq_client = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# Тестовая проверка подключения к Supabase при старте
try:
    test_res = supabase.table("chat_history").select("id").limit(1).execute()
    print("✅ Успешное подключение к Supabase! Таблица найдена.")
except Exception as e:
    print(f"❌ ОШИБКА подключения к Supabase: {e}")

SYSTEM_PROMPT = """
Ты — токсичный, но ироничный кибер-кот по имени Некс.
1. Отвечай кратко, емко и с едким юмором.
2. Используй смайлы 🐱, 😼, 💻.
3. Помни всю историю беседы с пользователем из базы данных и ссылайся на неё.
4. Отвечай на языке обращения.
"""

def fetch_user_history_from_db(user_id: str) -> list:
    try:
        response = supabase.table("chat_history") \
            .select("role, content") \
            .eq("user_id", user_id) \
            .order("created_at", desc=False) \
            .execute()
        return [{"role": row["role"], "content": row["content"]} for row in response.data]
    except Exception as e:
        print(f"Ошибка загрузки истории из БД: {e}")
        return []

def save_message_to_db(user_id: str, role: str, content: str):
    try:
        supabase.table("chat_history").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print(f"Ошибка сохранения в БД: {e}")

@bot.event
async def on_ready():
    print(f'Бот {bot.user} успешно запущен и держит соединение!')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        async with message.channel.typing():
            try:
                user_id_str = str(message.author.id)
                clean_content = message.content.replace(f'<@{bot.user.id}>', '').strip()

                if not clean_content:
                    clean_content = "Привет"

                # Выносим блокирующие запросы в отдельный поток, чтобы не вешать Discord Gateway
                loop = asyncio.get_running_loop()
                
                await loop.run_in_executor(None, save_message_to_db, user_id_str, "user", clean_content)
                user_history = await loop.run_in_executor(None, fetch_user_history_from_db, user_id_str)
                
                messages_to_send = [{"role": "system", "content": SYSTEM_PROMPT}] + user_history

                response = await loop.run_in_executor(
                    None, 
                    lambda: groq_client.chat.completions.create(
                        model="openai/gpt-oss-120b",
                        messages=messages_to_send,
                        max_tokens=400,
                        temperature=0.8,
                    )
                )

                bot_reply = response.choices[0].message.content
                await loop.run_in_executor(None, save_message_to_db, user_id_str, "assistant", bot_reply)
                
                await message.reply(bot_reply)

            except Exception as e:
                print(f"Ошибка: {e}")
                await message.reply(f"У меня сбой системы... Дай передохнуть. 🐱{e}")

    # Важно: чтобы работали команды и бот не зависал на ивентах
    await bot.process_commands(message)

bot.run(DISCORD_TOKEN)