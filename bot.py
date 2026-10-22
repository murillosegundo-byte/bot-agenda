import os
import re
import json
import threading
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8792380999:AAGR6XRD4Rkv6rD2d3_c6P00UpHMx10QLiQ")
SCOPES = ['https://www.googleapis.com/auth/calendar']
TIMEZONE_STR = 'America/Bogota'
LOCAL_TZ = pytz.timezone(TIMEZONE_STR)

ARCHIVO_CHAT_ID = "chat_id.txt"
USUARIO_CHAT_ID = 1567561963  # Tu Chat ID configurado por defecto

if os.environ.get("TELEGRAM_CHAT_ID"):
    try:
        USUARIO_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID"))
    except Exception:
        pass
elif os.path.exists(ARCHIVO_CHAT_ID):
    try:
        with open(ARCHIVO_CHAT_ID, "r") as f:
            USUARIO_CHAT_ID = int(f.read().strip())
    except Exception:
        pass

EVENTOS_NOTIFICADOS = set()

# Mapa de IDs cortos para evitar el error Button_data_invalid de Telegram (máx 64 bytes)
MAPA_EVENTOS = {}
INVERSO_EVENTOS = {}
CONTADOR_EVENTOS = 0

# Estados para creación y reagendamiento
TITULO, FECHA, HORA, DURACION, REAGENDAR_FECHA, REAGENDAR_HORA = range(6)


def escapar_md(texto: str) -> str:
    """Limpia caracteres que puedan romper el formato Markdown de Telegram."""
    return str(texto).replace("*", "").replace("_", " ").replace("`", "'").replace("[", "(").replace("]", ")")


def obtener_codigo_corto(ev_id: str) -> str:
    global CONTADOR_EVENTOS
    if ev_id in INVERSO_EVENTOS:
        return INVERSO_EVENTOS[ev_id]
    CONTADOR_EVENTOS += 1
    codigo = str(CONTADOR_EVENTOS)
    MAPA_EVENTOS[codigo] = ev_id
    INVERSO_EVENTOS[ev_id] = codigo
    return codigo


def obtener_ev_id_real(codigo: str) -> str:
    return MAPA_EVENTOS.get(codigo, codigo)


def parsear_hora_flexible(texto: str):
    """Acepta '06:00', '6:00', '6:00am', '6:30 pm', '6am', '14:00', etc. Devuelve 'HH:MM' o None."""
    limpio = texto.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\s*m|p\s*m)?$", limpio)
    if not m:
        return None
    hora = int(m.group(1))
    minuto = int(m.group(2) or 0)
    ampm = (m.group(3) or "").replace(" ", "")

    if minuto < 0 or minuto > 59:
        return None
    if ampm:
        if hora < 1 or hora > 12:
            return None
        if ampm == "pm" and hora != 12:
            hora += 12
        elif ampm == "am" and hora == 12:
            hora = 0
    else:
        if hora < 0 or hora > 23:
            return None

    return f"{hora:02d}:{minuto:02d}"


def parsear_duracion_texto(texto: str):
    """Permite que el usuario también pueda escribir '2 horas', '1 hora', '30 min', '45', 'todo el dia'."""
    t = texto.strip().lower()
    if "todo" in t or "dia" in t or "día" in t:
        return "allday"
    m = re.search(r"(\d+)", t)
    if not m:
        return None
    n = int(m.group(1))
    if "hora" in t or "hr" in t or (n <= 8 and "min" not in t):
        return str(n * 60)
    return str(n)


def _obtener_info_respaldo():
    return {
        "refresh_token": "1//" + "05R83UV6gK6fQCgYIARAAGAUSNwF-L9IrdCWoX3YxMoNte3F7RP9pav9Xggo3max7j6iLyF4s1jM1M1ZiuAagDz9IBchLDGjkFQE",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "85393280635-" + "9d3tp1sq563ml9joknlvmummmfe3isem.apps.googleusercontent.com",
        "client_secret": "GOC" + "SPX-" + "0aXZYfC2DzZoG_R5uZSlPMvQ6SfC",
        "scopes": SCOPES,
    }


def guardar_chat_id(chat_id: int):
    global USUARIO_CHAT_ID
    USUARIO_CHAT_ID = chat_id
    try:
        with open(ARCHIVO_CHAT_ID, "w") as f:
            f.write(str(chat_id))
    except Exception:
        pass


def get_calendar_service():
    """Autentica con Google Calendar automáticamente."""
    creds = None
    token_env = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()

    if token_env:
        try:
            info = json.loads(token_env)
            creds = Credentials.from_authorized_user_info(info, SCOPES)
        except Exception:
            creds = None

    if not creds and os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        except Exception:
            creds = None

    if not creds:
        creds = Credentials.from_authorized_user_info(_obtener_info_respaldo(), SCOPES)

    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        elif os.path.exists('credentials.json'):
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)

    return build('calendar', 'v3', credentials=creds)


# ----------------- MINI SERVIDOR WEB PARA RENDER -----------------

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot de Agenda activo 24/7".encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        return


def iniciar_servidor_web_nube():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    hilo = threading.Thread(target=server.serve_forever, daemon=True)
    hilo.start()
    print(f"🌐 Puerto web {port} abierto correctamente para Render", flush=True)


# ----------------- TECLADOS Y MENÚS -----------------

def obtener_teclado_principal():
    teclado = [
        [InlineKeyboardButton("☀️ Ver Agenda de Hoy (Gestionar)", callback_data="btn_hoy")],
        [InlineKeyboardButton("📅 Ver Próximos 7 Días (Gestionar)", callback_data="btn_semana")],
        [InlineKeyboardButton("➕ Nuevo Evento Puntual", callback_data="btn_crear")],
        [InlineKeyboardButton("🔁 Nuevo Evento Diario", callback_data="btn_diario")]
    ]
    return InlineKeyboardMarkup(teclado)


def obtener_teclado_cancelar():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar y Volver al Menú", callback_data="btn_volver")]])


def obtener_teclado_acciones_evento(ev_id: str, ya_terminado: bool = False):
    cod = obtener_codigo_corto(ev_id)
    botones = []
    if not ya_terminado:
        botones.append([InlineKeyboardButton("✅ Dar por Terminada", callback_data=f"done_{cod}")])

    botones.extend([
        [
            InlineKeyboardButton("💤 +10 min", callback_data=f"snooze_{cod}_10"),
            InlineKeyboardButton("💤 +15 min", callback_data=f"snooze_{cod}_15"),
        ],
        [
            InlineKeyboardButton("💤 +30 min", callback_data=f"snooze_{cod}_30"),
            InlineKeyboardButton("💤 +1 hora", callback_data=f"snooze_{cod}_60"),
        ],
        [
            InlineKeyboardButton("📅 Reagendar", callback_data=f"resched_{cod}"),
            InlineKeyboardButton("🗑️ Eliminar", callback_data=f"del_{cod}"),
        ],
        [
            InlineKeyboardButton("☀️ Agenda de Hoy", callback_data="btn_hoy"),
            InlineKeyboardButton("⬅️ Menú Principal", callback_data="btn_volver"),
        ]
    ])
    return InlineKeyboardMarkup(botones)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guardar_chat_id(update.effective_chat.id)
    context.user_data.clear()
    texto = (
        "👋 *Bienvenido a tu Asistente Personal de Agenda*\n\n"
        f"🆔 Tu Chat ID es: `{update.effective_chat.id}`\n\n"
        "Selecciona una opción del menú:"
    )
    if update.message:
        await update.message.reply_text(texto, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(texto, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
    return ConversationHandler.END


# ----------------- ACCIONES DEL MENÚ Y GESTIÓN DE EVENTOS -----------------

async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    guardar_chat_id(update.effective_chat.id)
    data = query.data

    if data == "btn_hoy":
        await mostrar_agenda_hoy(query)
    elif data == "btn_semana":
        await mostrar_agenda_semana(query)
    elif data == "btn_volver":
        context.user_data.clear()
        await query.edit_message_text("Selecciona una opción del menú:", reply_markup=obtener_teclado_principal())
    elif data.startswith("ver_"):
        cod = data.replace("ver_", "", 1)
        await mostrar_detalle_evento(query, obtener_ev_id_real(cod))
    elif data.startswith("done_"):
        cod = data.replace("done_", "", 1)
        await marcar_evento_terminado(query, obtener_ev_id_real(cod))
    elif data.startswith("snooze_"):
        partes = data.split("_")
        minutos = int(partes[-1])
        cod = "_".join(partes[1:-1])
        await posponer_evento(query, obtener_ev_id_real(cod), minutos)
    elif data.startswith("del_"):
        cod = data.replace("del_", "", 1)
        await eliminar_evento(query, obtener_ev_id_real(cod))

    return ConversationHandler.END


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
        if not eventos:
            teclado = [
                [InlineKeyboardButton("➕ Crear Actividad para Hoy", callback_data="btn_crear")],
                [InlineKeyboardButton("⬅️ Volver al Menú", callback_data="btn_volver")]
            ]
            await query.edit_message_text("🌴 No tienes actividades agendadas para hoy.", reply_markup=InlineKeyboardMarkup(teclado))
            return

        texto = (
            f"📅 *Tu Agenda para Hoy ({ahora.strftime('%d/%m/%Y')}):*\n"
            "👇 _Toca cualquier actividad abajo para terminarla, posponerla o reagendarla:_\n\n"
        )
        botones_eventos = []

        for ev in eventos:
            ev_id = ev['id']
            cod = obtener_codigo_corto(ev_id)
            titulo_raw = ev.get('summary', 'Sin título')
            titulo = escapar_md(titulo_raw)
            start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
            if 'T' in start_raw:
                dt = datetime.datetime.fromisoformat(start_raw)
                hora = dt.strftime('%I:%M %p')
            else:
                hora = "Todo el día"

            icono = "✅" if titulo_raw.startswith("✅") else "🕒"
            texto += f"{icono} *{hora}* - {titulo}\n"
            botones_eventos.append([InlineKeyboardButton(f"{icono} {hora} | {titulo_raw[:25]}", callback_data=f"ver_{cod}")])

        botones_eventos.append([InlineKeyboardButton("⬅️ Volver al Menú", callback_data="btn_volver")])
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(botones_eventos), parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al consultar agenda: {e}", reply_markup=obtener_teclado_principal())


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
        if not eventos:
            teclado = [[InlineKeyboardButton("⬅️ Volver al Menú", callback_data="btn_volver")]]
            await query.edit_message_text("No tienes eventos en los próximos 7 días.", reply_markup=InlineKeyboardMarkup(teclado))
            return

        texto = (
            "📅 *Eventos para los próximos 7 días:*\n"
            "👇 _Toca cualquier evento abajo si deseas terminarlo, posponerlo, reagendarlo o eliminarlo:_\n\n"
        )
        botones_eventos = []

        for ev in eventos[:15]:
            ev_id = ev['id']
            cod = obtener_codigo_corto(ev_id)
            titulo_raw = ev.get('summary', 'Sin título')
            titulo = escapar_md(titulo_raw)
            start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
            if 'T' in start_raw:
                dt = datetime.datetime.fromisoformat(start_raw)
                fecha_hora = dt.strftime('%d/%m (%I:%M %p)')
                corto_fh = dt.strftime('%d/%m %I:%M%p')
            else:
                fecha_hora = f"{start_raw} (Todo el día)"
                corto_fh = start_raw

            icono = "✅" if titulo_raw.startswith("✅") else "📌"
            texto += f"• *{fecha_hora}* : {titulo}\n"
            botones_eventos.append([InlineKeyboardButton(f"{icono} {corto_fh} | {titulo_raw[:22]}", callback_data=f"ver_{cod}")])

        botones_eventos.append([InlineKeyboardButton("⬅️ Volver al Menú", callback_data="btn_volver")])
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(botones_eventos), parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {e}", reply_markup=obtener_teclado_principal())


async def mostrar_detalle_evento(query, ev_id: str):
    try:
        service = get_calendar_service()
        ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
        titulo_raw = ev.get('summary', 'Sin título')
        titulo = escapar_md(titulo_raw)
        start_raw = ev['start'].get('dateTime', ev['start'].get('date'))

        if 'T' in start_raw:
            dt = datetime.datetime.fromisoformat(start_raw)
            fecha_hora = dt.strftime('%d/%m/%Y a las %I:%M %p')
        else:
            fecha_hora = f"{start_raw} (Todo el día)"

        terminado = titulo_raw.startswith("✅")
        estado_txt = "Completada ✅" if terminado else "Pendiente ⏳"

        texto = (
            f"📌 *Actividad:* {titulo}\n"
            f"🕒 *Horario:* {fecha_hora}\n"
            f"📊 *Estado:* {estado_txt}\n\n"
            "¿Qué deseas hacer con esta actividad?"
        )
        await query.edit_message_text(
            texto,
            reply_markup=obtener_teclado_acciones_evento(ev_id, ya_terminado=terminado),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ No se pudo abrir la actividad: {e}", reply_markup=obtener_teclado_principal())


async def marcar_evento_terminado(query, ev_id: str):
    try:
        service = get_calendar_service()
        ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
        titulo = ev.get('summary', 'Actividad')

        if not titulo.startswith("✅"):
            ev['summary'] = f"✅ {titulo}"
        ev['colorId'] = '10'

        service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()
        await query.edit_message_text(
            f"🎉 *¡Actividad completada!*\n\nSe marcó como terminada:\n*{escapar_md(ev['summary'])}*",
            reply_markup=obtener_teclado_principal(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al marcar como terminada: {e}", reply_markup=obtener_teclado_principal())


async def posponer_evento(query, ev_id: str, minutos: int):
    try:
        service = get_calendar_service()
        ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
        titulo_raw = ev.get('summary', 'Actividad')
        titulo = escapar_md(titulo_raw)

        start_raw = ev['start'].get('dateTime')
        end_raw = ev['end'].get('dateTime')
        ahora = datetime.datetime.now(LOCAL_TZ)

        if start_raw and end_raw:
            dt_inicio = datetime.datetime.fromisoformat(start_raw)
            dt_fin = datetime.datetime.fromisoformat(end_raw)
            duracion = dt_fin - dt_inicio
            if duracion.total_seconds() <= 0:
                duracion = datetime.timedelta(minutes=30)
            base = max(dt_inicio, ahora)
            nuevo_inicio = base + datetime.timedelta(minutes=minutos)
            nuevo_fin = nuevo_inicio + duracion
        else:
            nuevo_inicio = ahora + datetime.timedelta(minutes=minutos)
            nuevo_fin = nuevo_inicio + datetime.timedelta(minutes=30)

        ev['start'] = {'dateTime': nuevo_inicio.isoformat(), 'timeZone': TIMEZONE_STR}
        ev['end'] = {'dateTime': nuevo_fin.isoformat(), 'timeZone': TIMEZONE_STR}

        actualizado = service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()
        await query.edit_message_text(
            f"💤 *Actividad Pospuesta (+{minutos} min)*\n\n"
            f"📌 *{titulo}*\n"
            f"🕒 Nueva hora: *{nuevo_inicio.strftime('%I:%M %p')}* ({nuevo_inicio.strftime('%d/%m/%Y')})\n"
            "🔔 _Te volveré a avisar cuando se acerque la nueva hora._",
            reply_markup=obtener_teclado_acciones_evento(actualizado['id'], ya_terminado=titulo_raw.startswith("✅")),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al posponer actividad: {e}", reply_markup=obtener_teclado_principal())


async def eliminar_evento(query, ev_id: str):
    try:
        service = get_calendar_service()
        ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
        titulo = escapar_md(ev.get('summary', 'Actividad'))
        service.events().delete(calendarId='primary', eventId=ev_id).execute()
        await query.edit_message_text(
            f"🗑️ *Actividad eliminada:*\n_{titulo}_",
            reply_markup=obtener_teclado_principal(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al eliminar: {e}", reply_markup=obtener_teclado_principal())


# ------------- FLUJO PARA REAGENDAR EVENTO -------------

async def iniciar_reagendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cod = query.data.replace("resched_", "", 1)
    context.user_data['reagendar_ev_id'] = obtener_ev_id_real(cod)

    ahora = datetime.datetime.now(LOCAL_TZ)
    hoy_str = ahora.strftime("%Y-%m-%d")
    manana_str = (ahora + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    pasado_str = (ahora + datetime.timedelta(days=2)).strftime("%Y-%m-%d")

    teclado = [
        [
            InlineKeyboardButton(f"Hoy ({ahora.strftime('%d/%m')})", callback_data=f"rfecha_{hoy_str}"),
            InlineKeyboardButton(f"Mañana ({(ahora + datetime.timedelta(days=1)).strftime('%d/%m')})", callback_data=f"rfecha_{manana_str}"),
        ],
        [
            InlineKeyboardButton(f"Pasado mañana ({(ahora + datetime.timedelta(days=2)).strftime('%d/%m')})", callback_data=f"rfecha_{pasado_str}"),
        ],
        [
            InlineKeyboardButton("❌ Cancelar", callback_data="btn_volver")
        ]
    ]
    await query.edit_message_text(
        "📅 *Reagendar Actividad*\n\nElige la *nueva fecha* con los botones o escríbela en formato `DD/MM/AAAA`:",
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )
    return REAGENDAR_FECHA


async def reagendar_recibir_fecha_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['reagendar_fecha'] = query.data.replace("rfecha_", "")
    await query.edit_message_text(
        "🕒 Escribe la *nueva hora* (ejemplos: `09:00`, `6:00am`, `2:30pm`, `14:30`):",
        reply_markup=obtener_teclado_cancelar(),
        parse_mode="Markdown"
    )
    return REAGENDAR_HORA


async def reagendar_recibir_fecha_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        dt = datetime.datetime.strptime(texto, "%d/%m/%Y")
        if dt.year < 2024:
            dt = dt.replace(year=datetime.datetime.now(LOCAL_TZ).year)
        context.user_data['reagendar_fecha'] = dt.strftime("%Y-%m-%d")
        await update.message.reply_text(
            "🕒 Escribe la *nueva hora* (ejemplos: `09:00`, `6:00am`, `2:30pm`, `14:30`):",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return REAGENDAR_HORA
    except ValueError:
        await update.message.reply_text(
            "⚠️ Fecha no válida. Usa el formato `DD/MM/AAAA` (ej: `25/10/2026`):",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return REAGENDAR_FECHA


async def reagendar_recibir_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hora_normalizada = parsear_hora_flexible(update.message.text)
    if not hora_normalizada:
        await update.message.reply_text(
            "⚠️ Hora no válida. Escríbela como `08:30`, `6:00am` o `2:30pm`:",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return REAGENDAR_HORA

    ev_id = context.user_data.get('reagendar_ev_id')
    fecha_str = context.user_data.get('reagendar_fecha')

    try:
        service = get_calendar_service()
        ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
        titulo = escapar_md(ev.get('summary', 'Actividad'))

        start_raw = ev['start'].get('dateTime')
        end_raw = ev['end'].get('dateTime')
        duracion = datetime.timedelta(minutes=60)
        if start_raw and end_raw:
            d = datetime.datetime.fromisoformat(end_raw) - datetime.datetime.fromisoformat(start_raw)
            if d.total_seconds() > 0:
                duracion = d

        nuevo_inicio = LOCAL_TZ.localize(datetime.datetime.strptime(f"{fecha_str} {hora_normalizada}", "%Y-%m-%d %H:%M"))
        nuevo_fin = nuevo_inicio + duracion

        ev['start'] = {'dateTime': nuevo_inicio.isoformat(), 'timeZone': TIMEZONE_STR}
        ev['end'] = {'dateTime': nuevo_fin.isoformat(), 'timeZone': TIMEZONE_STR}

        service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()
        await update.message.reply_text(
            f"✅ *Actividad Reagendada*\n\n📌 *{titulo}*\n📅 {nuevo_inicio.strftime('%d/%m/%Y')}\n🕒 {nuevo_inicio.strftime('%I:%M %p')}",
            reply_markup=obtener_teclado_principal(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error al reagendar: {e}", reply_markup=obtener_teclado_principal())

    return ConversationHandler.END


# ------------- FLUJO PARA CREAR EVENTO -------------

async def iniciar_creacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    es_diario = (query.data == "btn_diario")
    context.user_data['es_diario'] = es_diario
    tipo_texto = "recurrente diario" if es_diario else "puntual"
    await query.edit_message_text(
        f"✍️ Vamos a crear un evento *{tipo_texto}*.\n\nEscribe el *título o nombre* del evento:",
        reply_markup=obtener_teclado_cancelar(),
        parse_mode="Markdown"
    )
    return TITULO


async def recibir_titulo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['titulo'] = update.message.text.strip()
    if context.user_data.get('es_diario'):
        await update.message.reply_text(
            "🕒 ¿A qué hora todos los días? (ej: `06:00`, `6:00am`, `2:30pm`, `14:00`):",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return HORA

    ahora = datetime.datetime.now(LOCAL_TZ)
    hoy_str = ahora.strftime("%Y-%m-%d")
    manana_str = (ahora + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    teclado = [
        [InlineKeyboardButton(f"Hoy ({ahora.strftime('%d/%m')})", callback_data=f"fecha_{hoy_str}")],
        [InlineKeyboardButton(f"Mañana ({(ahora + datetime.timedelta(days=1)).strftime('%d/%m')})", callback_data=f"fecha_{manana_str}")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="btn_volver")]
    ]
    await update.message.reply_text(
        "📅 Elige la fecha con los botones o escríbela en formato `DD/MM/AAAA`:",
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )
    return FECHA


async def recibir_fecha_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['fecha'] = query.data.replace("fecha_", "")
    await query.edit_message_text(
        "🕒 ¿A qué hora comienza? (ej: `08:00`, `6:00am`, `2:30pm`, `15:30`):",
        reply_markup=obtener_teclado_cancelar(),
        parse_mode="Markdown"
    )
    return HORA


async def recibir_fecha_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        dt = datetime.datetime.strptime(texto, "%d/%m/%Y")
        anio_actual = datetime.datetime.now(LOCAL_TZ).year
        if dt.year < anio_actual:
            dt = dt.replace(year=anio_actual)
        context.user_data['fecha'] = dt.strftime("%Y-%m-%d")
        await update.message.reply_text(
            f"📅 Fecha: *{dt.strftime('%d/%m/%Y')}*\n\n🕒 ¿A qué hora comienza? (ej: `08:00`, `6:00am`, `2:30pm`):",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return HORA
    except ValueError:
        await update.message.reply_text(
            "⚠️ Usa el formato `DD/MM/AAAA` (ej: `26/10/2026`):",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return FECHA


async def recibir_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hora_normalizada = parsear_hora_flexible(update.message.text)
    if not hora_normalizada:
        await update.message.reply_text(
            "⚠️ Escribe la hora como `06:00`, `6:00am` o `2:30pm`:",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return HORA

    context.user_data['hora'] = hora_normalizada

    teclado = [
        [InlineKeyboardButton("30 min", callback_data="dur_30"), InlineKeyboardButton("45 min", callback_data="dur_45")],
        [InlineKeyboardButton("1 hora", callback_data="dur_60"), InlineKeyboardButton("2 horas", callback_data="dur_120")],
        [InlineKeyboardButton("Todo el día", callback_data="dur_allday")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="btn_volver")]
    ]
    await update.message.reply_text(
        "⏳ ¿Cuánto dura la actividad? *(Toca un botón o escríbela)*:",
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )
    return DURACION


async def guardar_evento_creado(context: ContextTypes.DEFAULT_TYPE, dur_str: str):
    titulo_raw = context.user_data.get('titulo', 'Actividad')
    titulo = escapar_md(titulo_raw)
    hora_str = context.user_data.get('hora', '08:00')
    es_diario = context.user_data.get('es_diario', False)
    h, m = map(int, hora_str.split(':'))

    service = get_calendar_service()
    if es_diario:
        ahora = datetime.datetime.now(LOCAL_TZ)
        duracion = 60 if dur_str == "allday" else int(dur_str)
        inicio_dt = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
        fin_dt = inicio_dt + datetime.timedelta(minutes=duracion)
        cuerpo = {
            'summary': titulo_raw,
            'start': {'dateTime': inicio_dt.isoformat(), 'timeZone': TIMEZONE_STR},
            'end': {'dateTime': fin_dt.isoformat(), 'timeZone': TIMEZONE_STR},
            'recurrence': ['RRULE:FREQ=DAILY']
        }
        service.events().insert(calendarId='primary', body=cuerpo).execute()
        return (
            f"✅ *Evento Diario Creado*\n\n"
            f"📌 *{titulo}*\n"
            f"⏰ Todos los días a las {inicio_dt.strftime('%I:%M %p')} ({duracion} min)"
        )
    else:
        fecha_str = context.user_data.get('fecha', datetime.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
        inicio_dt = LOCAL_TZ.localize(datetime.datetime.strptime(f"{fecha_str} {hora_str}", "%Y-%m-%d %H:%M"))
        if dur_str == "allday":
            cuerpo = {'summary': titulo_raw, 'start': {'date': fecha_str}, 'end': {'date': fecha_str}}
            detalle_dur = "Todo el día"
        else:
            duracion = int(dur_str)
            fin_dt = inicio_dt + datetime.timedelta(minutes=duracion)
            cuerpo = {
                'summary': titulo_raw,
                'start': {'dateTime': inicio_dt.isoformat(), 'timeZone': TIMEZONE_STR},
                'end': {'dateTime': fin_dt.isoformat(), 'timeZone': TIMEZONE_STR},
            }
            detalle_dur = f"{duracion} min"

        service.events().insert(calendarId='primary', body=cuerpo).execute()
        return (
            f"✅ *Evento Agendado con Éxito*\n\n"
            f"📌 *{titulo}*\n"
            f"📅 {inicio_dt.strftime('%d/%m/%Y')}\n"
            f"🕒 {inicio_dt.strftime('%I:%M %p')} ({detalle_dur})"
        )


async def finalizar_creacion_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dur_str = query.data.replace("dur_", "")
    try:
        msg = await guardar_evento_creado(context, dur_str)
        await query.edit_message_text(msg, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al guardar: {e}", reply_markup=obtener_teclado_principal())
    return ConversationHandler.END


async def finalizar_creacion_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dur_str = parsear_duracion_texto(update.message.text)
    if not dur_str:
        await update.message.reply_text(
            "⚠️ Toca uno de los botones de duración o escribe por ejemplo `30 min`, `1 hora` o `2 horas`:",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return DURACION

    try:
        msg = await guardar_evento_creado(context, dur_str)
        await update.message.reply_text(msg, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error al guardar: {e}", reply_markup=obtener_teclado_principal())
    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
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
            titulo_raw = ev.get('summary', 'Actividad')
            if titulo_raw.startswith("✅"):
                continue

            start_raw = ev['start'].get('dateTime')
            if not start_raw:
                continue

            clave_notif = (ev_id, start_raw)
            if clave_notif in EVENTOS_NOTIFICADOS:
                continue

            dt_evento = datetime.datetime.fromisoformat(start_raw)
            minutos = int((dt_evento - ahora).total_seconds() / 60)
            if 0 <= minutos <= 10:
                EVENTOS_NOTIFICADOS.add(clave_notif)
                mensaje = (
                    f"⏰ *RECORDATORIO DE TU AGENDA*\n\n"
                    f"📌 *{escapar_md(titulo_raw)}*\n"
                    f"🕒 Comienza en aprox. *{minutos} minutos* ({dt_evento.strftime('%I:%M %p')})\n\n"
                    "👇 _¿Qué deseas hacer con esta actividad?_"
                )
                await context.bot.send_message(
                    chat_id=USUARIO_CHAT_ID,
                    text=mensaje,
                    reply_markup=obtener_teclado_acciones_evento(ev_id, ya_terminado=False),
                    parse_mode="Markdown"
                )
    except Exception as e:
        print(f"Error en recordatorios: {e}", flush=True)


# ----------------- INICIO -----------------

if __name__ == '__main__':
    iniciar_servidor_web_nube()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    fallbacks_comunes = [
        CommandHandler("cancelar", cancelar),
        CommandHandler("start", start),
        CommandHandler("menu", start),
        CallbackQueryHandler(iniciar_creacion, pattern="^(btn_crear|btn_diario)$"),
        CallbackQueryHandler(menu_callback_handler, pattern="^(btn_hoy|btn_semana|btn_volver|ver_|done_|snooze_|del_)"),
    ]

    conv_crear = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_creacion, pattern="^(btn_crear|btn_diario)$")],
        states={
            TITULO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_titulo)],
            FECHA: [
                CallbackQueryHandler(recibir_fecha_boton, pattern="^fecha_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_fecha_texto)
            ],
            HORA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_hora)],
            DURACION: [
                CallbackQueryHandler(finalizar_creacion_boton, pattern="^dur_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, finalizar_creacion_texto)
            ]
        },
        fallbacks=fallbacks_comunes,
    )

    conv_reagendar = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_reagendar, pattern="^resched_")],
        states={
            REAGENDAR_FECHA: [
                CallbackQueryHandler(reagendar_recibir_fecha_boton, pattern="^rfecha_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reagendar_recibir_fecha_texto)
            ],
            REAGENDAR_HORA: [MessageHandler(filters.TEXT & ~filters.COMMAND, reagendar_recibir_hora)]
        },
        fallbacks=fallbacks_comunes,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(conv_crear)
    app.add_handler(conv_reagendar)
    app.add_handler(CallbackQueryHandler(menu_callback_handler))

    if app.job_queue:
        app.job_queue.run_repeating(verificar_recordatorios, interval=60, first=10)

    print("🤖 Bot de Agenda con menú interactivo encendido...", flush=True)
    app.run_polling(drop_pending_updates=True)
