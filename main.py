import logging
import sqlite3
import threading
import textwrap
import re
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import ollama

# --- НАСТРОЙКИ ---
TOKEN = '7833956504:AAHCLP5vnewZX3icxE7aZ50vSkMdkKzdFcw'
MODEL_NAME = 'llama3'
TEMPERATURE = 0.6

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# --- SQLite (один поток, оптимизация) ---
DB_LOCK = threading.Lock()
conn = sqlite3.connect("/home/bogdan/projects_2025/telegrammteach/prompts.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS prompts (
    user_id INTEGER PRIMARY KEY,
    prompt TEXT
)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS memory (
    user_id INTEGER,
    question TEXT,
    answer TEXT
)''')
conn.commit()

# --- ПРОФИЛИ, ВОПРОСЫ, ПАМЯТЬ ---
student_profile = {}
dialog_memory = {}
MAX_HISTORY = 5

questions = [
    {"question": "Какой у вас уровень опыта в IT?", "options": ["Начинающий", "Средний", "Продвинутый"], "key": "опыт"},
    {"question": "Какова ваша мотивация?", "options": ["Низкая", "Средняя", "Высокая"], "key": "мотивация"},
    {"question": "Какой способ подачи информации вам удобен?", "options": ["Визуальный", "Текстовый", "Интерактивный"], "key": "стиль"},
    {"question": "С какими трудностями сталкиваетесь?", "options": ["Дефицит внимания", "Плохая память", "Прокрастинация", "Нет проблем"], "key": "особенности"},
    {"question": "Есть ли ещё что-то, что вы хотели бы сообщить о себе?", "options": [], "key": "дополнительно"}
]
current_question = {}

@dp.message_handler(commands=['start'])
async def welcome_message(message: types.Message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(
        KeyboardButton("Пройти тестирование"),
        KeyboardButton("Общаться сразу"),
        KeyboardButton("📚 Показать диалог"),
        KeyboardButton("🧹 Очистить диалог")
    )
    await message.answer(
        "👋 Привет! Я виртуальный преподаватель, который поможет тебе в изучении IT.\n\n"
        "Ты можешь пройти небольшое тестирование, чтобы я мог персонализировать твои уроки, или сразу начать общение.",
        reply_markup=markup
    )

@dp.message_handler(lambda message: message.text == "Пройти тестирование")
async def start_test(message: types.Message):
    student_profile[message.from_user.id] = {}
    await send_question(message.from_user.id, 0)

@dp.message_handler(lambda message: message.text == "Общаться сразу")
async def start_chat_directly(message: types.Message):
    await message.answer("Отлично! Можешь задавать вопросы прямо сейчас.")

@dp.message_handler(lambda message: message.text == "Посмотреть мой промт")
async def show_prompt(message: types.Message):
    with DB_LOCK:
        cursor.execute("SELECT prompt FROM prompts WHERE user_id = ?", (message.from_user.id,))
        row = cursor.fetchone()

    if row:
        cleaned = clean_code_blocks(row[0])
        await message.answer(f"📋 Сохранённый промт:\n\n{cleaned}")
    else:
        await message.answer("❌ Промт пока не сформирован. Пройди тестирование или задай вопрос.")

@dp.message_handler(lambda message: message.text == "📚 Показать диалог")
async def show_dialog(message: types.Message):
    with DB_LOCK:
        cursor.execute("SELECT question, answer FROM memory WHERE user_id = ?", (message.from_user.id,))
        rows = cursor.fetchall()

    if not rows:
        await message.answer("Память пуста.")
        return

    dialog = "\n\n".join([f"🔹 {q}\n💬 {a}" for q, a in rows[-5:]])
    await message.answer(f"🧠 Последние сообщения:\n\n{dialog}")

@dp.message_handler(lambda message: message.text == "🧹 Очистить диалог")
async def clear_dialog(message: types.Message):
    dialog_memory.pop(message.from_user.id, None)
    with DB_LOCK:
        cursor.execute("DELETE FROM memory WHERE user_id = ?", (message.from_user.id,))
        conn.commit()
    await message.answer("🧠 История диалога очищена!")

@dp.message_handler()
async def handle_message(message: types.Message):
    user_id = message.from_user.id

    if len(message.text) > 1000:
        await message.answer("⚠️ Пожалуйста, сократи свой вопрос до 1000 символов.")
        return

    if user_id in current_question:
        q_index = current_question[user_id]
        question = questions[q_index]
        student_profile[user_id][question["key"]] = message.text

        if q_index + 1 < len(questions):
            await send_question(user_id, q_index + 1)
        else:
            del current_question[user_id]
            await message.answer("🧠 Генерируются персональные установки...")

            profile = student_profile[user_id]
            system_prompt = await generate_prompt_from_profile(profile)

            with DB_LOCK:
                cursor.execute("REPLACE INTO prompts (user_id, prompt) VALUES (?, ?)", (user_id, system_prompt))
                conn.commit()

            markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            markup.add(
                KeyboardButton("Посмотреть мой промт"),
                KeyboardButton("Общаться сразу"),
                KeyboardButton("Пройти тестирование"),
                KeyboardButton("📚 Показать диалог"),
                KeyboardButton("🧹 Очистить диалог")
            )
            await bot.send_message(user_id, "✅ Тестирование завершено! Установки созданы. Что дальше?", reply_markup=markup)

    else:
        with DB_LOCK:
            cursor.execute("SELECT prompt FROM prompts WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()

        base_prompt = row[0] if row else ""
        history = dialog_memory.get(user_id, [])
        history_text = "\n".join([f"Вопрос: {q}\nОтвет: {a}" for q, a in history])
        full_prompt = f"{base_prompt}\n\n{history_text}\nВопрос: {message.text}\nОтвет:"

        response = ollama.generate(
            model=MODEL_NAME,
            prompt=full_prompt,
            options={'temperature': TEMPERATURE}
        )

        cleaned_response = clean_code_blocks(response['response'].strip())
        short_question = await summarize_text(message.text)
        short_answer = await summarize_text(cleaned_response)

        dialog_memory.setdefault(user_id, []).append((short_question, short_answer))
        dialog_memory[user_id] = dialog_memory[user_id][-MAX_HISTORY:]

        with DB_LOCK:
            cursor.execute("INSERT INTO memory (user_id, question, answer) VALUES (?, ?, ?)",
                           (user_id, short_question, short_answer))
            cursor.execute("DELETE FROM memory WHERE user_id = ? AND rowid NOT IN (SELECT rowid FROM memory WHERE user_id = ? ORDER BY rowid DESC LIMIT ?)",
                           (user_id, user_id, MAX_HISTORY))
            conn.commit()

        await bot.send_message(user_id, cleaned_response)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def send_question(user_id, q_index):
    question = questions[q_index]
    current_question[user_id] = q_index
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    if question["options"]:
        for option in question["options"]:
            markup.add(KeyboardButton(option))
        await bot.send_message(user_id, question["question"], reply_markup=markup)
    else:
        await bot.send_message(user_id, question["question"])

def clean_code_blocks(text: str) -> str:
    def fix_block(match):
        code = match.group(1)
        dedented = textwrap.dedent(code)
        return f"```\n{dedented.strip()}\n```"
    return re.sub(r'```[a-zA-Z]*\n(.*?)```', fix_block, text, flags=re.DOTALL)

async def summarize_text(text: str) -> str:
    prompt = f"Сократи до одной строки, в чём суть вопроса или ответа:\n{text}\n\nСуть:"
    response = ollama.generate(
        model=MODEL_NAME,
        prompt=prompt,
        options={'temperature': 0.3}
    )
    return response['response'].strip()

async def generate_prompt_from_profile(profile):
    base_instruction = (
        f"Проанализируй профиль студента и сформулируй 2–3 краткие рекомендации для промтов к его запросу, чтобы получить подходящий ответ. "
        f"Отвечай на русском без вводных слов, только суть.\n\n"
        f"Профиль:\n"
        f"- Опыт: {profile.get('опыт')}\n"
        f"- Мотивация: {profile.get('мотивация')}\n"
        f"- Стиль: {profile.get('стиль')}\n"
        f"- Особенности: {profile.get('особенности')}\n"
        f"- Дополнительно: {profile.get('дополнительно', 'нет')}"
    )

    response = ollama.generate(
        model=MODEL_NAME,
        prompt=base_instruction,
        options={'temperature': TEMPERATURE}
    )

    recommendations = response['response'].strip()
    return (
        "Ты — виртуальный преподаватель по IT. Отвечай на русском, лаконично и понятно. Не пиши лишнего.\n"
        f"Профиль студента:\n"
        f"- Опыт: {profile.get('опыт')}\n"
        f"- Мотивация: {profile.get('мотивация')}\n"
        f"- Стиль: {profile.get('стиль')}\n"
        f"- Особенности: {profile.get('особенности')}\n"
        f"- Дополнительно: {profile.get('дополнительно', 'нет')}\n\n"
        f"Рекомендации:\n{recommendations}"
    )

# --- ПОДГРУЗКА ПАМЯТИ ---
with DB_LOCK:
    cursor.execute("SELECT user_id, question, answer FROM memory")
    for user_id, question, answer in cursor.fetchall():
        dialog_memory.setdefault(user_id, []).append((question, answer))

# --- ЗАПУСК ---
if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)

