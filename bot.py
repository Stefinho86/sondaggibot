import os
import logging
import sys
import sqlite3
from datetime import datetime, timedelta
from functools import partial
import traceback

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from timezonefinder import TimezoneFinder
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Conversation states
SELECT_CITY, QUESTION, OPTIONS, DATETIME, RECURRENCE = range(5)

# DB Setup
conn = sqlite3.connect('sondaggi.db', check_same_thread=False)
cur = conn.cursor()
cur.execute('''CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    question TEXT,
    options TEXT,
    schedule_time TEXT,
    recurrence TEXT
)''')
cur.execute('''CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    city TEXT,
    timezone TEXT
)''')
conn.commit()

scheduler = BackgroundScheduler()
scheduler.start()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
logger = logging.getLogger(__name__)

tf = TimezoneFinder()

# --- UTILS ---

def get_timezone_for_city(city_name):
    try:
        url = f"https://nominatim.openstreetmap.org/search?city={city_name}&format=json"
        response = requests.get(url, headers={"User-Agent": "TelegramPollBot/1.0"})
        results = response.json()
        if not results:
            return None, None
        lat = float(results[0]['lat'])
        lon = float(results[0]['lon'])
        timezone_str = tf.timezone_at(lat=lat, lng=lon)
        return timezone_str, (lat, lon)
    except Exception as e:
        logger.error(f"Errore nel recuperare la timezone per la città {city_name}: {e}")
        return None, None

def get_timezone_for_chat(chat_id):
    cur.execute("SELECT timezone FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    else:
        return None

def save_chat_timezone(chat_id, city, timezone):
    cur.execute("INSERT OR REPLACE INTO chat_settings (chat_id, city, timezone) VALUES (?, ?, ?)",
        (chat_id, city, timezone))
    conn.commit()

def get_city_for_chat(chat_id):
    cur.execute("SELECT city FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    else:
        return None

# --- APSCHEDULER PATCH: RUN ASYNC JOBS ---
def run_async_job(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        asyncio.ensure_future(coro)
    else:
        loop.run_until_complete(coro)

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_timezone_for_chat(chat_id)
    if not tz:
        await update.message.reply_text(
            "In che città vuoi usare il bot? (scrivi solo il nome della città, esempio: Milano)"
        )
        return SELECT_CITY
    else:
        city = get_city_for_chat(chat_id)
        await update.message.reply_text(
            f"Bot pronto per {city}.\n"
            "Usa /nuovosondaggio per iniziare.\n"
            "Comandi utili: /debug /ora"
        )

async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    city = update.message.text.strip()
    timezone_str, coords = get_timezone_for_city(city)
    if not timezone_str:
        await update.message.reply_text(
            "Città non trovata! Riprova (esempio: Roma, Napoli, Palermo, New York, London)"
        )
        return SELECT_CITY
    save_chat_timezone(chat_id, city.title(), timezone_str)
    await update.message.reply_text(
        f"Impostata città: {city.title()} (fuso orario: {timezone_str})\n"
        "Ora puoi usare /nuovosondaggio!"
    )
    return ConversationHandler.END

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_timezone_for_chat(chat_id)
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"chat_id: {chat_id}\n"
        f"Città: {city or 'Non impostata'}\n"
        f"Timezone: {tz or 'Non impostato'}"
    )

async def ora_attuale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    if not tz_str:
        await update.message.reply_text("Prima imposta la città con /start")
        return
    utc_now = datetime.utcnow()
    tz = pytz.timezone(tz_str)
    local_now = pytz.utc.localize(utc_now).astimezone(tz)
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Ora locale {city} ({tz_str}): {local_now.strftime('%Y-%m-%d %H:%M')}\n"
        f"Ora UTC: {utc_now.strftime('%Y-%m-%d %H:%M')}"
    )

async def nuovosondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_timezone_for_chat(chat_id)
    if not tz:
        await update.message.reply_text(
            "Prima imposta la città con /start"
        )
        return ConversationHandler.END
    await update.message.reply_text("Scrivi la domanda del sondaggio.")
    return QUESTION

async def ricevi_domanda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['question'] = update.message.text
    await update.message.reply_text("Ora scrivi le opzioni separate da virgola (esempio: Sì,No,Forse)")
    return OPTIONS

async def ricevi_opzioni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [opt.strip() for opt in update.message.text.split(',')]
    if len(options) < 2:
        await update.message.reply_text("Servono almeno due opzioni!")
        return OPTIONS
    if len(options) > 10:
        await update.message.reply_text("Telegram permette massimo 10 opzioni. Riprova.")
        return OPTIONS
    context.user_data['options'] = options
    chat_id = update.effective_chat.id
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Quando vuoi pubblicare il sondaggio?\n"
        f"(Formato: YYYY-MM-DD HH:MM, orario LOCALE di {city})"
    )
    return DATETIME

async def ricevi_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    if not tz_str:
        await update.message.reply_text("Prima imposta la città con /start")
        return ConversationHandler.END
    try:
        local_dt = datetime.strptime(update.message.text, "%Y-%m-%d %H:%M")
        tz = pytz.timezone(tz_str)
        local_dt = tz.localize(local_dt)
        utc_dt = local_dt.astimezone(pytz.utc)
        if utc_dt < datetime.utcnow().replace(tzinfo=pytz.utc):
            await update.message.reply_text("La data è nel passato. Riprova.")
            return DATETIME
        context.user_data['dt'] = utc_dt.strftime("%Y-%m-%d %H:%M")
        context.user_data['local_dt'] = local_dt.strftime("%Y-%m-%d %H:%M")
        city = get_city_for_chat(chat_id)
        await update.message.reply_text(
            "Vuoi che il sondaggio sia:\n"
            "- Senza ricorrenza\n"
            "- Giornaliera\n"
            "- Settimanale\n"
            "Scrivi: nessuna, giornaliera, settimanale"
        )
        return RECURRENCE
    except Exception:
        await update.message.reply_text(
            "Formato data/ora non valido. Riprova (esempio: 2025-04-30 16:00)"
        )
        return DATETIME

async def ricevi_ricorrenza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recurrence = update.message.text.strip().lower()
    if recurrence not in ["nessuna", "giornaliera", "settimanale"]:
        await update.message.reply_text("Rispondi: nessuna, giornaliera, o settimanale.")
        return RECURRENCE

    chat_id = update.effective_chat.id
    question = context.user_data['question']
    options = context.user_data['options']
    dt = context.user_data['dt']
    local_dt = context.user_data['local_dt']
    application = context.application

    cur.execute(
        "INSERT INTO polls (chat_id, question, options, schedule_time, recurrence) VALUES (?, ?, ?, ?, ?)",
        (chat_id, question, ",".join(options), dt, recurrence)
    )
    conn.commit()
    poll_id = cur.lastrowid

    scheduler.add_job(
        run_async_job,
        'date',
        run_date=datetime.strptime(dt, "%Y-%m-%d %H:%M"),
        args=(pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence),)
    )
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Sondaggio programmato per il {local_dt} ({city}) "
        f"(UTC: {dt}) con ricorrenza: {recurrence}.\n"
        f"ATTENZIONE: il bot deve essere amministratore del gruppo e poter inviare sondaggi!"
    )
    print(f"--- SCHEDULATO sondaggio per chat_id={chat_id} | Domanda: {question} | Opzioni: {options} | Data(UTC): {dt} | Ricorrenza: {recurrence}", flush=True)
    logger.info(f"Sondaggio schedulato per chat_id={chat_id} | Domanda: {question} | Opzioni: {options} | Data: {dt} | Ricorrenza: {recurrence}")
    return ConversationHandler.END

async def annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operazione annullata.")
    return ConversationHandler.END

async def pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence):
    print(f"--- PUBBLICAZIONE: provo a inviare sondaggio a chat_id={chat_id} alle {datetime.utcnow()}", flush=True)
    try:
        logger.info(f"Tentativo invio sondaggio a chat_id={chat_id} | Domanda: {question} | Opzioni: {options}")
        await application.bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=False
        )
        print(f"--- INVIATO sondaggio per chat_id={chat_id} a {datetime.utcnow()}", flush=True)
        logger.info(f"Sondaggio pubblicato per chat_id={chat_id} con successo!")
        if recurrence == "giornaliera":
            next_time = datetime.utcnow() + timedelta(days=1)
        elif recurrence == "settimanale":
            next_time = datetime.utcnow() + timedelta(weeks=1)
        else:
            next_time = None
        if next_time:
            cur.execute(
                "UPDATE polls SET schedule_time = ? WHERE id = ?",
                (next_time.strftime("%Y-%m-%d %H:%M"), poll_id)
            )
            conn.commit()
            scheduler.add_job(
                run_async_job,
                'date',
                run_date=next_time,
                args=(pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence),)
            )
        else:
            cur.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
            conn.commit()
    except Exception as e:
        print(f"--- ERRORE invio sondaggio: {e}", flush=True)
        logger.error(f"Errore nell'inviare sondaggio a chat_id={chat_id}: {e}")
        traceback.print_exc()

def carica_sondaggi_precedenti(application):
    cur.execute("SELECT id, chat_id, question, options, schedule_time, recurrence FROM polls")
    for poll in cur.fetchall():
        poll_id, chat_id, question, options, schedule_time, recurrence = poll
        dt = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M")
        if dt > datetime.utcnow():
            scheduler.add_job(
                run_async_job,
                'date',
                run_date=dt,
                args=(pubblica_sondaggio(chat_id, question, options.split(','), application, poll_id, recurrence),)
            )
            print(f"--- RIPRISTINATO sondaggio per chat_id={chat_id} | Domanda: {question} | Data: {schedule_time} | Ricorrenza: {recurrence}", flush=True)
            logger.info(f"Sondaggio ripristinato per chat_id={chat_id} | Domanda: {question} | Data: {schedule_time} | Ricorrenza: {recurrence}")

if __name__ == "__main__":
    print("Sto avviando il BOT! Versione città/fuso orario auto con log pubblicazione!", flush=True)

    if not TOKEN:
        print("Errore: TOKEN non impostato. Devi configurare la variabile d'ambiente TELEGRAM_BOT_TOKEN.", flush=True)
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('nuovosondaggio', nuovosondaggio)],
        states={
            SELECT_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_city)],
            QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_domanda)],
            OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_opzioni)],
            DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_data)],
            RECURRENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_ricorrenza)],
        },
        fallbacks=[CommandHandler('annulla', annulla)],
    )

    city_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECT_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_city)],
        },
        fallbacks=[CommandHandler('annulla', annulla)],
    )

    application.add_handler(city_conv_handler)
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("ora", ora_attuale))
    application.add_handler(conv_handler)

    carica_sondaggi_precedenti(application)

    print("Bot in esecuzione... Premi CTRL+C per fermarlo.", flush=True)
    application.run_polling()