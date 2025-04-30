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
import re

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Conversation states
SELECT_CITY, QUESTION, OPTIONS, DATETIME, ASK_RECURRENCE, RECURRENCE_DETAIL = range(6)
MOD_SELECT, MOD_QUESTION, MOD_OPTIONS, MOD_DATETIME, MOD_REC, MOD_REC_DETAIL = range(10, 16)

# DB Setup
conn = sqlite3.connect('sondaggi.db', check_same_thread=False)
cur = conn.cursor()
cur.execute('''CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    question TEXT,
    options TEXT,
    schedule_time TEXT,
    recurrence TEXT,
    recurrence_detail TEXT
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

def parse_italian_datetime(input_str, tz_str):
    """
    Accetta stringa nel formato GG/MM/AAAA HH.MM e restituisce (utc_datetime, local_datetime)
    """
    try:
        dt = datetime.strptime(input_str, "%d/%m/%Y %H.%M")
        tz = pytz.timezone(tz_str)
        local_dt = tz.localize(dt)
        utc_dt = local_dt.astimezone(pytz.utc)
        return utc_dt, local_dt
    except Exception as e:
        return None, None

def parse_recurrence_detail(input_str, tz_str, first_dt_utc):
    """
    Interpreta input come 'ogni martedì alle 10.00', 'ogni giorno alle 18.30'
    Restituisce (tipo, next_run_utc, recurrence_detail)
    """
    input_str = input_str.lower().strip()
    weekdays = {
        "lunedì": 0, "lunedi": 0, "martedì": 1, "martedi": 1, "mercoledì": 2, "mercoledi": 2,
        "giovedì": 3, "giovedi": 3, "venerdì": 4, "venerdi": 4, "sabato": 5, "domenica": 6
    }
    # ogni giorno alle HH.MM
    m = re.match(r"ogni giorno alle (\d{1,2})[.:](\d{2})", input_str)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        tz = pytz.timezone(tz_str)
        now_local = first_dt_utc.astimezone(tz)
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        next_run_utc = candidate.astimezone(pytz.utc)
        return ("giornaliera", next_run_utc, f"giornaliera|{hour:02d}.{minute:02d}")
    # ogni [giorno della settimana] alle HH.MM
    m = re.match(r"ogni (\w+) alle (\d{1,2})[.:](\d{2})", input_str)
    if m and m.group(1) in weekdays:
        wd = weekdays[m.group(1)]
        hour, minute = int(m.group(2)), int(m.group(3))
        tz = pytz.timezone(tz_str)
        now_local = first_dt_utc.astimezone(tz)
        days_ahead = (wd - now_local.weekday() + 7) % 7
        candidate = now_local + timedelta(days=days_ahead)
        candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(weeks=1)
        next_run_utc = candidate.astimezone(pytz.utc)
        return ("settimanale", next_run_utc, f"settimanale|{wd}|{hour:02d}.{minute:02d}")
    # fallback: errore
    return (None, None, None)

def remove_job_by_poll_id(poll_id):
    # Cerca e rimuove il job APScheduler associato al poll_id
    for job in scheduler.get_jobs():
        if hasattr(job, 'args') and len(job.args) > 0:
            coro = job.args[0]
            # Il poll_id è sempre il 5° parametro della coroutine
            try:
                coro_poll_id = coro.cr_frame.f_locals.get('poll_id', None)
            except Exception:
                coro_poll_id = None
            if coro_poll_id == poll_id:
                job.remove()
                break

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
            "Comandi utili: /debug /ora /sondaggi /cancella <id> /modifica <id>"
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
        f"Ora locale {city} ({tz_str}): {local_now.strftime('%d/%m/%Y %H.%M')}\n"
        f"Ora UTC: {utc_now.strftime('%d/%m/%Y %H.%M')}"
    )

async def lista_sondaggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cur.execute("SELECT id, question, schedule_time, recurrence, recurrence_detail FROM polls WHERE chat_id = ?", (chat_id,))
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("Non ci sono sondaggi programmati.")
        return
    msg = "Sondaggi programmati:\n"
    for r in rows:
        id, q, t, rec, det = r
        msg += f"ID: {id} | {t} | {q}\n"
        if rec != "nessuna":
            msg += f"   Ricorrenza: {rec} ({det})\n"
    await update.message.reply_text(msg)

async def cancella_sondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usa: /cancella <id>")
        return
    poll_id = int(context.args[0])
    cur.execute("SELECT id FROM polls WHERE id=? AND chat_id=?", (poll_id, chat_id))
    row = cur.fetchone()
    if not row:
        await update.message.reply_text("Sondaggio non trovato.")
        return
    cur.execute("DELETE FROM polls WHERE id=?", (poll_id,))
    conn.commit()
    remove_job_by_poll_id(poll_id)
    await update.message.reply_text(f"Sondaggio ID {poll_id} cancellato.")

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
        f"(Formato: GG/MM/AAAA HH.MM, orario LOCALE di {city})"
    )
    return DATETIME

async def ricevi_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    if not tz_str:
        await update.message.reply_text("Prima imposta la città con /start")
        return ConversationHandler.END
    utc_dt, local_dt = parse_italian_datetime(update.message.text, tz_str)
    if not utc_dt:
        await update.message.reply_text(
            "Formato data/ora non valido. Riprova (esempio: 30/04/2025 16.30)"
        )
        return DATETIME
    if utc_dt < datetime.utcnow().replace(tzinfo=pytz.utc):
        await update.message.reply_text("La data è nel passato. Riprova.")
        return DATETIME
    context.user_data['dt'] = utc_dt.strftime("%Y-%m-%d %H:%M")
    context.user_data['local_dt'] = local_dt.strftime("%d/%m/%Y %H.%M")
    await update.message.reply_text(
        "Vuoi che il sondaggio sia pubblicato ricorrentemente?\n"
        "Scrivi:\n"
        "- no (pubblica solo una volta)\n"
        "- si (richiederà dettagli dopo)"
    )
    return ASK_RECURRENCE

async def ricevi_ask_ricorrenza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    risposta = update.message.text.strip().lower()
    if risposta == "no":
        context.user_data['recurrence'] = "nessuna"
        context.user_data['recurrence_detail'] = ""
        return await schedula_sondaggio(update, context)
    elif risposta == "si":
        await update.message.reply_text(
            "Ogni quanto deve essere pubblicato il sondaggio?\n"
            "Esempi:\n"
            "- ogni giorno alle 18.30\n"
            "- ogni martedì alle 10.00"
        )
        return RECURRENCE_DETAIL
    else:
        await update.message.reply_text("Rispondi: no oppure si.")
        return ASK_RECURRENCE

async def ricevi_recurrence_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    dt = context.user_data['dt']
    first_dt_utc = datetime.strptime(dt, "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
    kind, next_run_utc, detail = parse_recurrence_detail(update.message.text, tz_str, first_dt_utc)
    if not kind or not next_run_utc:
        await update.message.reply_text(
            "Non riesco a capire la ricorrenza. Esempi validi:\n"
            "- ogni giorno alle 18.30\n"
            "- ogni martedì alle 10.00"
        )
        return RECURRENCE_DETAIL
    context.user_data['recurrence'] = kind
    context.user_data['recurrence_detail'] = detail
    context.user_data['dt'] = next_run_utc.strftime("%Y-%m-%d %H:%M")
    return await schedula_sondaggio(update, context)

async def schedula_sondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    question = context.user_data['question']
    options = context.user_data['options']
    dt = context.user_data['dt']
    local_dt = context.user_data['local_dt']
    recurrence = context.user_data.get('recurrence', "nessuna")
    recurrence_detail = context.user_data.get('recurrence_detail', "")
    application = context.application

    cur.execute(
        "INSERT INTO polls (chat_id, question, options, schedule_time, recurrence, recurrence_detail) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, question, ",".join(options), dt, recurrence, recurrence_detail)
    )
    conn.commit()
    poll_id = cur.lastrowid

    scheduler.add_job(
        run_async_job,
        'date',
        run_date=datetime.strptime(dt, "%Y-%m-%d %H:%M"),
        args=(pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence, recurrence_detail),)
    )
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Sondaggio programmato per il {local_dt} ({city}) "
        f"(UTC: {dt}) con ricorrenza: {recurrence if recurrence != 'nessuna' else 'nessuna'}.\n"
        f"ATTENZIONE: il bot deve essere amministratore del gruppo e poter inviare sondaggi!"
    )
    print(f"--- SCHEDULATO sondaggio per chat_id={chat_id} | Domanda: {question} | Opzioni: {options} | Data(UTC): {dt} | Ricorrenza: {recurrence} | Dettaglio: {recurrence_detail}", flush=True)
    logger.info(f"Sondaggio schedulato per chat_id={chat_id} | Domanda: {question} | Opzioni: {options} | Data: {dt} | Ricorrenza: {recurrence} | Dettaglio: {recurrence_detail}")
    return ConversationHandler.END

async def annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operazione annullata.")
    return ConversationHandler.END

async def pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence, recurrence_detail):
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
        next_time = None
        if recurrence == "giornaliera":
            _, hourmin = recurrence_detail.split("|")
            hour, minute = map(int, hourmin.split("."))
            tz_str = get_timezone_for_chat(chat_id)
            tz = pytz.timezone(tz_str)
            now_local = datetime.utcnow().astimezone(tz)
            candidate = now_local + timedelta(days=1)
            candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            next_time = candidate.astimezone(pytz.utc)
        elif recurrence == "settimanale":
            _, wd, hourmin = recurrence_detail.split("|")
            wd = int(wd)
            hour, minute = map(int, hourmin.split("."))
            tz_str = get_timezone_for_chat(chat_id)
            tz = pytz.timezone(tz_str)
            now_local = datetime.utcnow().astimezone(tz)
            days_ahead = (wd - now_local.weekday() + 7) % 7
            candidate = now_local + timedelta(days=days_ahead)
            candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now_local:
                candidate += timedelta(weeks=1)
            next_time = candidate.astimezone(pytz.utc)
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
                args=(pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence, recurrence_detail),)
            )
        else:
            cur.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
            conn.commit()
    except Exception as e:
        print(f"--- ERRORE invio sondaggio: {e}", flush=True)
        logger.error(f"Errore nell'inviare sondaggio a chat_id={chat_id}: {e}")
        traceback.print_exc()

def carica_sondaggi_precedenti(application):
    cur.execute("SELECT id, chat_id, question, options, schedule_time, recurrence, recurrence_detail FROM polls")
    for poll in cur.fetchall():
        poll_id, chat_id, question, options, schedule_time, recurrence, recurrence_detail = poll
        dt = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M")
        if dt > datetime.utcnow():
            scheduler.add_job(
                run_async_job,
                'date',
                run_date=dt,
                args=(pubblica_sondaggio(chat_id, question, options.split(','), application, poll_id, recurrence, recurrence_detail),)
            )
            print(f"--- RIPRISTINATO sondaggio per chat_id={chat_id} | Domanda: {question} | Data: {schedule_time} | Ricorrenza: {recurrence} | Dettaglio: {recurrence_detail}", flush=True)
            logger.info(f"Sondaggio ripristinato per chat_id={chat_id} | Domanda: {question} | Data: {schedule_time} | Ricorrenza: {recurrence} | Dettaglio: {recurrence_detail}")

# --- MODIFICA SONDAGGI ---

async def modifica_sondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usa: /modifica <id>")
        return ConversationHandler.END
    poll_id = int(context.args[0])
    cur.execute("SELECT question, options, schedule_time, recurrence, recurrence_detail FROM polls WHERE id=? AND chat_id=?", (poll_id, chat_id))
    row = cur.fetchone()
    if not row:
        await update.message.reply_text("Sondaggio non trovato.")
        return ConversationHandler.END
    context.user_data['mod_poll_id'] = poll_id
    context.user_data['question'], options, schedule_time, recurrence, rec_detail = row
    context.user_data['options'] = options.split(',')
    context.user_data['dt'] = schedule_time
    context.user_data['recurrence'] = recurrence
    context.user_data['recurrence_detail'] = rec_detail
    await update.message.reply_text(
        f"Modifica sondaggio ID {poll_id}. Che cosa vuoi modificare?\n"
        "Rispondi: domanda, opzioni, data, ricorrenza, niente"
    )
    return MOD_SELECT

async def mod_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt == "domanda":
        await update.message.reply_text("Scrivi la nuova domanda.")
        return MOD_QUESTION
    elif txt == "opzioni":
        await update.message.reply_text("Scrivi le nuove opzioni separate da virgola.")
        return MOD_OPTIONS
    elif txt == "data":
        await update.message.reply_text("Nuova data? (GG/MM/AAAA HH.MM)")
        return MOD_DATETIME
    elif txt == "ricorrenza":
        await update.message.reply_text("Vuoi che sia ricorrente? (si/no)")
        return MOD_REC
    elif txt == "niente":
        await update.message.reply_text("Modifica annullata.")
        return ConversationHandler.END
    else:
        await update.message.reply_text("Rispondi: domanda, opzioni, data, ricorrenza, niente")
        return MOD_SELECT

async def mod_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['question'] = update.message.text
    await update.message.reply_text("OK, domanda aggiornata. Vuoi modificare altro? (domanda, opzioni, data, ricorrenza, niente)")
    return MOD_SELECT

async def mod_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [opt.strip() for opt in update.message.text.split(',')]
    if len(options) < 2 or len(options) > 10:
        await update.message.reply_text("Numero opzioni non valido!")
        return MOD_OPTIONS
    context.user_data['options'] = options
    await update.message.reply_text("OK, opzioni aggiornate. Vuoi modificare altro? (domanda, opzioni, data, ricorrenza, niente)")
    return MOD_SELECT

async def mod_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    utc_dt, local_dt = parse_italian_datetime(update.message.text, tz_str)
    if not utc_dt:
        await update.message.reply_text("Formato data/ora non valido. (GG/MM/AAAA HH.MM)")
        return MOD_DATETIME
    context.user_data['dt'] = utc_dt.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text("OK, data aggiornata. Vuoi modificare altro? (domanda, opzioni, data, ricorrenza, niente)")
    return MOD_SELECT

async def mod_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt == "no":
        context.user_data['recurrence'] = "nessuna"
        context.user_data['recurrence_detail'] = ""
        await update.message.reply_text("OK, ricorrenza rimossa. Vuoi modificare altro? (domanda, opzioni, data, ricorrenza, niente)")
        return MOD_SELECT
    elif txt == "si":
        await update.message.reply_text("Ogni quanto? (es: ogni giorno alle 18.30, ogni martedì alle 10.00)")
        return MOD_REC_DETAIL
    else:
        await update.message.reply_text("Rispondi si oppure no.")
        return MOD_REC

async def mod_rec_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    dt = context.user_data['dt']
    first_dt_utc = datetime.strptime(dt, "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
    kind, next_run_utc, detail = parse_recurrence_detail(update.message.text, tz_str, first_dt_utc)
    if not kind or not next_run_utc:
        await update.message.reply_text("Non riesco a capire la ricorrenza.")
        return MOD_REC_DETAIL
    context.user_data['recurrence'] = kind
    context.user_data['recurrence_detail'] = detail
    context.user_data['dt'] = next_run_utc.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text("OK, ricorrenza aggiornata. Vuoi modificare altro? (domanda, opzioni, data, ricorrenza, niente)")
    return MOD_SELECT

async def mod_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll_id = context.user_data['mod_poll_id']
    question = context.user_data['question']
    options = context.user_data['options']
    dt = context.user_data['dt']
    recurrence = context.user_data.get('recurrence', "nessuna")
    recurrence_detail = context.user_data.get('recurrence_detail', "")
    cur.execute(
        "UPDATE polls SET question=?, options=?, schedule_time=?, recurrence=?, recurrence_detail=? WHERE id=?",
        (question, ",".join(options), dt, recurrence, recurrence_detail, poll_id)
    )
    conn.commit()
    remove_job_by_poll_id(poll_id)
    application = context.application
    scheduler.add_job(
        run_async_job,
        'date',
        run_date=datetime.strptime(dt, "%Y-%m-%d %H:%M"),
        args=(pubblica_sondaggio(update.effective_chat.id, question, options, application, poll_id, recurrence, recurrence_detail),)
    )
    await update.message.reply_text("Sondaggio aggiornato!")
    return ConversationHandler.END

if __name__ == "__main__":
    print("Sto avviando il BOT! Versione con gestione cancella e modifica!", flush=True)

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
            ASK_RECURRENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_ask_ricorrenza)],
            RECURRENCE_DETAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_recurrence_detail)],
        },
        fallbacks=[CommandHandler('annulla', annulla)],
    )

    mod_conv = ConversationHandler(
        entry_points=[CommandHandler('modifica', modifica_sondaggio)],
        states={
            MOD_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_select)],
            MOD_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_question)],
            MOD_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_options)],
            MOD_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_datetime)],
            MOD_REC: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_rec)],
            MOD_REC_DETAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_rec_detail)],
        },
        fallbacks=[MessageHandler(filters.TEXT & ~filters.COMMAND, mod_end)],
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
    application.add_handler(CommandHandler("sondaggi", lista_sondaggi))
    application.add_handler(CommandHandler("cancella", cancella_sondaggio))
    application.add_handler(conv_handler)
    application.add_handler(mod_conv)

    carica_sondaggi_precedenti(application)

    print("Bot in esecuzione... Premi CTRL+C per fermarlo.", flush=True)
    application.run_polling()