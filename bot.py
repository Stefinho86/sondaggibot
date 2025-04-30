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
from apscheduler.schedulers.background import BackgroundScheduler

# ENV
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Conversation states
QUESTION, OPTIONS, DATETIME, RECURRENCE = range(4)

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
conn.commit()

# Scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
logger = logging.getLogger(__name__)

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao! Questo bot crea sondaggi programmati.\n"
        "Usa /nuovosondaggio per iniziare.\n"
        "Comandi utili: /debug /ora"
    )

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"chat_id: {chat_id}")

async def ora_attuale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ora UTC secondo il bot: " + datetime.utcnow().strftime("%Y-%m-%d %H:%M"))

async def nuovosondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(
        "Quando vuoi pubblicare il sondaggio?\n"
        "(Formato: YYYY-MM-DD HH:MM, orario UTC. Scrivi /ora per sapere l'ora UTC attuale)"
    )
    return DATETIME

async def ricevi_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        dt = datetime.strptime(update.message.text, "%Y-%m-%d %H:%M")
        if dt < datetime.utcnow():
            await update.message.reply_text("La data è nel passato. Riprova.")
            return DATETIME
        context.user_data['dt'] = dt.strftime("%Y-%m-%d %H:%M")
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
    application = context.application

    # Salva nel DB
    cur.execute(
        "INSERT INTO polls (chat_id, question, options, schedule_time, recurrence) VALUES (?, ?, ?, ?, ?)",
        (chat_id, question, ",".join(options), dt, recurrence)
    )
    conn.commit()
    poll_id = cur.lastrowid

    # Schedula la pubblicazione
    scheduler.add_job(
        partial(pubblica_sondaggio, chat_id, question, options, application, poll_id, recurrence),
        'date',
        run_date=datetime.strptime(dt, "%Y-%m-%d %H:%M")
    )
    print(f"--- SCHEDULATO sondaggio per chat_id={chat_id} | Domanda: {question} | Opzioni: {options} | Data: {dt} | Ricorrenza: {recurrence}", flush=True)
    logger.info(f"Sondaggio schedulato per chat_id={chat_id} | Domanda: {question} | Opzioni: {options} | Data: {dt} | Ricorrenza: {recurrence}")

    await update.message.reply_text(
        f"Sondaggio programmato per il {dt} UTC con ricorrenza: {recurrence}.\n"
        f"ATTENZIONE: il bot deve essere amministratore del gruppo e poter inviare sondaggi!"
    )
    return ConversationHandler.END

async def annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operazione annullata.")
    return ConversationHandler.END

# --- PUBBLICAZIONE SONDAGGIO ---

async def pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence):
    try:
        print(f"--- SONO DENTRO pubblica_sondaggio per chat_id={chat_id} a {datetime.utcnow()}", flush=True)
        logger.info(f"Tentativo invio sondaggio a chat_id={chat_id} | Domanda: {question} | Opzioni: {options}")
        await application.bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=False
        )
        print(f"--- INVIATO sondaggio per chat_id={chat_id} a {datetime.utcnow()}", flush=True)
        logger.info(f"Sondaggio pubblicato per chat_id={chat_id} con successo!")
        # Ricorrenza
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
                partial(pubblica_sondaggio, chat_id, question, options, application, poll_id, recurrence),
                'date',
                run_date=next_time
            )
        else:
            cur.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
            conn.commit()
    except Exception as e:
        print(f"--- ERRORE invio sondaggio: {e}", flush=True)
        logger.error(f"Errore nell'inviare sondaggio a chat_id={chat_id}: {e}")
        traceback.print_exc()

# --- RIPRISTINO SONDAGGI PERSISTENTI ---

def carica_sondaggi_precedenti(application):
    cur.execute("SELECT id, chat_id, question, options, schedule_time, recurrence FROM polls")
    for poll in cur.fetchall():
        poll_id, chat_id, question, options, schedule_time, recurrence = poll
        dt = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M")
        if dt > datetime.utcnow():
            scheduler.add_job(
                partial(pubblica_sondaggio, chat_id, question, options.split(','), application, poll_id, recurrence),
                'date',
                run_date=dt
            )
            print(f"--- RIPRISTINATO sondaggio per chat_id={chat_id} | Domanda: {question} | Data: {schedule_time} | Ricorrenza: {recurrence}", flush=True)
            logger.info(f"Sondaggio ripristinato per chat_id={chat_id} | Domanda: {question} | Data: {schedule_time} | Ricorrenza: {recurrence}")

# --- MAIN ---

if __name__ == "__main__":
    print("Sto avviando il BOT! Versione aggiornata con /debug e /ora!", flush=True)

    if not TOKEN:
        print("Errore: TOKEN non impostato. Devi configurare la variabile d'ambiente TELEGRAM_BOT_TOKEN.", flush=True)
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('nuovosondaggio', nuovosondaggio)],
        states={
            QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_domanda)],
            OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_opzioni)],
            DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_data)],
            RECURRENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_ricorrenza)],
        },
        fallbacks=[CommandHandler('annulla', annulla)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("ora", ora_attuale))
    application.add_handler(conv_handler)

    carica_sondaggi_precedenti(application)

    print("Bot in esecuzione... Premi CTRL+C per fermarlo.", flush=True)
    application.run_polling()