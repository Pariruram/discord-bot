import os
import threading
import discord
from discord.ext import commands
from groq import Groq
from supabase import create_client, Client
from dotenv import load_dotenv
from flask import Flask

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Настройка Flask для удержания Web Service бесплатным
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Запускаем Flask в отдельном потоке, чтобы он не блокировал бота
threading.Thread(target=run_flask).start()

# Инициализация бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

groq_client = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
    print(f'Бот {bot.user} запущен на Web Service и подключен к Supabase!')

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

                save_message_to_db(user_id_str, "user", clean_content)
                user_history = fetch_user_history_from_db(user_id_str)
                messages_to_send = [{"role": "system", "content": SYSTEM_PROMPT}] + user_history

                response = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=messages_to_send,
                    max_tokens=400,
                    temperature=0.8,
                )

                bot_reply = response.choices[0].message.content
                save_message_to_db(user_id_str, "assistant", bot_reply)
                await message.reply(bot_reply)

            except Exception as e:
                print(f"Ошибка: {e}")
                await message.reply("У меня сбой системы... Дай передохнуть. 🐱")

    await bot.process_commands(message)

bot.run(DISCORD_TOKEN)