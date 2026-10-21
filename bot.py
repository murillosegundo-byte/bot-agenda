import os
import datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ================= CONFIGURACIÓN =================
TELEGRAM_TOKEN = "8792380999:AAGR6XRD4Rkv6rD2d3_c6P00UpHMx10QLiQ"
SCOPES = ['https://www.googleapis.com/auth/calendar']
TIMEZONE_STR = 'America/Bogota'
LOCAL_TZ = pytz.timezone(TIMEZONE_STR)

USUARIO_CHAT_ID = None
EVENTOS_NOTIFICADOS = set()

# Estados para la creación guiada de eventos
TITULO, FECHA, HORA, DURACION = range(4)
# =================================================

def get_calendar_service():
    """Autentica con Google Calendar usando token.json."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

# ----------------- MENÚ PRINCIPAL -----------------

def obtener_teclado_principal():
    teclado = [
        [InlineKeyboardButton("☀️ Ver Agenda de Hoy", callback_data="btn_hoy")],
        [InlineKeyboardButton("📅 Ver Próximos 7 Días", callback_data="btn_semana")],
        [InlineKeyboardButton("➕ Nuevo Evento Puntual", callback_data="btn_crear")],
        [InlineKeyboardButton("🔁 Nuevo Evento Diario", callback_data="btn_diario")]
    ]
    return InlineKeyboardMarkup(teclado)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global USUARIO_CHAT_ID
    USUARIO_CHAT_ID = update.effective_chat.id

    texto = (
        "👋 *Bienvenido a tu Asistente Personal de Agenda*\n\n"
        "Selecciona una opción del menú:"
    )
    if update.message:
        await update.message.reply_text(texto, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(texto, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")

# ----------------- ACCIONES DEL MENÚ -----------------

async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_hoy":
        await mostrar_agenda_hoy(query)
    elif query.data == "btn_semana":
        await mostrar_agenda_semana(query)
    elif query.data == "btn_volver":
        await query.edit_message_text("Selecciona una opción del menú:", reply_markup=obtener_teclado_principal())

async def mostrar_agenda_hoy(query):
    try:
        service = get_calendar_service()
        ahora = datetime.datetime.now(LOCAL_TZ)
        inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        fin_dia = ahora.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        res = service.events().list(
            calendarId='primary',
            timeMin=inicio_dia,
            timeMax=fin_dia,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        eventos = res.get('items', [])
        teclado = [[InlineKeyboardButton("⬅️ Volver al Menú", callback_data="btn_volver")]]
        
        if not eventos:
            await query.edit_message_text("🌴 No tienes actividades agendadas para hoy.", reply_markup=InlineKeyboardMarkup(teclado))
            return

        texto = f"📅 *Tu Agenda para Hoy ({ahora.strftime('%d/%m/%Y')}):*\n\n"
        for ev in eventos:
            start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
            if 'T' in start_raw:
                dt = datetime.datetime.fromisoformat(start_raw)
                hora = dt.strftime('%I:%M %p')
            else:
                hora = "Todo el día"
            texto += f"▫️ *{hora}* - {ev.get('summary', 'Sin título')}\n"

        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al consultar agenda: {e}")

async def mostrar_agenda_semana(query):
    try:
        service = get_calendar_service()
        ahora = datetime.datetime.now(LOCAL_TZ)
        limite = ahora + datetime.timedelta(days=7)

        res = service.events().list(
            calendarId='primary',
            timeMin=ahora.isoformat(),
            timeMax=limite.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        eventos = res.get('items', [])
        teclado = [[InlineKeyboardButton("⬅️ Volver al Menú", callback_data="btn_volver")]]

        if not eventos:
            await query.edit_message_text("No tienes eventos en los próximos 7 días.", reply_markup=InlineKeyboardMarkup(teclado))
            return

        texto = "📅 *Eventos para los próximos 7 días:*\n\n"
        for ev in eventos:
            start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
            if 'T' in start_raw:
                dt = datetime.datetime.fromisoformat(start_raw)
                fecha_hora = dt.strftime('%d/%m (%I:%M %p)')
            else:
                fecha_hora = f"{start_raw} (Todo el día)"
            texto += f"• *{fecha_hora}* : {ev.get('summary', 'Sin título')}\n"

        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {e}")

# ------------- FLUJO PASO A PASO PARA CREAR EVENTOS -------------

async def iniciar_creacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    es_diario = (query.data == "btn_diario")
    context.user_data['es_diario'] = es_diario

    tipo_texto = "recurrente diario" if es_diario else "puntual"
    await query.message.reply_text(
        f"✍️ Vamos a crear un evento *{tipo_texto}*.\n\n"
        "Escribe el *título o nombre* del evento (o envía /cancelar para salir):",
        parse_mode="Markdown"
    )
    return TITULO

async def recibir_titulo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['titulo'] = update.message.text.strip()

    if context.user_data.get('es_diario'):
        await update.message.reply_text(
            "🕒 ¿A qué hora debe comenzar todos los días?\n\n"
            "Ejemplos: `07:30`, `14:00`, `20:15`",
            parse_mode="Markdown"
        )
        return HORA

    ahora = datetime.datetime.now(LOCAL_TZ)
    hoy_str = ahora.strftime("%Y-%m-%d")
    manana_str = (ahora + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    teclado = [
        [InlineKeyboardButton(f"Hoy ({ahora.strftime('%d/%m')})", callback_data=f"fecha_{hoy_str}")],
        [InlineKeyboardButton(f"Mañana ({(ahora + datetime.timedelta(days=1)).strftime('%d/%m')})", callback_data=f"fecha_{manana_str}")],
    ]
    await update.message.reply_text(
        "📅 Elige la fecha o escríbela en formato `DD/MM/AAAA` (por ej: `22/10/2026`):",
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )
    return FECHA

async def recibir_fecha_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fecha_sel = query.data.replace("fecha_", "")
    context.user_data['fecha'] = fecha_sel

    await query.message.reply_text(
        "🕒 ¿A qué hora comienza el evento?\n\n"
        "Ejemplos: `08:00`, `15:30`, `19:00` (formato 24 horas)",
        parse_mode="Markdown"
    )
    return HORA

async def recibir_fecha_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        dt = datetime.datetime.strptime(texto, "%d/%m/%Y")
        context.user_data['fecha'] = dt.strftime("%Y-%m-%d")
        await update.message.reply_text(
            "🕒 ¿A qué hora comienza el evento?\n\n"
            "Ejemplos: `08:00`, `15:30`, `19:00`",
            parse_mode="Markdown"
        )
        return HORA
    except ValueError:
        await update.message.reply_text("⚠️ Fecha inválida. Usa el formato `DD/MM/AAAA` (por ejemplo: `22/10/2026`):", parse_mode="Markdown")
        return FECHA

async def recibir_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hora_texto = update.message.text.strip()
    try:
        datetime.datetime.strptime(hora_texto, "%H:%M")
        context.user_data['hora'] = hora_texto
    except ValueError:
        await update.message.reply_text("⚠️ Hora no válida. Escribe la hora en formato HH:MM (por ej: `08:30` o `17:00`):", parse_mode="Markdown")
        return HORA

    teclado = [
        [InlineKeyboardButton("30 min", callback_data="dur_30"), InlineKeyboardButton("45 min", callback_data="dur_45")],
        [InlineKeyboardButton("1 hora", callback_data="dur_60"), InlineKeyboardButton("2 horas", callback_data="dur_120")],
        [InlineKeyboardButton("Todo el día", callback_data="dur_allday")]
    ]
    await update.message.reply_text("⏳ ¿Cuánto dura la actividad?", reply_markup=InlineKeyboardMarkup(teclado))
    return DURACION

async def finalizar_creacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    dur_str = query.data.replace("dur_", "")
    titulo = context.user_data['titulo']
    hora_str = context.user_data['hora']
    es_diario = context.user_data.get('es_diario', False)

    h, m = map(int, hora_str.split(':'))
    service = get_calendar_service()

    try:
        if es_diario:
            ahora = datetime.datetime.now(LOCAL_TZ)
            duracion = 60 if dur_str == "allday" else int(dur_str)
            inicio_dt = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
            fin_dt = inicio_dt + datetime.timedelta(minutes=duracion)

            cuerpo = {
                'summary': titulo,
                'start': {'dateTime': inicio_dt.isoformat(), 'timeZone': TIMEZONE_STR},
                'end': {'dateTime': fin_dt.isoformat(), 'timeZone': TIMEZONE_STR},
                'recurrence': ['RRULE:FREQ=DAILY']
            }
            service.events().insert(calendarId='primary', body=cuerpo).execute()
            await query.edit_message_text(
                f"✅ *Evento Diario Creado*\n\n📌 *{titulo}*\n⏰ Todos los días a las {hora_str} ({duracion} min)",
                reply_markup=obtener_teclado_principal(),
                parse_mode="Markdown"
            )
        else:
            fecha_str = context.user_data['fecha']
            inicio_dt = datetime.datetime.strptime(f"{fecha_str} {hora_str}", "%Y-%m-%d %H:%M")
            inicio_dt = LOCAL_TZ.localize(inicio_dt)

            if dur_str == "allday":
                cuerpo = {
                    'summary': titulo,
                    'start': {'date': fecha_str},
                    'end': {'date': fecha_str},
                }
                detalle_dur = "Todo el día"
            else:
                duracion = int(dur_str)
                fin_dt = inicio_dt + datetime.timedelta(minutes=duracion)
                cuerpo = {
                    'summary': titulo,
                    'start': {'dateTime': inicio_dt.isoformat(), 'timeZone': TIMEZONE_STR},
                    'end': {'dateTime': fin_dt.isoformat(), 'timeZone': TIMEZONE_STR},
                }
                detalle_dur = f"{duracion} min"

            service.events().insert(calendarId='primary', body=cuerpo).execute()
            await query.edit_message_text(
                f"✅ *Evento Agendado con Éxito*\n\n📌 *{titulo}*\n📅 {fecha_str}\n🕒 {hora_str} ({detalle_dur})",
                reply_markup=obtener_teclado_principal(),
                parse_mode="Markdown"
            )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al guardar en Google Calendar: {e}", reply_markup=obtener_teclado_principal())

    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operación cancelada.", reply_markup=obtener_teclado_principal())
    return ConversationHandler.END

# ----------------- RECORDATORIOS AUTOMÁTICOS -----------------

async def verificar_recordatorios(context: ContextTypes.DEFAULT_TYPE):
    global USUARIO_CHAT_ID, EVENTOS_NOTIFICADOS
    if not USUARIO_CHAT_ID:
        return

    try:
        service = get_calendar_service()
        ahora = datetime.datetime.now(LOCAL_TZ)
        en_15_min = ahora + datetime.timedelta(minutes=15)

        res = service.events().list(
            calendarId='primary',
            timeMin=ahora.isoformat(),
            timeMax=en_15_min.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        for ev in res.get('items', []):
            ev_id = ev['id']
            if ev_id in EVENTOS_NOTIFICADOS:
                continue

            start_raw = ev['start'].get('dateTime')
            if start_raw:
                dt_evento = datetime.datetime.fromisoformat(start_raw)
                minutos = int((dt_evento - ahora).total_seconds() / 60)
                if 0 <= minutos <= 10:
                    EVENTOS_NOTIFICADOS.add(ev_id)
                    mensaje = (
                        f"⏰ *RECORDATORIO DE TU AGENDA*\n\n"
                        f"📌 *{ev.get('summary', 'Actividad')}*\n"
                        f"🕒 Comienza en aprox. *{minutos} minutos* ({dt_evento.strftime('%I:%M %p')})"
                    )
                    await context.bot.send_message(chat_id=USUARIO_CHAT_ID, text=mensaje, parse_mode="Markdown")
    except Exception:
        pass

# ----------------- INICIO -----------------

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(iniciar_creacion, pattern="^(btn_crear|btn_diario)$")
        ],
        states={
            TITULO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_titulo)],
            FECHA: [
                CallbackQueryHandler(recibir_fecha_boton, pattern="^fecha_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_fecha_texto)
            ],
            HORA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_hora)],
            DURACION: [CallbackQueryHandler(finalizar_creacion, pattern="^dur_")]
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(menu_callback_handler))

    if app.job_queue:
        app.job_queue.run_repeating(verificar_recordatorios, interval=60, first=10)

    print("🤖 Bot de Agenda con menú interactivo encendido...")
    app.run_polling()
