import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import sqlite3

TOKEN = "7430014492:AAEh-fyDDfmsIs3ArNFYQCEKY26aD_ROHDg"  # <-- metti qui il tuo token!

# Stati della conversazione
QUESTION, OPTIONS, DATETIME, RECURRENCE = range(4)

# DB setup
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

scheduler = BackgroundScheduler()
scheduler.start()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

# Funzione per pubblicare il sondaggio
async def pubblica_sondaggio(chat_id, question, options, context, poll_id, recurrence):
    try:
        await context.bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=False
        )
        # Rischedula se ricorrente
        if recurrence == "giornaliera":
            next_time = datetime.now() + timedelta(days=1)
        elif recurrence == "settimanale":
            next_time = datetime.now() + timedelta(weeks=1)
        else:
            next_time = None
        if next_time:
            # Aggiorna la prossima data nel DB
            cur.execute(
                "UPDATE polls SET schedule_time = ? WHERE id = ?",
                (next_time.strftime("%Y-%m-%d %H:%M"), poll_id)
            )
            conn.commit()
            # Rischedula
            scheduler.add_job(
                lambda: context.application.create_task(
                    pubblica_sondaggio(chat_id, question, options, context, poll_id, recurrence)
                ),
                'date',
                run_date=next_time
            )
        else:
            # Se non è ricorrente, cancella dal DB dopo invio
            cur.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
            conn.commit()
    except Exception as e:
        print("Errore nell'inviare sondaggio:", e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao! Usa /nuovosondaggio per programmare un sondaggio, anche ricorrente."
    )

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
    context.user_data['options'] = options
    await update.message.reply_text("Quando vuoi pubblicare il sondaggio? (esempio: 2025-04-29 15:30)")
    return DATETIME

async def ricevi_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        dt = datetime.strptime(update.message.text, "%Y-%m-%d %H:%M")
        context.user_data['dt'] = dt.strftime("%Y-%m-%d %H:%M")
        await update.message.reply_text("Vuoi che il sondaggio sia:\n- Senza ricorrenza\n- Giornaliera\n- Settimanale\n\nScrivi: nessuna, giornaliera, settimanale")
        return RECURRENCE
    except Exception as e:
        await update.message.reply_text("Formato data/ora non valido. Riprova (esempio: 2025-04-29 15:30)")
        return DATETIME

async def ricevi_ricorrenza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recurrence = update.message.text.strip().lower()
    if recurrence not in ["nessuna", "giornaliera", "settimanale"]:
        await update.message.reply_text("Rispondi: nessuna, giornaliera, o settimanale.")
        return RECURRENCE
    chat_id = update.message.chat_id
    question = context.user_data['question']
    options = context.user_data['options']
    dt = context.user_data['dt']
    # Salva nel DB
    cur.execute(
        "INSERT INTO polls (chat_id, question, options, schedule_time, recurrence) VALUES (?, ?, ?, ?, ?)",
        (chat_id, question, ",".join(options), dt, recurrence)
    )
    conn.commit()
    poll_id = cur.lastrowid
    # Programma la pubblicazione
    scheduler.add_job(
        lambda: context.application.create_task(
            pubblica_sondaggio(chat_id, question, options, context, poll_id, recurrence)
        ),
        'date',
        run_date=datetime.strptime(dt, "%Y-%m-%d %H:%M")
    )
    await update.message.reply_text(
        f"Sondaggio programmato per il {dt} con ricorrenza: {recurrence}."
    )
    return ConversationHandler.END

async def annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operazione annullata.")
    return ConversationHandler.END

def carica_sondaggi_precedenti(app):
    cur.execute("SELECT id, chat_id, question, options, schedule_time, recurrence FROM polls")
    for poll in cur.fetchall():
        poll_id, chat_id, question, options, schedule_time, recurrence = poll
        dt = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M")
        if dt > datetime.now():
            # Programma la pubblicazione
            scheduler.add_job(
                lambda chat_id=chat_id, question=question, options=options.split(','), poll_id=poll_id, recurrence=recurrence:
                    app.create_task(
                        pubblica_sondaggio(chat_id, question, options.split(','), app, poll_id, recurrence)
                    ),
                'date',
                run_date=dt
            )

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    # Al riavvio carica i sondaggi già programmati
    carica_sondaggi_precedenti(app)

    print("Bot in esecuzione... Premi CTRL+C per fermarlo.")
    app.run_polling()