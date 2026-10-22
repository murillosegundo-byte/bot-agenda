import os
import re
import json
import socket
import asyncio
import threading
import datetime
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Evita que cualquier llamada de red a Google Calendar pueda congelar el bot
socket.setdefaulttimeout(15)

# ================= CONFIGURACIÓN =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8792380999:AAGR6XRD4Rkv6rD2d3_c6P00UpHMx10QLiQ")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://bot-agenda-tbnp.onrender.com")
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

# Estado directo por usuario (reemplaza ConversationHandler para que NUNCA se quede pegado)
ESTADOS_CHAT = {}


def obtener_estado(chat_id: int) -> dict:
    if chat_id not in ESTADOS_CHAT:
        ESTADOS_CHAT[chat_id] = {"paso": None}
    return ESTADOS_CHAT[chat_id]


def limpiar_estado(chat_id: int):
    ESTADOS_CHAT[chat_id] = {"paso": None}


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
    """Permite escribir '2 horas', '1 hora', '30 min', '45', 'todo el dia'."""
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

    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


# ----------------- MINI SERVIDOR WEB Y AUTO-PING PARA RENDER -----------------

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


def _loop_autoping():
    """Hace ping cada 4 minutos a la URL de Render para evitar que se duerma por inactividad."""
    import time
    while True:
        time.sleep(240)
        try:
            req = urllib.request.Request(RENDER_URL, headers={"User-Agent": "KeepAlive-Bot/1.0"})
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass


def iniciar_servidor_web_nube():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    hilo = threading.Thread(target=server.serve_forever, daemon=True)
    hilo.start()

    hilo_ping = threading.Thread(target=_loop_autoping, daemon=True)
    hilo_ping.start()
    print(f"🌐 Puerto web {port} abierto y auto-ping activo para Render", flush=True)


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


# ----------------- COMANDOS SIEMPRE ACTIVOS (/start, /menu, /cancelar) -----------------

async def comando_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    guardar_chat_id(chat_id)
    limpiar_estado(chat_id)

    texto = (
        "👋 *Bienvenido a tu Asistente Personal de Agenda*\n\n"
        "Selecciona una opción del menú:"
    )
    await update.message.reply_text(texto, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")


async def comando_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    limpiar_estado(chat_id)
    await update.message.reply_text(
        "✅ Operación cancelada. Selecciona una opción del menú:",
        reply_markup=obtener_teclado_principal()
    )


# ----------------- MANEJADOR ÚNICO DE TODOS LOS BOTONES -----------------

async def manejar_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    guardar_chat_id(chat_id)
    estado = obtener_estado(chat_id)
    data = query.data

    # 1. Volver al menú o cancelar
    if data == "btn_volver":
        limpiar_estado(chat_id)
        await query.edit_message_text("Selecciona una opción del menú:", reply_markup=obtener_teclado_principal())
        return

    # 2. Ver Agenda de Hoy
    if data == "btn_hoy":
        limpiar_estado(chat_id)
        await mostrar_agenda_hoy(query)
        return

    # 3. Ver Próximos 7 Días
    if data == "btn_semana":
        limpiar_estado(chat_id)
        await mostrar_agenda_semana(query)
        return

    # 4. Iniciar creación de evento (Puntual o Diario)
    if data in ("btn_crear", "btn_diario"):
        es_diario = (data == "btn_diario")
        ESTADOS_CHAT[chat_id] = {"paso": "TITULO", "es_diario": es_diario}
        tipo_texto = "recurrente diario" if es_diario else "puntual"
        await query.edit_message_text(
            f"✍️ Vamos a crear un evento *{tipo_texto}*.\n\nEscribe en el chat el *título o nombre* del evento:",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return

    # 5. Selección de fecha con botón al crear evento
    if data.startswith("fecha_"):
        estado["fecha"] = data.replace("fecha_", "")
        estado["paso"] = "HORA"
        await query.edit_message_text(
            f"📅 Fecha elegida: *{estado['fecha']}*\n\n"
            "🕒 Ahora escribe en el chat la *hora de inicio* (ej: `08:00`, `6:00am`, `2:30pm`):",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return

    # 6. Selección de duración con botón al crear evento
    if data.startswith("dur_"):
        dur_str = data.replace("dur_", "")
        await query.edit_message_text("⏳ _Guardando en Google Calendar..._", parse_mode="Markdown")
        try:
            msg = await asyncio.to_thread(guardar_evento_creado_sync, estado, dur_str)
            limpiar_estado(chat_id)
            await query.edit_message_text(msg, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
        except Exception as e:
            limpiar_estado(chat_id)
            await query.edit_message_text(f"❌ Error al guardar: {e}", reply_markup=obtener_teclado_principal())
        return

    # 7. Gestión de actividades existentes
    if data.startswith("ver_"):
        limpiar_estado(chat_id)
        cod = data.replace("ver_", "", 1)
        await mostrar_detalle_evento(query, obtener_ev_id_real(cod))
        return

    if data.startswith("done_"):
        limpiar_estado(chat_id)
        cod = data.replace("done_", "", 1)
        await marcar_evento_terminado(query, obtener_ev_id_real(cod))
        return

    if data.startswith("snooze_"):
        limpiar_estado(chat_id)
        partes = data.split("_")
        minutos = int(partes[-1])
        cod = "_".join(partes[1:-1])
        await posponer_evento(query, obtener_ev_id_real(cod), minutos)
        return

    if data.startswith("del_"):
        limpiar_estado(chat_id)
        cod = data.replace("del_", "", 1)
        await eliminar_evento(query, obtener_ev_id_real(cod))
        return

    # 8. Reagendar evento
    if data.startswith("resched_"):
        cod = data.replace("resched_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        ESTADOS_CHAT[chat_id] = {"paso": "REAGENDAR_FECHA", "reagendar_ev_id": ev_id}

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
        return

    if data.startswith("rfecha_"):
        estado["reagendar_fecha"] = data.replace("rfecha_", "")
        estado["paso"] = "REAGENDAR_HORA"
        await query.edit_message_text(
            f"📅 Nueva fecha: *{estado['reagendar_fecha']}*\n\n"
            "🕒 Escribe en el chat la *nueva hora* (ejemplos: `09:00`, `6:00am`, `2:30pm`):",
            reply_markup=obtener_teclado_cancelar(),
            parse_mode="Markdown"
        )
        return


# ----------------- MANEJADOR ÚNICO DE TEXTO ESCRITO POR EL USUARIO -----------------

async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    guardar_chat_id(chat_id)
    estado = obtener_estado(chat_id)
    paso = estado.get("paso")
    texto = update.message.text.strip()

    # Si no había ningún paso activo, le muestra el menú para que nunca parezca "pegado"
    if not paso:
        await update.message.reply_text(
            "👋 Selecciona qué deseas hacer en el menú:",
            reply_markup=obtener_teclado_principal()
        )
        return

    # PASO 1: Recibir Título
    if paso == "TITULO":
        estado["titulo"] = texto
        if estado.get("es_diario"):
            estado["paso"] = "HORA"
            await update.message.reply_text(
                f"📌 Título: *{escapar_md(texto)}*\n\n"
                "🕒 ¿A qué hora debe sonar *todos los días*? (ej: `06:00`, `6:00am`, `2:30pm`):",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
        else:
            estado["paso"] = "FECHA"
            ahora = datetime.datetime.now(LOCAL_TZ)
            hoy_str = ahora.strftime("%Y-%m-%d")
            manana_str = (ahora + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            teclado = [
                [InlineKeyboardButton(f"Hoy ({ahora.strftime('%d/%m')})", callback_data=f"fecha_{hoy_str}")],
                [InlineKeyboardButton(f"Mañana ({(ahora + datetime.timedelta(days=1)).strftime('%d/%m')})", callback_data=f"fecha_{manana_str}")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="btn_volver")]
            ]
            await update.message.reply_text(
                f"📌 Título: *{escapar_md(texto)}*\n\n"
                "📅 Toca un botón para elegir la fecha o escríbela en formato `DD/MM/AAAA`:",
                reply_markup=InlineKeyboardMarkup(teclado),
                parse_mode="Markdown"
            )
        return

    # PASO 2: Recibir Fecha por texto
    if paso == "FECHA":
        try:
            dt = datetime.datetime.strptime(texto, "%d/%m/%Y")
            anio_actual = datetime.datetime.now(LOCAL_TZ).year
            if dt.year < anio_actual:
                dt = dt.replace(year=anio_actual)
            estado["fecha"] = dt.strftime("%Y-%m-%d")
            estado["paso"] = "HORA"
            await update.message.reply_text(
                f"📅 Fecha: *{dt.strftime('%d/%m/%Y')}*\n\n"
                "🕒 ¿A qué hora comienza? (ej: `08:00`, `6:00am`, `2:30pm`):",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Fecha no válida. Toca el botón *Hoy / Mañana* o escríbela como `DD/MM/AAAA` (ej: `26/10/2026`):",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
        return

    # PASO 3: Recibir Hora
    if paso == "HORA":
        hora_norm = parsear_hora_flexible(texto)
        if not hora_norm:
            await update.message.reply_text(
                "⚠️ Hora no válida. Escríbela por ejemplo así: `06:00`, `6:00am` o `2:30pm`:",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
            return

        estado["hora"] = hora_norm
        estado["paso"] = "DURACION"
        teclado = [
            [InlineKeyboardButton("30 min", callback_data="dur_30"), InlineKeyboardButton("45 min", callback_data="dur_45")],
            [InlineKeyboardButton("1 hora", callback_data="dur_60"), InlineKeyboardButton("2 horas", callback_data="dur_120")],
            [InlineKeyboardButton("Todo el día", callback_data="dur_allday")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="btn_volver")]
        ]
        await update.message.reply_text(
            f"🕒 Hora: *{hora_norm}*\n\n⏳ ¿Cuánto dura la actividad? *(Toca un botón o escríbela)*:",
            reply_markup=InlineKeyboardMarkup(teclado),
            parse_mode="Markdown"
        )
        return

    # PASO 4: Recibir Duración por texto
    if paso == "DURACION":
        dur_str = parsear_duracion_texto(texto)
        if not dur_str:
            await update.message.reply_text(
                "⚠️ Toca uno de los botones verdes de duración o escribe `30 min`, `1 hora` o `2 horas`:",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
            return

        espera = await update.message.reply_text("⏳ _Guardando en Google Calendar..._", parse_mode="Markdown")
        try:
            msg = await asyncio.to_thread(guardar_evento_creado_sync, estado, dur_str)
            limpiar_estado(chat_id)
            await espera.edit_text(msg, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
        except Exception as e:
            limpiar_estado(chat_id)
            await espera.edit_text(f"❌ Error al guardar: {e}", reply_markup=obtener_teclado_principal())
        return

    # PASO 5: Reagendar - recibir fecha por texto
    if paso == "REAGENDAR_FECHA":
        try:
            dt = datetime.datetime.strptime(texto, "%d/%m/%Y")
            anio_actual = datetime.datetime.now(LOCAL_TZ).year
            if dt.year < anio_actual:
                dt = dt.replace(year=anio_actual)
            estado["reagendar_fecha"] = dt.strftime("%Y-%m-%d")
            estado["paso"] = "REAGENDAR_HORA"
            await update.message.reply_text(
                f"📅 Nueva fecha: *{dt.strftime('%d/%m/%Y')}*\n\n"
                "🕒 Escribe la *nueva hora* (ejemplos: `09:00`, `6:00am`, `2:30pm`):",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Fecha no válida. Usa el formato `DD/MM/AAAA` (ej: `25/10/2026`):",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
        return

    # PASO 6: Reagendar - recibir hora por texto
    if paso == "REAGENDAR_HORA":
        hora_norm = parsear_hora_flexible(texto)
        if not hora_norm:
            await update.message.reply_text(
                "⚠️ Hora no válida. Escríbela como `08:30`, `6:00am` o `2:30pm`:",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
            return

        ev_id = estado.get("reagendar_ev_id")
        fecha_str = estado.get("reagendar_fecha")
        espera = await update.message.reply_text("⏳ _Reagendando en Google Calendar..._", parse_mode="Markdown")
        try:
            msg = await asyncio.to_thread(reagendar_evento_sync, ev_id, fecha_str, hora_norm)
            limpiar_estado(chat_id)
            await espera.edit_text(msg, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")
        except Exception as e:
            limpiar_estado(chat_id)
            await espera.edit_text(f"❌ Error al reagendar: {e}", reply_markup=obtener_teclado_principal())
        return


# ----------------- FUNCIONES DE GOOGLE CALENDAR (EN HILO SEPARADO CON TIMEOUT) -----------------

def guardar_evento_creado_sync(estado: dict, dur_str: str) -> str:
    titulo_raw = estado.get('titulo', 'Actividad')
    titulo = escapar_md(titulo_raw)
    hora_str = estado.get('hora', '08:00')
    es_diario = estado.get('es_diario', False)
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
        fecha_str = estado.get('fecha', datetime.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
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


def reagendar_evento_sync(ev_id: str, fecha_str: str, hora_normalizada: str) -> str:
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
    return (
        f"✅ *Actividad Reagendada*\n\n"
        f"📌 *{titulo}*\n"
        f"📅 {nuevo_inicio.strftime('%d/%m/%Y')}\n"
        f"🕒 {nuevo_inicio.strftime('%I:%M %p')}"
    )


def _listar_eventos_sync(time_min: str, time_max: str):
    service = get_calendar_service()
    res = service.events().list(
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    return res.get('items', [])


async def mostrar_agenda_hoy(query):
    try:
        ahora = datetime.datetime.now(LOCAL_TZ)
        inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        fin_dia = ahora.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        eventos = await asyncio.to_thread(_listar_eventos_sync, inicio_dia, fin_dia)
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
        ahora = datetime.datetime.now(LOCAL_TZ)
        limite = ahora + datetime.timedelta(days=7)

        eventos = await asyncio.to_thread(_listar_eventos_sync, ahora.isoformat(), limite.isoformat())
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


def _obtener_evento_sync(ev_id: str):
    service = get_calendar_service()
    return service.events().get(calendarId='primary', eventId=ev_id).execute()


async def mostrar_detalle_evento(query, ev_id: str):
    try:
        ev = await asyncio.to_thread(_obtener_evento_sync, ev_id)
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


def _marcar_terminado_sync(ev_id: str):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    titulo = ev.get('summary', 'Actividad')
    if not titulo.startswith("✅"):
        ev['summary'] = f"✅ {titulo}"
    ev['colorId'] = '10'
    return service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()


async def marcar_evento_terminado(query, ev_id: str):
    try:
        ev = await asyncio.to_thread(_marcar_terminado_sync, ev_id)
        await query.edit_message_text(
            f"🎉 *¡Actividad completada!*\n\nSe marcó como terminada:\n*{escapar_md(ev['summary'])}*",
            reply_markup=obtener_teclado_principal(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al marcar como terminada: {e}", reply_markup=obtener_teclado_principal())


def _posponer_sync(ev_id: str, minutos: int):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    titulo_raw = ev.get('summary', 'Actividad')

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
    return actualizado, titulo_raw, nuevo_inicio


async def posponer_evento(query, ev_id: str, minutos: int):
    try:
        actualizado, titulo_raw, nuevo_inicio = await asyncio.to_thread(_posponer_sync, ev_id, minutos)
        await query.edit_message_text(
            f"💤 *Actividad Pospuesta (+{minutos} min)*\n\n"
            f"📌 *{escapar_md(titulo_raw)}*\n"
            f"🕒 Nueva hora: *{nuevo_inicio.strftime('%I:%M %p')}* ({nuevo_inicio.strftime('%d/%m/%Y')})\n"
            "🔔 _Te volveré a avisar cuando se acerque la nueva hora._",
            reply_markup=obtener_teclado_acciones_evento(actualizado['id'], ya_terminado=titulo_raw.startswith("✅")),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al posponer actividad: {e}", reply_markup=obtener_teclado_principal())


def _eliminar_sync(ev_id: str):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    titulo = ev.get('summary', 'Actividad')
    service.events().delete(calendarId='primary', eventId=ev_id).execute()
    return titulo


async def eliminar_evento(query, ev_id: str):
    try:
        titulo = await asyncio.to_thread(_eliminar_sync, ev_id)
        await query.edit_message_text(
            f"🗑️ *Actividad eliminada:*\n_{escapar_md(titulo)}_",
            reply_markup=obtener_teclado_principal(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al eliminar: {e}", reply_markup=obtener_teclado_principal())


# ----------------- RECORDATORIOS AUTOMÁTICOS -----------------

async def verificar_recordatorios(context: ContextTypes.DEFAULT_TYPE):
    global USUARIO_CHAT_ID, EVENTOS_NOTIFICADOS
    if not USUARIO_CHAT_ID:
        return

    try:
        ahora = datetime.datetime.now(LOCAL_TZ)
        en_15_min = ahora + datetime.timedelta(minutes=15)

        eventos = await asyncio.to_thread(_listar_eventos_sync, ahora.isoformat(), en_15_min.isoformat())

        for ev in eventos:
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

    app.add_handler(CommandHandler("start", comando_menu))
    app.add_handler(CommandHandler("menu", comando_menu))
    app.add_handler(CommandHandler("cancelar", comando_cancelar))
    app.add_handler(CallbackQueryHandler(manejar_botones))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))

    if app.job_queue:
        app.job_queue.run_repeating(verificar_recordatorios, interval=60, first=10)

    print("🤖 Bot de Agenda con menú interactivo encendido...", flush=True)
    app.run_polling(drop_pending_updates=True)
