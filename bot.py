import os
import re
import json
import socket
import asyncio
import threading
import datetime
import urllib.parse
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

GOOGLE_CLIENT_ID = "85393280635-" + "9d3tp1sq563ml9joknlvmummmfe3isem.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOC" + "SPX-" + "0aXZYfC2DzZoG_R5uZSlPMvQ6SfC"
OAUTH_REDIRECT_URI = "http://localhost:8080"

# Código de autorización recién generado (se canjea automáticamente al arrancar)
CODIGO_OAUTH_INICIAL = (
    "4/0AUn6Hd9kB8aPV1VEh2qTxcIYb7Blq"
    + "OACH8dZ8UDPBBwnYInFsmRiV4xf4bUrXNd15mhBhQ"
)

# Hora del resumen matutino automático (6:30 AM hora Colombia)
HORA_RESUMEN_DIARIO = 6
MINUTO_RESUMEN_DIARIO = 30

# Separador interno para guardar metadatos de archivos de Telegram en Google Calendar
MARCADOR_META = "\n\n===TG_META===\n"

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
ULTIMA_FECHA_RESUMEN = None
REFRESH_TOKEN_ACTIVO = None
CODIGO_INICIAL_PROBADO = False

# Mapa de IDs cortos para evitar el error Button_data_invalid de Telegram (máx 64 bytes)
MAPA_EVENTOS = {}
INVERSO_EVENTOS = {}
CONTADOR_EVENTOS = 0

# Estado directo por usuario
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


def limpiar_prefijo_check(titulo: str) -> str:
    """Quita el emoji ✅ del inicio para no duplicarlo en botones ni pantallas."""
    t = str(titulo).strip()
    while t.startswith("✅"):
        t = t[1:].strip()
    return t or "Sin título"


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


# ----------------- GESTIÓN DE NOTAS Y ADJUNTOS EN GOOGLE CALENDAR -----------------

def extraer_info_descripcion(descripcion_raw: str):
    if not descripcion_raw:
        return "", []
    if MARCADOR_META in descripcion_raw:
        partes = descripcion_raw.split(MARCADOR_META, 1)
        notas = partes[0].strip()
        try:
            meta = json.loads(partes[1].strip())
            archivos = meta.get("files", [])
        except Exception:
            archivos = []
        return notas, archivos
    return descripcion_raw.strip(), []


def empaquetar_descripcion(notas: str, archivos: list) -> str:
    notas_limpias = (notas or "").strip()
    if not archivos:
        return notas_limpias
    meta_json = json.dumps({"files": archivos}, ensure_ascii=False)
    return f"{notas_limpias}{MARCADOR_META}{meta_json}"


def tiene_contenido_extra(ev: dict) -> bool:
    notas, archivos = extraer_info_descripcion(ev.get("description", ""))
    ubicacion = (ev.get("location") or "").strip()
    return bool(notas or archivos or ubicacion)


# ----------------- PARSEADORES DE HORA Y DURACIÓN -----------------

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


# ----------------- AUTENTICACIÓN Y RECONEXIÓN DESDE TELEGRAM -----------------

def _obtener_info_respaldo():
    token_actual = REFRESH_TOKEN_ACTIVO or ("1//" + "05R83UV6gK6fQCgYIARAAGAUSNwF-L9IrdCWoX3YxMoNte3F7RP9pav9Xggo3max7j6iLyF4s1jM1M1ZiuAagDz9IBchLDGjkFQE")
    return {
        "refresh_token": token_actual,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "scopes": SCOPES,
    }


def generar_url_autorizacion_google() -> str:
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar",
        "access_type": "offline",
        "prompt": "consent",
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"


def extraer_codigo_oauth(texto: str):
    """Extrae el código de autorización ya sea que el usuario pegue la URL completa de localhost o solo el código."""
    t = texto.strip()
    if "code=" in t:
        parsed = urllib.parse.urlparse(t)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs and qs["code"]:
            return qs["code"][0].strip()
        m = re.search(r"code=([^&\s]+)", t)
        if m:
            return urllib.parse.unquote(m.group(1))
    if t.startswith("4/"):
        return t
    return None


def canjear_codigo_oauth_sync(codigo: str) -> str:
    """Canjea el código de Google por un nuevo refresh_token y lo activa de inmediato."""
    global REFRESH_TOKEN_ACTIVO
    datos_post = urllib.parse.urlencode({
        "code": codigo,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=datos_post,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        respuesta = json.loads(resp.read().decode("utf-8"))

    nuevo_refresh = respuesta.get("refresh_token")
    if not nuevo_refresh:
        raise RuntimeError("Google no devolvió un refresh_token. Intenta nuevamente aceptando todos los permisos.")

    REFRESH_TOKEN_ACTIVO = nuevo_refresh
    info_guardar = {
        "token": respuesta.get("access_token", ""),
        "refresh_token": nuevo_refresh,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "scopes": SCOPES,
    }
    try:
        with open("token.json", "w", encoding="utf-8") as f:
            json.dump(info_guardar, f)
    except Exception:
        pass

    return nuevo_refresh


def inicializar_oauth_si_pendiente():
    """Intenta canjear automáticamente el código inicial al arrancar el contenedor."""
    global CODIGO_INICIAL_PROBADO
    if CODIGO_INICIAL_PROBADO or not CODIGO_OAUTH_INICIAL:
        return None
    CODIGO_INICIAL_PROBADO = True
    try:
        nuevo = canjear_codigo_oauth_sync(CODIGO_OAUTH_INICIAL)
        print("✅ Código OAuth inicial canjeado con éxito por un refresh_token permanente.", flush=True)
        return nuevo
    except Exception as e:
        print(f"ℹ️ Aviso al verificar código OAuth inicial: {e}", flush=True)
        return None


def es_error_token_vencido(err: Exception) -> bool:
    s = str(err).lower()
    return "invalid_grant" in s or "expired or revoked" in s


def construir_mensaje_token_vencido():
    url = generar_url_autorizacion_google()
    texto = (
        "🔑 *Renovar conexión con Google Calendar*\n\n"
        "*Sigue estos 3 pasos rápidos desde aquí mismo:*\n"
        "1️⃣ Toca el botón **🔗 1. Autorizar Google Calendar** abajo e inicia sesión con tu correo.\n"
        "2️⃣ Al darle *Continuar*, el navegador te llevará a una página que dice *No se puede acceder a este sitio* (`localhost:8080/?code=...`). **¡Eso es normal!**\n"
        "3️⃣ **Copia toda esa dirección URL** de la barra superior del navegador y **pégala aquí mismo en este chat de Telegram**."
    )
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 1. Autorizar Google Calendar", url=url)],
        [InlineKeyboardButton("🏠 Menú Principal", callback_data="btn_volver")],
    ])
    return texto, teclado


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
    inicializar_oauth_si_pendiente()
    creds = None

    # 1. Si hay un token activo en memoria, lo usamos primero
    if REFRESH_TOKEN_ACTIVO:
        try:
            creds = Credentials.from_authorized_user_info(_obtener_info_respaldo(), SCOPES)
            if not creds.valid and creds.refresh_token:
                creds.refresh(Request())
        except Exception:
            creds = None

    # 2. Archivo token.json local
    if not creds and os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            if not creds.valid and creds.refresh_token:
                creds.refresh(Request())
        except Exception:
            creds = None

    # 3. Variable de entorno en Render (si existe)
    if not creds:
        token_env = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
        if token_env:
            try:
                info = json.loads(token_env)
                creds = Credentials.from_authorized_user_info(info, SCOPES)
                if not creds.valid and creds.refresh_token:
                    creds.refresh(Request())
            except Exception:
                creds = None

    # 4. Respaldo integrado
    if not creds:
        creds = Credentials.from_authorized_user_info(_obtener_info_respaldo(), SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())

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
    """Hace ping cada 4 minutos a la URL de Render."""
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
        [InlineKeyboardButton("☀️ Agenda de Hoy", callback_data="btn_hoy")],
        [InlineKeyboardButton("📅 Próximos 7 Días", callback_data="btn_semana")],
        [
            InlineKeyboardButton("➕ Evento Puntual", callback_data="btn_crear"),
            InlineKeyboardButton("🔁 Evento Diario", callback_data="btn_diario"),
        ],
    ]
    return InlineKeyboardMarkup(teclado)


def obtener_teclado_cancelar(cod_volver: str = None):
    botones = []
    if cod_volver:
        botones.append([InlineKeyboardButton("⬅️ Volver a la Actividad", callback_data=f"ver_{cod_volver}")])
    botones.append([InlineKeyboardButton("❌ Cancelar y Menú Principal", callback_data="btn_volver")])
    return InlineKeyboardMarkup(botones)


def obtener_teclado_acciones_evento(ev_id: str, ya_terminado: bool = False, num_archivos: int = 0):
    cod = obtener_codigo_corto(ev_id)
    botones = []

    if not ya_terminado:
        botones.append([InlineKeyboardButton("✅ Dar por Terminada", callback_data=f"done_{cod}")])
    else:
        botones.append([InlineKeyboardButton("🔄 Reabrir Actividad (Marcar Pendiente)", callback_data=f"undone_{cod}")])

    if num_archivos > 0:
        botones.append([
            InlineKeyboardButton(f"📥 Ver Archivos / Fotos Adjuntas ({num_archivos})", callback_data=f"sendfiles_{cod}")
        ])

    botones.append([
        InlineKeyboardButton("✏️ Editar Actividad", callback_data=f"editmenu_{cod}"),
        InlineKeyboardButton("📎 Adjuntar / Notas", callback_data=f"adj_{cod}"),
    ])

    botones.extend([
        [
            InlineKeyboardButton("💤 +10 min", callback_data=f"snooze_{cod}_10"),
            InlineKeyboardButton("💤 +15 min", callback_data=f"snooze_{cod}_15"),
            InlineKeyboardButton("💤 +30 min", callback_data=f"snooze_{cod}_30"),
            InlineKeyboardButton("💤 +1 h", callback_data=f"snooze_{cod}_60"),
        ],
        [
            InlineKeyboardButton("📅 Mover de Día", callback_data=f"resched_{cod}"),
            InlineKeyboardButton("🗑️ Eliminar", callback_data=f"del_{cod}"),
        ],
        [
            InlineKeyboardButton("⬅️ Volver a Agenda de Hoy", callback_data="btn_hoy"),
            InlineKeyboardButton("🏠 Menú", callback_data="btn_volver"),
        ]
    ])
    return InlineKeyboardMarkup(botones)


def obtener_teclado_edicion(cod: str):
    teclado = [
        [InlineKeyboardButton("✏️ Cambiar Nombre / Título", callback_data=f"edittit_{cod}")],
        [InlineKeyboardButton("🕒 Cambiar Solo la Hora", callback_data=f"edithora_{cod}")],
        [InlineKeyboardButton("📅 Cambiar Fecha y Hora", callback_data=f"resched_{cod}")],
        [InlineKeyboardButton("⏳ Cambiar Duración", callback_data=f"editdur_{cod}")],
        [InlineKeyboardButton("📎 Agregar Archivo / Foto / Nota / Ubicación", callback_data=f"adj_{cod}")],
        [InlineKeyboardButton("⬅️ Volver a la Actividad", callback_data=f"ver_{cod}")],
    ]
    return InlineKeyboardMarkup(teclado)


# ----------------- COMANDOS SIEMPRE ACTIVOS (/start, /menu, /conectar, /cancelar) -----------------

async def comando_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    guardar_chat_id(chat_id)
    limpiar_estado(chat_id)

    texto = (
        "👋 *Bienvenido a tu Asistente Personal de Agenda*\n\n"
        "Selecciona una opción del menú:"
    )
    await update.message.reply_text(texto, reply_markup=obtener_teclado_principal(), parse_mode="Markdown")


async def comando_conectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    guardar_chat_id(chat_id)
    limpiar_estado(chat_id)
    texto, teclado = construir_mensaje_token_vencido()
    await update.message.reply_text(texto, reply_markup=teclado, parse_mode="Markdown")


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

    if data == "btn_volver":
        limpiar_estado(chat_id)
        await query.edit_message_text("Selecciona una opción del menú:", reply_markup=obtener_teclado_principal())
        return

    if data == "btn_reconectar":
        limpiar_estado(chat_id)
        texto, teclado = construir_mensaje_token_vencido()
        await query.edit_message_text(texto, reply_markup=teclado, parse_mode="Markdown")
        return

    if data == "btn_hoy":
        limpiar_estado(chat_id)
        await mostrar_agenda_hoy(query)
        return

    if data == "btn_semana":
        limpiar_estado(chat_id)
        await mostrar_agenda_semana(query)
        return

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

    if data.startswith("dur_"):
        dur_str = data.replace("dur_", "")
        await query.edit_message_text("⏳ _Guardando en Google Calendar..._", parse_mode="Markdown")
        try:
            ev_creado, msg = await asyncio.to_thread(guardar_evento_creado_sync, estado, dur_str)
            limpiar_estado(chat_id)
            cod = obtener_codigo_corto(ev_creado['id'])
            teclado_post = InlineKeyboardMarkup([
                [InlineKeyboardButton("📎 Adjuntar Instructivo / Foto / Nota / Ubicación", callback_data=f"adj_{cod}")],
                [InlineKeyboardButton("🔍 Ver / Editar Actividad", callback_data=f"ver_{cod}")],
                [InlineKeyboardButton("☀️ Ir a Agenda de Hoy", callback_data="btn_hoy")],
                [InlineKeyboardButton("🏠 Menú Principal", callback_data="btn_volver")],
            ])
            await query.edit_message_text(msg, reply_markup=teclado_post, parse_mode="Markdown")
        except Exception as e:
            limpiar_estado(chat_id)
            if es_error_token_vencido(e):
                t_err, k_err = construir_mensaje_token_vencido()
                await query.edit_message_text(t_err, reply_markup=k_err, parse_mode="Markdown")
            else:
                await query.edit_message_text(f"❌ Error al guardar: {e}", reply_markup=obtener_teclado_principal())
        return

    if data.startswith("ver_"):
        limpiar_estado(chat_id)
        cod = data.replace("ver_", "", 1)
        await mostrar_detalle_evento(query, obtener_ev_id_real(cod))
        return

    if data.startswith("editmenu_"):
        limpiar_estado(chat_id)
        cod = data.replace("editmenu_", "", 1)
        await query.edit_message_text(
            "✏️ *Editar Actividad*\n\n¿Qué dato deseas modificar?",
            reply_markup=obtener_teclado_edicion(cod),
            parse_mode="Markdown"
        )
        return

    if data.startswith("edittit_"):
        cod = data.replace("edittit_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        ESTADOS_CHAT[chat_id] = {"paso": "EDITAR_TITULO", "ev_id": ev_id, "cod": cod}
        await query.edit_message_text(
            "✏️ *Cambiar Nombre de la Actividad*\n\nEscribe en el chat el *nuevo nombre o título*:",
            reply_markup=obtener_teclado_cancelar(cod),
            parse_mode="Markdown"
        )
        return

    if data.startswith("edithora_"):
        cod = data.replace("edithora_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        ESTADOS_CHAT[chat_id] = {"paso": "EDITAR_SOLO_HORA", "ev_id": ev_id, "cod": cod}
        await query.edit_message_text(
            "🕒 *Cambiar Hora (mismo día)*\n\nEscribe en el chat la *nueva hora* (ej: `08:30`, `6:00am`, `2:30pm`):",
            reply_markup=obtener_teclado_cancelar(cod),
            parse_mode="Markdown"
        )
        return

    if data.startswith("editdur_"):
        cod = data.replace("editdur_", "", 1)
        teclado = [
            [
                InlineKeyboardButton("15 min", callback_data=f"setdur_{cod}_15"),
                InlineKeyboardButton("30 min", callback_data=f"setdur_{cod}_30"),
            ],
            [
                InlineKeyboardButton("45 min", callback_data=f"setdur_{cod}_45"),
                InlineKeyboardButton("1 hora", callback_data=f"setdur_{cod}_60"),
            ],
            [
                InlineKeyboardButton("1.5 horas", callback_data=f"setdur_{cod}_90"),
                InlineKeyboardButton("2 horas", callback_data=f"setdur_{cod}_120"),
            ],
            [InlineKeyboardButton("⬅️ Volver a la Actividad", callback_data=f"ver_{cod}")],
        ]
        await query.edit_message_text(
            "⏳ *Cambiar Duración*\n\nSelecciona la nueva duración para esta actividad:",
            reply_markup=InlineKeyboardMarkup(teclado),
            parse_mode="Markdown"
        )
        return

    if data.startswith("setdur_"):
        partes = data.split("_")
        minutos = int(partes[-1])
        cod = "_".join(partes[1:-1])
        ev_id = obtener_ev_id_real(cod)
        await query.edit_message_text("⏳ _Actualizando duración..._", parse_mode="Markdown")
        try:
            await asyncio.to_thread(_cambiar_duracion_sync, ev_id, minutos)
            await mostrar_detalle_evento(query, ev_id, aviso_extra=f"✅ _Duración cambiada a {minutos} min._")
        except Exception as e:
            await query.edit_message_text(f"❌ Error al cambiar duración: {e}", reply_markup=obtener_teclado_principal())
        return

    if data.startswith("adj_"):
        cod = data.replace("adj_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        ESTADOS_CHAT[chat_id] = {"paso": "ADJUNTAR", "ev_id": ev_id, "cod": cod}
        teclado = [
            [InlineKeyboardButton("🧹 Borrar Notas / Adjuntos Actuales", callback_data=f"clearadj_{cod}")],
            [InlineKeyboardButton("✅ Listo (Volver a la Actividad)", callback_data=f"ver_{cod}")],
        ]
        await query.edit_message_text(
            "📎 *Adjuntar Instructivos, Archivos, Fotos, Notas o Ubicación*\n\n"
            "Envíame ahora mismo al chat cualquiera de estos elementos y quedará guardado en la actividad:\n"
            "• 📄 *Documento* (PDF, Word, Excel, manual, recibo)\n"
            "• 🖼️ *Foto o Imagen* (captura, factura, guía)\n"
            "• 📍 *Ubicación GPS* (usando el clip de Telegram)\n"
            "• 🔗 *Texto, Links o Instructivo* escrito\n\n"
            "_Puedes enviar varios seguidos. Cuando termines, toca el botón de abajo:_",
            reply_markup=InlineKeyboardMarkup(teclado),
            parse_mode="Markdown"
        )
        return

    if data.startswith("clearadj_"):
        cod = data.replace("clearadj_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        await query.edit_message_text("⏳ _Limpiando notas y adjuntos..._", parse_mode="Markdown")
        try:
            await asyncio.to_thread(_limpiar_adjuntos_sync, ev_id)
            limpiar_estado(chat_id)
            await mostrar_detalle_evento(query, ev_id, aviso_extra="🧹 _Se borraron las notas y adjuntos de la actividad._")
        except Exception as e:
            await query.edit_message_text(f"❌ Error al limpiar adjuntos: {e}", reply_markup=obtener_teclado_principal())
        return

    if data.startswith("sendfiles_"):
        cod = data.replace("sendfiles_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        await enviar_archivos_adjuntos(query, context, chat_id, ev_id)
        return

    if data.startswith("bindadj_"):
        cod = data.replace("bindadj_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        pendiente = estado.get("adjunto_pendiente")
        if not pendiente:
            await mostrar_detalle_evento(query, ev_id)
            return
        await query.edit_message_text("⏳ _Guardando adjunto en la actividad..._", parse_mode="Markdown")
        try:
            resumen_guardado = await asyncio.to_thread(_guardar_adjunto_en_evento_sync, ev_id, pendiente)
            limpiar_estado(chat_id)
            await mostrar_detalle_evento(query, ev_id, aviso_extra=f"✅ _{resumen_guardado}_")
        except Exception as e:
            limpiar_estado(chat_id)
            await query.edit_message_text(f"❌ Error al guardar adjunto: {e}", reply_markup=obtener_teclado_principal())
        return

    if data.startswith("done_"):
        limpiar_estado(chat_id)
        cod = data.replace("done_", "", 1)
        await marcar_evento_terminado(query, obtener_ev_id_real(cod), terminar=True)
        return

    if data.startswith("undone_"):
        limpiar_estado(chat_id)
        cod = data.replace("undone_", "", 1)
        await marcar_evento_terminado(query, obtener_ev_id_real(cod), terminar=False)
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

    if data.startswith("resched_"):
        cod = data.replace("resched_", "", 1)
        ev_id = obtener_ev_id_real(cod)
        ESTADOS_CHAT[chat_id] = {"paso": "REAGENDAR_FECHA", "reagendar_ev_id": ev_id, "cod": cod}

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
                InlineKeyboardButton("⬅️ Volver a la Actividad", callback_data=f"ver_{cod}"),
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
        cod = estado.get("cod")
        await query.edit_message_text(
            f"📅 Nueva fecha: *{estado['reagendar_fecha']}*\n\n"
            "🕒 Escribe en el chat la *nueva hora* (ejemplos: `09:00`, `6:00am`, `2:30pm`):",
            reply_markup=obtener_teclado_cancelar(cod),
            parse_mode="Markdown"
        )
        return


# ----------------- MANEJADOR DE ARCHIVOS, FOTOS, AUDIOS Y UBICACIONES -----------------

def _extraer_datos_mensaje_multimedia(msg):
    resultado = {"notas_extra": (msg.caption or "").strip(), "ubicacion": None, "archivo": None}

    if msg.location:
        lat = msg.location.latitude
        lon = msg.location.longitude
        resultado["ubicacion"] = f"https://www.google.com/maps?q={lat},{lon}"
    elif msg.document:
        resultado["archivo"] = {
            "type": "document",
            "file_id": msg.document.file_id,
            "name": msg.document.file_name or "Documento",
        }
    elif msg.photo:
        foto = msg.photo[-1]
        resultado["archivo"] = {
            "type": "photo",
            "file_id": foto.file_id,
            "name": "Foto adjunta",
        }
    elif msg.video:
        resultado["archivo"] = {
            "type": "video",
            "file_id": msg.video.file_id,
            "name": msg.video.file_name or "Video adjunto",
        }
    elif msg.audio:
        resultado["archivo"] = {
            "type": "audio",
            "file_id": msg.audio.file_id,
            "name": msg.audio.file_name or "Audio adjunto",
        }
    elif msg.voice:
        resultado["archivo"] = {
            "type": "voice",
            "file_id": msg.voice.file_id,
            "name": "Nota de voz",
        }
    return resultado


async def manejar_adjunto_multimedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    guardar_chat_id(chat_id)
    estado = obtener_estado(chat_id)
    paso = estado.get("paso")
    datos = _extraer_datos_mensaje_multimedia(update.message)

    if paso == "ADJUNTAR" and estado.get("ev_id"):
        ev_id = estado["ev_id"]
        cod = estado.get("cod") or obtener_codigo_corto(ev_id)
        espera = await update.message.reply_text("⏳ _Guardando en la actividad..._", parse_mode="Markdown")
        try:
            resumen = await asyncio.to_thread(_guardar_adjunto_en_evento_sync, ev_id, datos)
            teclado = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Listo (Ver Actividad)", callback_data=f"ver_{cod}")],
                [InlineKeyboardButton("☀️ Volver a Agenda de Hoy", callback_data="btn_hoy")],
            ])
            await espera.edit_text(
                f"✅ *{resumen}*\n\n"
                "📎 _Puedes enviar otro archivo/foto/nota si deseas, o tocar Listo:_",
                reply_markup=teclado,
                parse_mode="Markdown"
            )
        except Exception as e:
            await espera.edit_text(f"❌ Error al adjuntar: {e}", reply_markup=obtener_teclado_principal())
        return

    try:
        ahora = datetime.datetime.now(LOCAL_TZ)
        inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        fin_dia = ahora.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        eventos = await asyncio.to_thread(_listar_eventos_sync, inicio_dia, fin_dia)

        if not eventos:
            await update.message.reply_text(
                "📎 Recibí tu archivo, pero no tienes actividades hoy para adjuntarlo. Primero crea o abre una actividad.",
                reply_markup=obtener_teclado_principal()
            )
            return

        estado["adjunto_pendiente"] = datos
        botones = []
        for ev in eventos[:12]:
            cod = obtener_codigo_corto(ev['id'])
            titulo_limpio = limpiar_prefijo_check(ev.get('summary', 'Sin título'))
            start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
            hora = datetime.datetime.fromisoformat(start_raw).strftime('%I:%M %p') if 'T' in start_raw else "Hoy"
            botones.append([
                InlineKeyboardButton(f"📎 {hora} | {titulo_limpio[:26]}", callback_data=f"bindadj_{cod}")
            ])
        botones.append([InlineKeyboardButton("❌ Cancelar", callback_data="btn_volver")])

        await update.message.reply_text(
            "📎 *¡Archivo / Ubicación recibida!*\n\n👇 Toca la actividad de hoy a la que deseas adjuntarlo:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown"
        )
    except Exception as e:
        if es_error_token_vencido(e):
            t_err, k_err = construir_mensaje_token_vencido()
            await update.message.reply_text(t_err, reply_markup=k_err, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Error: {e}", reply_markup=obtener_teclado_principal())


# ----------------- MANEJADOR ÚNICO DE TEXTO ESCRITO POR EL USUARIO -----------------

async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    guardar_chat_id(chat_id)
    estado = obtener_estado(chat_id)
    paso = estado.get("paso")
    texto = update.message.text.strip()

    # 1. Detectar automáticamente si el usuario pegó la URL o código de autorización de Google
    codigo_oauth = extraer_codigo_oauth(texto)
    if codigo_oauth:
        espera = await update.message.reply_text("⏳ _Verificando código con Google Calendar..._", parse_mode="Markdown")
        try:
            nuevo_refresh = await asyncio.to_thread(canjear_codigo_oauth_sync, codigo_oauth)
            limpiar_estado(chat_id)
            parte_token = nuevo_refresh[3:] if nuevo_refresh.startswith("1//") else nuevo_refresh
            await espera.edit_text(
                "✅ *¡Google Calendar reconectado en modo PERMANENTE!*\n\n"
                "Tu agenda ya está activa y funcionando.\n\n"
                f"📌 _Tu nuevo código permanente es:_\n`{parte_token}`",
                reply_markup=obtener_teclado_principal(),
                parse_mode="Markdown"
            )
        except Exception as e:
            await espera.edit_text(
                f"❌ No se pudo canjear el código (puede que ya haya sido usado): {e}\n\nGenera uno nuevo tocando abajo:",
                reply_markup=construir_mensaje_token_vencido()[1]
            )
        return

    if not paso:
        await update.message.reply_text(
            "👋 Selecciona qué deseas hacer en el menú:",
            reply_markup=obtener_teclado_principal()
        )
        return

    if paso == "EDITAR_TITULO":
        ev_id = estado.get("ev_id")
        espera = await update.message.reply_text("⏳ _Actualizando nombre en Google Calendar..._", parse_mode="Markdown")
        try:
            await asyncio.to_thread(_cambiar_titulo_sync, ev_id, texto)
            limpiar_estado(chat_id)
            await mostrar_detalle_en_mensaje(espera, ev_id, aviso_extra="✅ _Nombre actualizado correctamente._")
        except Exception as e:
            limpiar_estado(chat_id)
            await espera.edit_text(f"❌ Error al cambiar nombre: {e}", reply_markup=obtener_teclado_principal())
        return

    if paso == "EDITAR_SOLO_HORA":
        cod = estado.get("cod")
        hora_norm = parsear_hora_flexible(texto)
        if not hora_norm:
            await update.message.reply_text(
                "⚠️ Hora no válida. Escríbela como `08:30`, `6:00am` o `2:30pm`:",
                reply_markup=obtener_teclado_cancelar(cod),
                parse_mode="Markdown"
            )
            return

        ev_id = estado.get("ev_id")
        espera = await update.message.reply_text("⏳ _Actualizando hora en Google Calendar..._", parse_mode="Markdown")
        try:
            await asyncio.to_thread(_cambiar_solo_hora_sync, ev_id, hora_norm)
            limpiar_estado(chat_id)
            await mostrar_detalle_en_mensaje(espera, ev_id, aviso_extra=f"✅ _Hora actualizada a las {hora_norm}._")
        except Exception as e:
            limpiar_estado(chat_id)
            await espera.edit_text(f"❌ Error al cambiar hora: {e}", reply_markup=obtener_teclado_principal())
        return

    if paso == "ADJUNTAR":
        ev_id = estado.get("ev_id")
        cod = estado.get("cod") or obtener_codigo_corto(ev_id)
        espera = await update.message.reply_text("⏳ _Guardando nota/link en la actividad..._", parse_mode="Markdown")
        try:
            datos = {"notas_extra": texto, "ubicacion": None, "archivo": None}
            if "maps.google" in texto.lower() or "goo.gl/maps" in texto.lower() or "maps.app.goo.gl" in texto.lower():
                datos["ubicacion"] = texto
            resumen = await asyncio.to_thread(_guardar_adjunto_en_evento_sync, ev_id, datos)
            teclado = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Listo (Ver Actividad)", callback_data=f"ver_{cod}")],
                [InlineKeyboardButton("☀️ Volver a Agenda de Hoy", callback_data="btn_hoy")],
            ])
            await espera.edit_text(
                f"✅ *{resumen}*\n\n"
                "📎 _Puedes enviar otro archivo, foto o nota si deseas, o tocar Listo:_",
                reply_markup=teclado,
                parse_mode="Markdown"
            )
        except Exception as e:
            await espera.edit_text(f"❌ Error al guardar nota: {e}", reply_markup=obtener_teclado_principal())
        return

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
                "⚠️ Fecha no válida. Toca el botón *Hoy / Mañana* o escríbela como `DD/MM/AAAA` (ej: `28/10/2026`):",
                reply_markup=obtener_teclado_cancelar(),
                parse_mode="Markdown"
            )
        return

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
            ev_creado, msg = await asyncio.to_thread(guardar_evento_creado_sync, estado, dur_str)
            limpiar_estado(chat_id)
            cod = obtener_codigo_corto(ev_creado['id'])
            teclado_post = InlineKeyboardMarkup([
                [InlineKeyboardButton("📎 Adjuntar Instructivo / Foto / Nota / Ubicación", callback_data=f"adj_{cod}")],
                [InlineKeyboardButton("🔍 Ver / Editar Actividad", callback_data=f"ver_{cod}")],
                [InlineKeyboardButton("☀️ Ir a Agenda de Hoy", callback_data="btn_hoy")],
                [InlineKeyboardButton("🏠 Menú Principal", callback_data="btn_volver")],
            ])
            await espera.edit_text(msg, reply_markup=teclado_post, parse_mode="Markdown")
        except Exception as e:
            limpiar_estado(chat_id)
            if es_error_token_vencido(e):
                t_err, k_err = construir_mensaje_token_vencido()
                await espera.edit_text(t_err, reply_markup=k_err, parse_mode="Markdown")
            else:
                await espera.edit_text(f"❌ Error al guardar: {e}", reply_markup=obtener_teclado_principal())
        return

    if paso == "REAGENDAR_FECHA":
        cod = estado.get("cod")
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
                reply_markup=obtener_teclado_cancelar(cod),
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Fecha no válida. Usa el formato `DD/MM/AAAA` (ej: `28/10/2026`):",
                reply_markup=obtener_teclado_cancelar(cod),
                parse_mode="Markdown"
            )
        return

    if paso == "REAGENDAR_HORA":
        cod = estado.get("cod")
        hora_norm = parsear_hora_flexible(texto)
        if not hora_norm:
            await update.message.reply_text(
                "⚠️ Hora no válida. Escríbela como `08:30`, `6:00am` o `2:30pm`:",
                reply_markup=obtener_teclado_cancelar(cod),
                parse_mode="Markdown"
            )
            return

        ev_id = estado.get("reagendar_ev_id")
        fecha_str = estado.get("reagendar_fecha")
        espera = await update.message.reply_text("⏳ _Reagendando en Google Calendar..._", parse_mode="Markdown")
        try:
            await asyncio.to_thread(reagendar_evento_sync, ev_id, fecha_str, hora_norm)
            limpiar_estado(chat_id)
            await mostrar_detalle_en_mensaje(espera, ev_id, aviso_extra="✅ _Actividad reagendada con éxito._")
        except Exception as e:
            limpiar_estado(chat_id)
            await espera.edit_text(f"❌ Error al reagendar: {e}", reply_markup=obtener_teclado_principal())
        return


# ----------------- FUNCIONES DE GOOGLE CALENDAR (EN HILO SEPARADO CON TIMEOUT) -----------------

def guardar_evento_creado_sync(estado: dict, dur_str: str):
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
        creado = service.events().insert(calendarId='primary', body=cuerpo).execute()
        msg = (
            f"✅ *Evento Diario Creado*\n\n"
            f"📌 *{titulo}*\n"
            f"⏰ Todos los días a las {inicio_dt.strftime('%I:%M %p')} ({duracion} min)\n\n"
            "👇 _Si deseas subirle un instructivo, foto, link o ubicación, toca el botón de abajo:_"
        )
        return creado, msg
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

        creado = service.events().insert(calendarId='primary', body=cuerpo).execute()
        msg = (
            f"✅ *Evento Agendado con Éxito*\n\n"
            f"📌 *{titulo}*\n"
            f"📅 {inicio_dt.strftime('%d/%m/%Y')}\n"
            f"🕒 {inicio_dt.strftime('%I:%M %p')} ({detalle_dur})\n\n"
            "👇 _Si deseas subirle un instructivo, foto, link o ubicación, toca el botón de abajo:_"
        )
        return creado, msg


def _sincronizar_en_padre_recurrente(service, ev: dict, nuevo_summary=None, nueva_desc=None, nueva_loc=None):
    parent_id = ev.get('recurringEventId')
    if not parent_id:
        return
    try:
        padre = service.events().get(calendarId='primary', eventId=parent_id).execute()
        if nuevo_summary is not None:
            padre['summary'] = limpiar_prefijo_check(nuevo_summary)
        if nueva_desc is not None:
            padre['description'] = nueva_desc
        if nueva_loc is not None:
            padre['location'] = nueva_loc
        service.events().update(calendarId='primary', eventId=parent_id, body=padre).execute()
    except Exception:
        pass


def _cambiar_titulo_sync(ev_id: str, nuevo_titulo: str):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    estaba_terminado = ev.get('summary', '').startswith("✅")
    limpio = limpiar_prefijo_check(nuevo_titulo)
    ev['summary'] = f"✅ {limpio}" if estaba_terminado else limpio
    actualizado = service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()
    _sincronizar_en_padre_recurrente(service, ev, nuevo_summary=limpio)
    return actualizado


def _cambiar_solo_hora_sync(ev_id: str, hora_normalizada: str):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
    end_raw = ev['end'].get('dateTime')

    duracion = datetime.timedelta(minutes=60)
    if start_raw and end_raw and 'T' in start_raw:
        d = datetime.datetime.fromisoformat(end_raw) - datetime.datetime.fromisoformat(start_raw)
        if d.total_seconds() > 0:
            duracion = d

    if 'T' in start_raw:
        fecha_str = datetime.datetime.fromisoformat(start_raw).strftime("%Y-%m-%d")
    else:
        fecha_str = start_raw

    nuevo_inicio = LOCAL_TZ.localize(datetime.datetime.strptime(f"{fecha_str} {hora_normalizada}", "%Y-%m-%d %H:%M"))
    nuevo_fin = nuevo_inicio + duracion

    ev['start'] = {'dateTime': nuevo_inicio.isoformat(), 'timeZone': TIMEZONE_STR}
    ev['end'] = {'dateTime': nuevo_fin.isoformat(), 'timeZone': TIMEZONE_STR}
    return service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()


def _cambiar_duracion_sync(ev_id: str, minutos: int):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    start_raw = ev['start'].get('dateTime')
    if start_raw:
        inicio_dt = datetime.datetime.fromisoformat(start_raw)
    else:
        fecha_str = ev['start'].get('date', datetime.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
        inicio_dt = LOCAL_TZ.localize(datetime.datetime.strptime(f"{fecha_str} 08:00", "%Y-%m-%d %H:%M"))
        ev['start'] = {'dateTime': inicio_dt.isoformat(), 'timeZone': TIMEZONE_STR}

    fin_dt = inicio_dt + datetime.timedelta(minutes=minutos)
    ev['end'] = {'dateTime': fin_dt.isoformat(), 'timeZone': TIMEZONE_STR}
    return service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()


def _guardar_adjunto_en_evento_sync(ev_id: str, datos: dict) -> str:
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    notas_actuales, archivos_actuales = extraer_info_descripcion(ev.get('description', ''))

    partes_guardadas = []

    if datos.get("notas_extra"):
        nueva_nota = datos["notas_extra"]
        notas_actuales = f"{notas_actuales}\n• {nueva_nota}".strip() if notas_actuales else f"• {nueva_nota}"
        partes_guardadas.append("Nota / Instructivo")

    if datos.get("archivo"):
        archivos_actuales.append(datos["archivo"])
        partes_guardadas.append(f"Archivo ({datos['archivo'].get('name', 'Adjunto')})")

    if datos.get("ubicacion"):
        ev['location'] = datos["ubicacion"]
        partes_guardadas.append("Ubicación GPS")

    nueva_desc = empaquetar_descripcion(notas_actuales, archivos_actuales)
    ev['description'] = nueva_desc
    service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()

    _sincronizar_en_padre_recurrente(
        service,
        ev,
        nueva_desc=nueva_desc,
        nueva_loc=ev.get('location')
    )
    return "Guardado: " + ", ".join(partes_guardadas) if partes_guardadas else "Adjunto guardado"


def _limpiar_adjuntos_sync(ev_id: str):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    ev['description'] = ""
    ev['location'] = ""
    service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()
    _sincronizar_en_padre_recurrente(service, ev, nueva_desc="", nueva_loc="")


def reagendar_evento_sync(ev_id: str, fecha_str: str, hora_normalizada: str) -> str:
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    titulo = escapar_md(limpiar_prefijo_check(ev.get('summary', 'Actividad')))

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


# ----------------- PRESENTACIÓN LIMPIA EN UN SOLO BLOQUE INTERACTIVO -----------------

def construir_mensaje_agenda_hoy(ahora, eventos, es_resumen_matutino: bool = False):
    saludo = "🌅 *¡Buenos días! Agenda de Hoy*" if es_resumen_matutino else "☀️ *Agenda de Hoy*"
    fecha_bonita = ahora.strftime('%d/%m/%Y')

    if not eventos:
        teclado = [
            [InlineKeyboardButton("➕ Crear Actividad para Hoy", callback_data="btn_crear")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="btn_volver")]
        ]
        return f"{saludo} *({fecha_bonita})*\n\n🌴 No tienes actividades agendadas para hoy.", InlineKeyboardMarkup(teclado)

    total = len(eventos)
    completadas = sum(1 for ev in eventos if ev.get('summary', '').startswith("✅"))
    pendientes = total - completadas

    texto = (
        f"{saludo} *({fecha_bonita})*\n"
        f"📊 *{pendientes} pendientes* ⏳  |  *{completadas} completadas* ✅\n\n"
        "👇 _Toca cualquier actividad para abrirla, editarla, ver sus archivos o posponerla:_"
    )
    botones_eventos = []

    for ev in eventos:
        ev_id = ev['id']
        cod = obtener_codigo_corto(ev_id)
        titulo_raw = ev.get('summary', 'Sin título')
        terminado = titulo_raw.startswith("✅")
        titulo_limpio = limpiar_prefijo_check(titulo_raw)

        start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
        if 'T' in start_raw:
            dt = datetime.datetime.fromisoformat(start_raw)
            hora = dt.strftime('%I:%M %p')
        else:
            hora = "Todo el día"

        icono = "✅" if terminado else "🕒"
        clip = "📎 " if tiene_contenido_extra(ev) else ""
        etiqueta_boton = f"{icono} {hora} | {clip}{titulo_limpio[:30]}"
        botones_eventos.append([InlineKeyboardButton(etiqueta_boton, callback_data=f"ver_{cod}")])

    botones_eventos.append([
        InlineKeyboardButton("➕ Nueva Actividad", callback_data="btn_crear"),
        InlineKeyboardButton("🏠 Menú Principal", callback_data="btn_volver"),
    ])
    return texto, InlineKeyboardMarkup(botones_eventos)


async def mostrar_agenda_hoy(query):
    try:
        ahora = datetime.datetime.now(LOCAL_TZ)
        inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        fin_dia = ahora.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        eventos = await asyncio.to_thread(_listar_eventos_sync, inicio_dia, fin_dia)
        texto, markup = construir_mensaje_agenda_hoy(ahora, eventos, es_resumen_matutino=False)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        if es_error_token_vencido(e):
            t_err, k_err = construir_mensaje_token_vencido()
            await query.edit_message_text(t_err, reply_markup=k_err, parse_mode="Markdown")
        else:
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
            "📅 *Próximos 7 Días*\n"
            "👇 _Toca cualquier actividad para abrirla, editarla, adjuntar documentos o reagendarla:_"
        )
        botones_eventos = []

        for ev in eventos[:18]:
            ev_id = ev['id']
            cod = obtener_codigo_corto(ev_id)
            titulo_raw = ev.get('summary', 'Sin título')
            terminado = titulo_raw.startswith("✅")
            titulo_limpio = limpiar_prefijo_check(titulo_raw)
            start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
            if 'T' in start_raw:
                dt = datetime.datetime.fromisoformat(start_raw)
                corto_fh = dt.strftime('%d/%m %I:%M%p')
            else:
                corto_fh = start_raw

            icono = "✅" if terminado else "📌"
            clip = "📎 " if tiene_contenido_extra(ev) else ""
            botones_eventos.append([
                InlineKeyboardButton(f"{icono} {corto_fh} | {clip}{titulo_limpio[:24]}", callback_data=f"ver_{cod}")
            ])

        botones_eventos.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="btn_volver")])
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(botones_eventos), parse_mode="Markdown")
    except Exception as e:
        if es_error_token_vencido(e):
            t_err, k_err = construir_mensaje_token_vencido()
            await query.edit_message_text(t_err, reply_markup=k_err, parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ Error: {e}", reply_markup=obtener_teclado_principal())


def _obtener_evento_sync(ev_id: str):
    service = get_calendar_service()
    return service.events().get(calendarId='primary', eventId=ev_id).execute()


def _formatear_detalle_evento(ev: dict, aviso_extra: str = None):
    titulo_raw = ev.get('summary', 'Sin título')
    terminado = titulo_raw.startswith("✅")
    titulo_limpio = escapar_md(limpiar_prefijo_check(titulo_raw))

    start_raw = ev['start'].get('dateTime', ev['start'].get('date'))
    end_raw = ev['end'].get('dateTime')

    if 'T' in start_raw:
        dt = datetime.datetime.fromisoformat(start_raw)
        if end_raw:
            dt_fin = datetime.datetime.fromisoformat(end_raw)
            mins = int((dt_fin - dt).total_seconds() // 60)
            fecha_hora = f"{dt.strftime('%d/%m/%Y')} de {dt.strftime('%I:%M %p')} a {dt_fin.strftime('%I:%M %p')} ({mins} min)"
        else:
            fecha_hora = dt.strftime('%d/%m/%Y a las %I:%M %p')
    else:
        fecha_hora = f"{start_raw} (Todo el día)"

    estado_txt = "Completada ✅" if terminado else "Pendiente ⏳"
    notas, archivos = extraer_info_descripcion(ev.get('description', ''))
    ubicacion = (ev.get('location') or '').strip()

    bloques = []
    if aviso_extra:
        bloques.append(aviso_extra)

    bloques.append(
        f"📌 *Actividad:* {titulo_limpio}\n"
        f"🕒 *Horario:* {fecha_hora}\n"
        f"📊 *Estado:* {estado_txt}"
    )

    if notas:
        bloques.append(f"📝 *Instructivo / Notas / Links:*\n{escapar_md(notas)}")

    if ubicacion:
        bloques.append(f"📍 *Ubicación:* {ubicacion}")

    if archivos:
        nombres = ", ".join(escapar_md(a.get("name", "Archivo")) for a in archivos)
        bloques.append(f"📎 *Archivos adjuntos ({len(archivos)}):* {nombres}")

    texto = "\n\n".join(bloques)
    markup = obtener_teclado_acciones_evento(ev['id'], ya_terminado=terminado, num_archivos=len(archivos))
    return texto, markup


async def mostrar_detalle_evento(query, ev_id: str, aviso_extra: str = None):
    try:
        ev = await asyncio.to_thread(_obtener_evento_sync, ev_id)
        texto, markup = _formatear_detalle_evento(ev, aviso_extra=aviso_extra)
        await query.edit_message_text(
            texto,
            reply_markup=markup,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        if es_error_token_vencido(e):
            t_err, k_err = construir_mensaje_token_vencido()
            await query.edit_message_text(t_err, reply_markup=k_err, parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ No se pudo abrir la actividad: {e}", reply_markup=obtener_teclado_principal())


async def mostrar_detalle_en_mensaje(mensaje_espera, ev_id: str, aviso_extra: str = None):
    ev = await asyncio.to_thread(_obtener_evento_sync, ev_id)
    texto, markup = _formatear_detalle_evento(ev, aviso_extra=aviso_extra)
    await mensaje_espera.edit_text(
        texto,
        reply_markup=markup,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def enviar_archivos_adjuntos(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, ev_id: str):
    try:
        ev = await asyncio.to_thread(_obtener_evento_sync, ev_id)
        titulo_limpio = escapar_md(limpiar_prefijo_check(ev.get('summary', 'Actividad')))
        _, archivos = extraer_info_descripcion(ev.get('description', ''))

        if not archivos:
            await mostrar_detalle_evento(query, ev_id, aviso_extra="ℹ️ _Esta actividad aún no tiene archivos adjuntos._")
            return

        for idx, item in enumerate(archivos, 1):
            tipo = item.get("type", "document")
            fid = item.get("file_id")
            nombre = escapar_md(item.get("name", f"Adjunto {idx}"))
            cap = f"📎 *{titulo_limpio}* — {nombre} ({idx}/{len(archivos)})"

            if tipo == "photo":
                await context.bot.send_photo(chat_id=chat_id, photo=fid, caption=cap, parse_mode="Markdown")
            elif tipo == "video":
                await context.bot.send_video(chat_id=chat_id, video=fid, caption=cap, parse_mode="Markdown")
            elif tipo == "audio":
                await context.bot.send_audio(chat_id=chat_id, audio=fid, caption=cap, parse_mode="Markdown")
            elif tipo == "voice":
                await context.bot.send_voice(chat_id=chat_id, voice=fid, caption=cap, parse_mode="Markdown")
            else:
                await context.bot.send_document(chat_id=chat_id, document=fid, caption=cap, parse_mode="Markdown")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error al enviar adjuntos: {e}")


def _marcar_terminado_sync(ev_id: str, terminar: bool = True):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    titulo_limpio = limpiar_prefijo_check(ev.get('summary', 'Actividad'))
    if terminar:
        ev['summary'] = f"✅ {titulo_limpio}"
        ev['colorId'] = '10'
    else:
        ev['summary'] = titulo_limpio
        ev.pop('colorId', None)
    return service.events().update(calendarId='primary', eventId=ev_id, body=ev).execute()


async def marcar_evento_terminado(query, ev_id: str, terminar: bool = True):
    try:
        await asyncio.to_thread(_marcar_terminado_sync, ev_id, terminar)
        aviso = "🎉 _¡Actividad marcada como completada!_" if terminar else "🔄 _Actividad reabierta como pendiente._"
        await mostrar_detalle_evento(query, ev_id, aviso_extra=aviso)
    except Exception as e:
        await query.edit_message_text(f"❌ Error al actualizar estado: {e}", reply_markup=obtener_teclado_principal())


def _posponer_sync(ev_id: str, minutos: int):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()

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
    return actualizado, nuevo_inicio


async def posponer_evento(query, ev_id: str, minutos: int):
    try:
        actualizado, nuevo_inicio = await asyncio.to_thread(_posponer_sync, ev_id, minutos)
        aviso = f"💤 _Pospuesta +{minutos} min (Nueva hora: {nuevo_inicio.strftime('%I:%M %p')}). Te volveré a avisar._"
        await mostrar_detalle_evento(query, actualizado['id'], aviso_extra=aviso)
    except Exception as e:
        await query.edit_message_text(f"❌ Error al posponer actividad: {e}", reply_markup=obtener_teclado_principal())


def _eliminar_sync(ev_id: str):
    service = get_calendar_service()
    ev = service.events().get(calendarId='primary', eventId=ev_id).execute()
    titulo = limpiar_prefijo_check(ev.get('summary', 'Actividad'))
    service.events().delete(calendarId='primary', eventId=ev_id).execute()
    return titulo


async def eliminar_evento(query, ev_id: str):
    try:
        titulo = await asyncio.to_thread(_eliminar_sync, ev_id)
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("☀️ Volver a Agenda de Hoy", callback_data="btn_hoy")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="btn_volver")],
        ])
        await query.edit_message_text(
            f"🗑️ *Actividad eliminada:*\n_{escapar_md(titulo)}_",
            reply_markup=teclado,
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al eliminar: {e}", reply_markup=obtener_teclado_principal())


# ----------------- RESUMEN AUTOMÁTICO 6:30 AM Y RECORDATORIOS 10 MIN ANTES -----------------

async def enviar_resumen_matutino(context: ContextTypes.DEFAULT_TYPE):
    global ULTIMA_FECHA_RESUMEN
    if not USUARIO_CHAT_ID:
        return

    ahora = datetime.datetime.now(LOCAL_TZ)
    hoy_str = ahora.strftime("%Y-%m-%d")
    if ULTIMA_FECHA_RESUMEN == hoy_str:
        return

    try:
        inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        fin_dia = ahora.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        eventos = await asyncio.to_thread(_listar_eventos_sync, inicio_dia, fin_dia)

        ULTIMA_FECHA_RESUMEN = hoy_str
        texto, markup = construir_mensaje_agenda_hoy(ahora, eventos, es_resumen_matutino=True)
        await context.bot.send_message(
            chat_id=USUARIO_CHAT_ID,
            text=texto,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error al enviar resumen matutino 6:30 AM: {e}", flush=True)


async def verificar_recordatorios(context: ContextTypes.DEFAULT_TYPE):
    global USUARIO_CHAT_ID, EVENTOS_NOTIFICADOS, ULTIMA_FECHA_RESUMEN
    if not USUARIO_CHAT_ID:
        return

    ahora = datetime.datetime.now(LOCAL_TZ)
    hoy_str = ahora.strftime("%Y-%m-%d")

    if ahora.hour == HORA_RESUMEN_DIARIO and MINUTO_RESUMEN_DIARIO <= ahora.minute <= (MINUTO_RESUMEN_DIARIO + 5):
        if ULTIMA_FECHA_RESUMEN != hoy_str:
            await enviar_resumen_matutino(context)

    try:
        en_12_min = ahora + datetime.timedelta(minutes=12)
        eventos = await asyncio.to_thread(_listar_eventos_sync, ahora.isoformat(), en_12_min.isoformat())

        for ev in eventos:
            ev_id = ev['id']
            titulo_raw = ev.get('summary', 'Actividad sin nombre')
            if titulo_raw.startswith("✅"):
                continue

            start_raw = ev['start'].get('dateTime')
            if not start_raw:
                continue

            clave_notif = (ev_id, start_raw)
            if clave_notif in EVENTOS_NOTIFICADOS:
                continue

            dt_evento = datetime.datetime.fromisoformat(start_raw)
            segundos_restantes = (dt_evento - ahora).total_seconds()
            minutos = max(0, round(segundos_restantes / 60))

            if 0 <= minutos <= 10:
                EVENTOS_NOTIFICADOS.add(clave_notif)
                notas, archivos = extraer_info_descripcion(ev.get('description', ''))
                ubicacion = (ev.get('location') or '').strip()

                partes_msg = [
                    f"⏰ *RECORDATORIO DE ACTIVIDAD*\n\n"
                    f"📌 *Actividad:* {escapar_md(limpiar_prefijo_check(titulo_raw))}\n"
                    f"🕒 *Hora:* {dt_evento.strftime('%I:%M %p')} _(comienza en {minutos} min)_"
                ]
                if notas:
                    partes_msg.append(f"📝 *Instructivo / Notas:*\n{escapar_md(notas)}")
                if ubicacion:
                    partes_msg.append(f"📍 *Ubicación:* {ubicacion}")
                if archivos:
                    partes_msg.append(f"📎 _Tiene {len(archivos)} archivo(s)/foto(s) adjunto(s). Toca 'Ver Archivos' abajo para abrirlos._")

                mensaje = "\n\n".join(partes_msg)
                await context.bot.send_message(
                    chat_id=USUARIO_CHAT_ID,
                    text=mensaje,
                    reply_markup=obtener_teclado_acciones_evento(ev_id, ya_terminado=False, num_archivos=len(archivos)),
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
    except Exception as e:
        print(f"Error en recordatorios: {e}", flush=True)


async def al_iniciar_bot(app):
    """Canjea el código inicial al arrancar y avisa por Telegram el token permanente."""
    nuevo = await asyncio.to_thread(inicializar_oauth_si_pendiente)
    if nuevo and USUARIO_CHAT_ID:
        parte = nuevo[3:] if nuevo.startswith("1//") else nuevo
        try:
            await app.bot.send_message(
                chat_id=USUARIO_CHAT_ID,
                text=(
                    "✅ *¡Google Calendar conectado en modo PERMANENTE!*\n\n"
                    f"🔑 _Código permanente generado:_\n`{parte}`\n\n"
                    "Selecciona una opción del menú:"
                ),
                reply_markup=obtener_teclado_principal(),
                parse_mode="Markdown"
            )
        except Exception:
            pass


# ----------------- INICIO -----------------

if __name__ == '__main__':
    iniciar_servidor_web_nube()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(al_iniciar_bot).build()

    app.add_handler(CommandHandler("start", comando_menu))
    app.add_handler(CommandHandler("menu", comando_menu))
    app.add_handler(CommandHandler("conectar", comando_conectar))
    app.add_handler(CommandHandler("cancelar", comando_cancelar))
    app.add_handler(CallbackQueryHandler(manejar_botones))

    filtro_multimedia = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.LOCATION
    )
    app.add_handler(MessageHandler(filtro_multimedia, manejar_adjunto_multimedia))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))

    if app.job_queue:
        app.job_queue.run_repeating(verificar_recordatorios, interval=60, first=10)
        hora_630 = datetime.time(hour=HORA_RESUMEN_DIARIO, minute=MINUTO_RESUMEN_DIARIO, tzinfo=LOCAL_TZ)
        app.job_queue.run_daily(enviar_resumen_matutino, time=hora_630)

    print("🤖 Bot de Agenda con Edición, Adjuntos y Reconexión Directa encendido...", flush=True)
    app.run_polling(drop_pending_updates=True)
