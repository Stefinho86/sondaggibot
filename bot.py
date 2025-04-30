import os
import sqlite3
from datetime import datetime, timedelta
import logging
import re
import traceback

import pytz
import requests
from timezonefinder import TimezoneFinder

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ConversationHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from apscheduler.schedulers.background import BackgroundScheduler

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

SELECT_CITY, Q, OPTS, DT, REC, REC_DETAIL = range(6)
MOD_SELECT, MOD_Q, MOD_O, MOD_D, MOD_R, MOD_RD = range(10, 16)

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
logging.basicConfig(level=logging.INFO)
tf = TimezoneFinder()

def get_timezone_for_city(city):
    try:
        url = f"https://nominatim.openstreetmap.org/search?city={city}&format=json"
        r = requests.get(url, headers={"User-Agent": "TelegramPollBot/1.0"})
        res = r.json()
        if not res: return None, None
        lat, lon = float(res[0]['lat']), float(res[0]['lon'])
        tz = tf.timezone_at(lat=lat, lng=lon)
        return tz, (lat, lon)
    except: return None, None

def get_timezone_for_chat(chat_id):
    cur.execute("SELECT timezone FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else None

def save_chat_timezone(chat_id, city, timezone):
    cur.execute("INSERT OR REPLACE INTO chat_settings (chat_id, city, timezone) VALUES (?, ?, ?)", (chat_id, city, timezone))
    conn.commit()

def get_city_for_chat(chat_id):
    cur.execute("SELECT city FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else None

def run_async_job(coro):
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        asyncio.run(coro)

def parse_italian_datetime(input_str, tz_str):
    try:
        dt = datetime.strptime(input_str, "%d/%m/%Y %H.%M")
        tz = pytz.timezone(tz_str)
        local_dt = tz.localize(dt)
        utc_dt = local_dt.astimezone(pytz.utc)
        return utc_dt, local_dt
    except: return None, None

def parse_recurrence_detail(input_str, tz_str, first_dt_utc):
    input_str = input_str.lower().strip()
    # ogni X minuti/ore/secondi
    m = re.match(r"ogni\s+(\d+)\s*(minuti|minuto|min|m)", input_str)
    if m:
        minutes = int(m.group(1))
        return ("intervallo", timedelta(minutes=minutes), f"intervallo|{minutes}|minuti")
    m = re.match(r"ogni\s+(\d+)\s*(ore|ora|h)", input_str)
    if m:
        hours = int(m.group(1))
        return ("intervallo", timedelta(hours=hours), f"intervallo|{hours}|ore")
    m = re.match(r"ogni\s+(\d+)\s*(secondi|secondo|sec|s)", input_str)
    if m:
        seconds = int(m.group(1))
        return ("intervallo", timedelta(seconds=seconds), f"intervallo|{seconds}|secondi")
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
    # ogni lunedì/martedì... alle HH.MM
    weekdays = {
        "lunedì": 0, "lunedi": 0, "martedì": 1, "martedi": 1, "mercoledì": 2, "mercoledi": 2,
        "giovedì": 3, "giovedi": 3, "venerdì": 4, "venerdi": 4, "sabato": 5, "domenica": 6
    }
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
    return (None, None, None)

def remove_job_by_poll_id(poll_id):
    for job in scheduler.get_jobs():
        if hasattr(job, 'args') and len(job.args) > 0:
            coro = job.args[0]
            try:
                coro_poll_id = coro.cr_frame.f_locals.get('poll_id', None)
            except Exception:
                coro_poll_id = None
            if coro_poll_id == poll_id:
                job.remove()
                break

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_timezone_for_chat(chat_id)
    if not tz:
        if update.effective_chat.type == "private":
            await cambia_citta(update, context)
            return SELECT_CITY
        else:
            await update.message.reply_text(
                "Imposta la città per questo gruppo: scrivi il nome della città (esempio: Milano)."
            )
            return SELECT_CITY
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Bot pronto per {city}.\nComandi:\n/nuovosondaggio\n/sondaggi\n/cancella\n/modifica\n/cambia_citta\n/ora\n/debug"
    )

async def cambia_citta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        kb = [[KeyboardButton("📍 Invia posizione", request_location=True)]]
        await update.message.reply_text(
            "In che città vuoi impostare il bot?\n"
            "Puoi scrivere il nome (es: Milano) oppure inviare la tua posizione premendo qui sotto:",
            reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
        )
    else:
        await update.message.reply_text(
            "In che città vuoi impostare il bot per questo gruppo? Scrivi il nome della città (esempio: Milano)."
        )
    return SELECT_CITY

async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.message.location and update.effective_chat.type == "private":
        lat = update.message.location.latitude
        lon = update.message.location.longitude
        tz = tf.timezone_at(lat=lat, lng=lon)
        if not tz:
            await update.message.reply_text("Non sono riuscito a rilevare il fuso orario. Riprova o inserisci il nome della città.", reply_markup=ReplyKeyboardRemove())
            return SELECT_CITY
        try:
            nominatim_url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
            r = requests.get(nominatim_url, headers={"User-Agent": "TelegramPollBot/1.0"})
            data = r.json()
            city = data.get('address', {}).get('city') or data.get('address', {}).get('town') or data.get('address', {}).get('village') or "Località non nota"
        except Exception:
            city = "Località non nota"
        save_chat_timezone(chat_id, city, tz)
        await update.message.reply_text(
            f"Impostato: {city} ({tz})", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    else:
        city = update.message.text.strip()
        timezone_str, coords = get_timezone_for_city(city)
        if not timezone_str:
            await update.message.reply_text("Città non trovata! Riprova.", reply_markup=ReplyKeyboardRemove())
            return SELECT_CITY
        save_chat_timezone(chat_id, city.title(), timezone_str)
        await update.message.reply_text(
            f"Città impostata: {city.title()} ({timezone_str}).", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

async def ora_attuale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    if not tz_str:
        await update.message.reply_text("Prima imposta la città con /start o /cambia_citta")
        return
    utc_now = datetime.now(pytz.utc)
    tz = pytz.timezone(tz_str)
    local_now = utc_now.astimezone(tz)
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Ora locale {city} ({tz_str}): {local_now.strftime('%d/%m/%Y %H.%M')}\n"
        f"Ora UTC: {utc_now.strftime('%d/%m/%Y %H.%M')}"
    )

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_timezone_for_chat(chat_id)
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"chat_id: {chat_id}\nCittà: {city or 'Non impostata'}\nTimezone: {tz or 'Non impostato'}"
    )

async def lista_sondaggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cur.execute("SELECT id, question, schedule_time, recurrence, recurrence_detail FROM polls WHERE chat_id = ?", (chat_id,))
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("Non ci sono sondaggi programmati.")
        return
    msg = "Sondaggi programmati:\n"
    for i, r in enumerate(rows, 1):
        id, q, t, rec, det = r
        msg += f"{i}. {q} (ID: {id}) ({t})\n"
        if rec != "nessuna":
            msg += f"   Ricorrenza: {rec} ({det})\n"
    await update.message.reply_text(msg)

async def cancella_sondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cur.execute("SELECT id, question FROM polls WHERE chat_id = ?", (chat_id,))
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("Non ci sono sondaggi da cancellare.")
        return
    keyboard = [
        [InlineKeyboardButton(f"{row[1]} (ID: {row[0]})", callback_data=f"cancel_{row[0]}")]
        for row in rows
    ]
    await update.message.reply_text(
        "Seleziona il sondaggio da cancellare oppure invia l'ID:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cancella_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("cancel_"): return
    poll_id = int(data.split("_")[1])
    cur.execute("DELETE FROM polls WHERE id=?", (poll_id,))
    conn.commit()
    remove_job_by_poll_id(poll_id)
    await query.edit_message_text(f"Sondaggio cancellato.")

async def cancella_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if text.isdigit():
        poll_id = int(text)
        cur.execute("SELECT id FROM polls WHERE chat_id = ? AND id = ?", (chat_id, poll_id))
        if cur.fetchone():
            cur.execute("DELETE FROM polls WHERE id=?", (poll_id,))
            conn.commit()
            remove_job_by_poll_id(poll_id)
            await update.message.reply_text(f"Sondaggio cancellato.")
        else:
            await update.message.reply_text("ID non trovato.")
    return

async def modifica_sondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cur.execute("SELECT id, question FROM polls WHERE chat_id = ?", (chat_id,))
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("Non ci sono sondaggi da modificare.")
        return
    keyboard = [
        [InlineKeyboardButton(f"{row[1]} (ID: {row[0]})", callback_data=f"modify_{row[0]}")]
        for row in rows
    ]
    await update.message.reply_text(
        "Seleziona il sondaggio da modificare oppure invia l'ID:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def modifica_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("modify_"): return
    poll_id = int(data.split("_")[1])
    await _start_modifica2(update, context, poll_id)

async def modifica_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if text.isdigit():
        poll_id = int(text)
        cur.execute("SELECT id FROM polls WHERE chat_id = ? AND id = ?", (chat_id, poll_id))
        if cur.fetchone():
            await _start_modifica2(update, context, poll_id)
        else:
            await update.message.reply_text("ID non trovato.")

async def _start_modifica2(update_or_query, context, poll_id):
    chat_id = update_or_query.effective_chat.id
    cur.execute("SELECT question, options, schedule_time, recurrence, recurrence_detail FROM polls WHERE id=? AND chat_id=?", (poll_id, chat_id))
    row = cur.fetchone()
    if not row:
        await update_or_query.message.reply_text("Sondaggio non trovato.")
        return
    context.user_data['mod_poll_id'] = poll_id
    context.user_data['question'], options, schedule_time, recurrence, rec_detail = row
    context.user_data['options'] = options.split(',')
    context.user_data['dt'] = schedule_time
    context.user_data['recurrence'] = recurrence
    context.user_data['recurrence_detail'] = rec_detail
    await update_or_query.message.reply_text(
        f"Cosa vuoi modificare?\nRispondi: domanda, opzioni, data, ricorrenza, niente"
    )
    return MOD_SELECT

async def nuovosondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_timezone_for_chat(chat_id)
    if not tz:
        await update.message.reply_text("Prima imposta la città con /start o /cambia_citta")
        return
    await update.message.reply_text("Scrivi la domanda del sondaggio.")
    return Q

async def ricevi_domanda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['question'] = update.message.text
    await update.message.reply_text("Ora scrivi le opzioni separate da virgola (esempio: Sì,No,Forse)")
    return OPTS

async def ricevi_opzioni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [opt.strip() for opt in update.message.text.split(',')]
    if len(options) < 2:
        await update.message.reply_text("Servono almeno due opzioni!")
        return OPTS
    if len(options) > 10:
        await update.message.reply_text("Max 10 opzioni. Riprova.")
        return OPTS
    context.user_data['options'] = options
    chat_id = update.effective_chat.id
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Quando vuoi pubblicare il sondaggio?\n(Formato: GG/MM/AAAA HH.MM, orario di {city})"
    )
    return DT

async def ricevi_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    utc_dt, local_dt = parse_italian_datetime(update.message.text, tz_str)
    if not utc_dt:
        await update.message.reply_text("Formato data/ora non valido. Riprova (es: 30/04/2025 16.30)")
        return DT
    if utc_dt < datetime.now(pytz.utc):
        await update.message.reply_text("La data è nel passato. Riprova.")
        return DT
    context.user_data['dt'] = utc_dt.strftime("%Y-%m-%d %H:%M")
    context.user_data['local_dt'] = local_dt.strftime("%d/%m/%Y %H.%M")
    await update.message.reply_text(
        "Vuoi che il sondaggio sia ricorrente?\nScrivi:\n- no (solo una volta)\n- si"
    )
    return REC

async def ricevi_ask_ricorrenza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    risposta = update.message.text.strip().lower()
    if risposta == "no":
        context.user_data['recurrence'] = "nessuna"
        context.user_data['recurrence_detail'] = ""
        return await schedula_sondaggio(update, context)
    elif risposta == "si":
        await update.message.reply_text(
            "Ogni quanto?\nEsempi:\n- ogni 5 minuti\n- ogni giorno alle 18.30\n- ogni martedì alle 10.00"
        )
        return REC_DETAIL
    await update.message.reply_text("Rispondi: no oppure si.")
    return REC

async def ricevi_recurrence_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    dt = context.user_data['dt']
    first_dt_utc = datetime.strptime(dt, "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
    kind, interval_or_next, detail = parse_recurrence_detail(update.message.text, tz_str, first_dt_utc)
    if not kind or not interval_or_next:
        await update.message.reply_text(
            "Non capisco la ricorrenza. Esempi:\n- ogni 5 minuti\n- ogni giorno alle 18.30\n- ogni martedì alle 10.00"
        )
        return REC_DETAIL
    context.user_data['recurrence'] = kind
    context.user_data['recurrence_detail'] = detail
    if kind == "intervallo":
        pass
    else:
        context.user_data['dt'] = interval_or_next.strftime("%Y-%m-%d %H:%M")
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
    if recurrence == "intervallo":
        _, value, unit = recurrence_detail.split("|")
        value = int(value)
        if unit == "minuti":
            trigger_args = dict(minutes=value)
        elif unit == "ore":
            trigger_args = dict(hours=value)
        elif unit == "secondi":
            trigger_args = dict(seconds=value)
        else:
            trigger_args = dict(minutes=5)
        scheduler.add_job(
            run_async_job,
            'interval',
            start_date=datetime.strptime(dt, "%Y-%m-%d %H:%M"),
            id=f"poll_{poll_id}",
            args=(pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence, recurrence_detail),),
            **trigger_args
        )
    else:
        scheduler.add_job(
            run_async_job,
            'date',
            run_date=datetime.strptime(dt, "%Y-%m-%d %H:%M"),
            id=f"poll_{poll_id}",
            args=(pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence, recurrence_detail),)
        )
    city = get_city_for_chat(chat_id)
    await update.message.reply_text(
        f"Sondaggio programmato per il {local_dt} ({city})\nRicorrenza: {recurrence if recurrence != 'nessuna' else 'nessuna'}"
    )
    return ConversationHandler.END

async def annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operazione annullata.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence, recurrence_detail):
    try:
        await application.bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=False
        )
        if recurrence == "intervallo":
            pass
        else:
            next_time = None
            if recurrence == "giornaliera":
                _, hourmin = recurrence_detail.split("|")
                hour, minute = map(int, hourmin.split("."))
                tz_str = get_timezone_for_chat(chat_id)
                tz = pytz.timezone(tz_str)
                now_local = datetime.now(pytz.utc).astimezone(tz)
                candidate = now_local + timedelta(days=1)
                candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                next_time = candidate.astimezone(pytz.utc)
            elif recurrence == "settimanale":
                _, wd, hourmin = recurrence_detail.split("|")
                wd = int(wd)
                hour, minute = map(int, hourmin.split("."))
                tz_str = get_timezone_for_chat(chat_id)
                tz = pytz.timezone(tz_str)
                now_local = datetime.now(pytz.utc).astimezone(tz)
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
                    id=f"poll_{poll_id}",
                    args=(pubblica_sondaggio(chat_id, question, options, application, poll_id, recurrence, recurrence_detail),)
                )
            else:
                cur.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
                conn.commit()
    except Exception as e:
        traceback.print_exc()

def carica_sondaggi_precedenti(application):
    cur.execute("SELECT id, chat_id, question, options, schedule_time, recurrence, recurrence_detail FROM polls")
    for poll in cur.fetchall():
        poll_id, chat_id, question, options, schedule_time, recurrence, recurrence_detail = poll
        dt = datetime.strptime(schedule_time, "%Y-%m-%d %H:%M")
        if recurrence == "intervallo":
            _, value, unit = recurrence_detail.split("|")
            value = int(value)
            if unit == "minuti":
                trigger_args = dict(minutes=value)
            elif unit == "ore":
                trigger_args = dict(hours=value)
            elif unit == "secondi":
                trigger_args = dict(seconds=value)
            else:
                trigger_args = dict(minutes=5)
            scheduler.add_job(
                run_async_job,
                'interval',
                start_date=dt,
                id=f"poll_{poll_id}",
                args=(pubblica_sondaggio(chat_id, question, options.split(','), application, poll_id, recurrence, recurrence_detail),),
                **trigger_args
            )
        elif dt > datetime.now(pytz.utc):
            scheduler.add_job(
                run_async_job,
                'date',
                run_date=dt,
                id=f"poll_{poll_id}",
                args=(pubblica_sondaggio(chat_id, question, options.split(','), application, poll_id, recurrence, recurrence_detail),)
            )

async def mod_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt == "domanda":
        await update.message.reply_text("Scrivi la nuova domanda.")
        return MOD_Q
    elif txt == "opzioni":
        await update.message.reply_text("Scrivi le nuove opzioni separate da virgola.")
        return MOD_O
    elif txt == "data":
        await update.message.reply_text("Nuova data? (GG/MM/AAAA HH.MM)")
        return MOD_D
    elif txt == "ricorrenza":
        await update.message.reply_text("Vuoi che sia ricorrente? (si/no)")
        return MOD_R
    elif txt == "niente":
        await update.message.reply_text("Modifica annullata.")
        return ConversationHandler.END
    else:
        await update.message.reply_text("Rispondi: domanda, opzioni, data, ricorrenza, niente")
        return MOD_SELECT

async def mod_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['question'] = update.message.text
    await update.message.reply_text("Domanda aggiornata. Altra modifica? (domanda, opzioni, data, ricorrenza, niente)")
    return MOD_SELECT

async def mod_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = [opt.strip() for opt in update.message.text.split(',')]
    if len(options) < 2 or len(options) > 10:
        await update.message.reply_text("Numero opzioni non valido!")
        return MOD_O
    context.user_data['options'] = options
    await update.message.reply_text("Opzioni aggiornate. Altra modifica? (domanda, opzioni, data, ricorrenza, niente)")
    return MOD_SELECT

async def mod_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    utc_dt, local_dt = parse_italian_datetime(update.message.text, tz_str)
    if not utc_dt:
        await update.message.reply_text("Formato data/ora non valido. (GG/MM/AAAA HH.MM)")
        return MOD_D
    context.user_data['dt'] = utc_dt.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text("Data aggiornata. Altra modifica? (domanda, opzioni, data, ricorrenza, niente)")
    return MOD_SELECT

async def mod_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt == "no":
        context.user_data['recurrence'] = "nessuna"
        context.user_data['recurrence_detail'] = ""
        await update.message.reply_text("Ricorrenza rimossa. Altra modifica? (domanda, opzioni, data, ricorrenza, niente)")
        return MOD_SELECT
    elif txt == "si":
        await update.message.reply_text("Ogni quanto? (es: ogni 5 minuti, ogni giorno alle 18.30, ogni martedì alle 10.00)")
        return MOD_RD
    else:
        await update.message.reply_text("Rispondi si oppure no.")
        return MOD_R

async def mod_rec_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz_str = get_timezone_for_chat(chat_id)
    dt = context.user_data['dt']
    first_dt_utc = datetime.strptime(dt, "%Y-%m-%d %H:%M").replace(tzinfo=pytz.utc)
    kind, interval_or_next, detail = parse_recurrence_detail(update.message.text, tz_str, first_dt_utc)
    if not kind or not interval_or_next:
        await update.message.reply_text("Non capisco la ricorrenza.")
        return MOD_RD
    context.user_data['recurrence'] = kind
    context.user_data['recurrence_detail'] = detail
    if kind == "intervallo":
        pass
    else:
        context.user_data['dt'] = interval_or_next.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text("Ricorrenza aggiornata. Altra modifica? (domanda, opzioni, data, ricorrenza, niente)")
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
    if recurrence == "intervallo":
        _, value, unit = recurrence_detail.split("|")
        value = int(value)
        if unit == "minuti":
            trigger_args = dict(minutes=value)
        elif unit == "ore":
            trigger_args = dict(hours=value)
        elif unit == "secondi":
            trigger_args = dict(seconds=value)
        else:
            trigger_args = dict(minutes=5)
        scheduler.add_job(
            run_async_job,
            'interval',
            start_date=datetime.strptime(dt, "%Y-%m-%d %H:%M"),
            id=f"poll_{poll_id}",
            args=(pubblica_sondaggio(update.effective_chat.id, question, options, application, poll_id, recurrence, recurrence_detail),),
            **trigger_args
        )
    else:
        scheduler.add_job(
            run_async_job,
            'date',
            run_date=datetime.strptime(dt, "%Y-%m-%d %H:%M"),
            id=f"poll_{poll_id}",
            args=(pubblica_sondaggio(update.effective_chat.id, question, options, application, poll_id, recurrence, recurrence_detail),)
        )
    await update.message.reply_text("Sondaggio aggiornato!")
    return ConversationHandler.END

if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).build()
    async def set_my_commands(app):
        commands = [
            BotCommand("start", "Avvia o cambia città"),
            BotCommand("nuovosondaggio", "Crea un nuovo sondaggio"),
            BotCommand("sondaggi", "Sondaggi programmati"),
            BotCommand("cancella", "Cancella un sondaggio"),
            BotCommand("modifica", "Modifica un sondaggio"),
            BotCommand("cambia_citta", "Cambia la città/fuso orario"),
            BotCommand("ora", "Orario locale"),
            BotCommand("debug", "Info chat"),
        ]
        await app.bot.set_my_commands(commands)
    application.post_init = set_my_commands

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('nuovosondaggio', nuovosondaggio)],
        states={
            Q: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_domanda)],
            OPTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_opzioni)],
            DT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_data)],
            REC: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_ask_ricorrenza)],
            REC_DETAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_recurrence_detail)],
        },
        fallbacks=[CommandHandler('annulla', annulla)],
    )

    mod_conv = ConversationHandler(
        entry_points=[MessageHandler((filters.Regex(r"^\d+$") | filters.COMMAND) & filters.ChatType.PRIVATE, modifica_id)],
        states={
            MOD_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_select)],
            MOD_Q: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_question)],
            MOD_O: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_options)],
            MOD_D: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_datetime)],
            MOD_R: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_rec)],
            MOD_RD: [MessageHandler(filters.TEXT & ~filters.COMMAND, mod_rec_detail)],
        },
        fallbacks=[MessageHandler(filters.TEXT & ~filters.COMMAND, mod_end)],
    )

    city_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('cambia_citta', cambia_citta)
        ],
        states={
            SELECT_CITY: [MessageHandler(filters.ALL, set_city)],
        },
        fallbacks=[CommandHandler('annulla', annulla)],
    )

    application.add_handler(city_conv_handler)
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("ora", ora_attuale))
    application.add_handler(CommandHandler("sondaggi", lista_sondaggi))

    application.add_handler(CommandHandler("cancella", cancella_sondaggio))
    application.add_handler(CallbackQueryHandler(cancella_callback, pattern="^cancel_"))
    application.add_handler(MessageHandler(filters.Regex(r"^\d+$") & filters.TEXT, cancella_id))

    application.add_handler(CommandHandler("modifica", modifica_sondaggio))
    application.add_handler(CallbackQueryHandler(modifica_callback, pattern="^modify_"))
    application.add_handler(MessageHandler(filters.Regex(r"^\d+$") & filters.TEXT, modifica_id))

    application.add_handler(conv_handler)
    application.add_handler(mod_conv)

    carica_sondaggi_precedenti(application)
    application.run_polling()