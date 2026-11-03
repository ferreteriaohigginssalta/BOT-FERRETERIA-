"""
Bot de WhatsApp de la ferretería + panel web para vendedores  (versión 3)

VARIABLES DE ENTORNO (en Render: Environment)
  WHATSAPP_TOKEN   token permanente de Meta                          (obligatoria)
  PHONE_NUMBER_ID  Phone Number ID del número del bot                (obligatoria)
  VERIFY_TOKEN     la clave del webhook (la misma que pusiste en Meta) (obligatoria)
  PANEL_PASSWORD   clave para entrar al panel de vendedores          (obligatoria)
  APP_SECRET       "Clave secreta de la app" de Meta                 (muy recomendada)
                   Meta > tu app > Configuración de la app > Básica
  SECRET_KEY       texto largo al azar para las sesiones             (opcional)
  DATA_DIR         carpeta donde se guardan mensajes y archivos      (opcional)
                   Con disco persistente de Render: /data

ARCHIVOS
  catalogo/        fotos de productos (arcilla.jpg, etc.). Se suben a GitHub junto con este archivo.
"""

import functools
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, abort, jsonify, redirect, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ferreteria")


# =====================================================================
# 1) DATOS DE LA FERRETERÍA  ← esta es la parte que vas a editar
# =====================================================================
NOMBRE_NEGOCIO = "Ferretería"  # poné acá el nombre real del local

# Días: 0 = lunes ... 6 = domingo. Horas en formato 24 hs.
TURNOS = [
    ("Lunes a Viernes", (0, 1, 2, 3, 4), "08:30", "19:30"),
    ("Sábados", (5,), "09:00", "13:00"),
]
DIAS_CERRADO = "Domingos y feriados"
FERIADOS = []  # fechas en que no abrís, formato "AAAA-MM-DD". Ej: ["2026-12-08", "2026-12-25"]

# Si los dejás vacíos, el bot deriva esas consultas a un vendedor.
DIRECCION = ""       # Ej: "📍 Estamos en Av. Siempre Viva 123, Salta."
MEDIOS_DE_PAGO = ""  # Ej: "💳 Aceptamos efectivo, transferencia y tarjetas."

# Productos con info y foto. La foto va en la carpeta "catalogo/" (jpg o png).
# Sin precios a propósito: los precios los pasa siempre una persona.
PRODUCTOS = {
    "arcilla": {
        "nombre": "Arcilla",
        "palabras": ("arcilla", "arcillas"),
        "foto": "arcilla.jpg",
        "texto": (
            "🧱 *Arcilla refractaria / común* 🛠️\n\n"
            "Tenemos excelente calidad para tus obras y construcciones. "
            "Viene en presentación lista para usar.\n\n"
            "Para consultar cantidades o precio, escribí *VENDEDOR*."
        ),
    },
}

# Frases que los vendedores pueden insertar con un toque en el panel.
RESPUESTAS_RAPIDAS = [
    "Hola, ¿en qué te puedo ayudar?",
    "Un momento, lo consulto y te aviso.",
    "¿Querés que te pase una foto del producto?",
    "Gracias por escribirnos, ¡que tengas un buen día!",
]

# Si el cliente manda una foto, audio, documento o ubicación, ¿se deriva a un vendedor?
DERIVAR_ARCHIVOS = True

SALUDO = (
    f"¡Hola! 👋 Bienvenido a *{NOMBRE_NEGOCIO}*. Es un gusto saludarte.\n"
    "Soy el asistente virtual (un bot) y estoy acá para ayudarte rápido."
)
MENU_ESCRITO = (
    "Escribí lo que necesites:\n"
    "• *HORARIOS* para ver cuándo abrimos\n"
    "• *PRODUCTOS* para ver info y fotos\n"
    "• *VENDEDOR* para hablar con una persona del equipo"
)
DERIVACION = "🔔 Te acabo de derivar con un vendedor. En un momento te responderá por acá."
DERIVACION_PRECIOS = (
    "Los precios y presupuestos te los pasa una persona del equipo, así son siempre los valores "
    "actualizados.\n🔔 Ya te derivé con un vendedor."
)
DERIVACION_ARCHIVO = "Recibí lo que me mandaste 📎. Se lo paso a un vendedor para que lo revise. 🔔"
DERIVACION_SIN_DATO = "Eso te lo confirma un vendedor. 🔔 Ya lo avisé y te responde por acá."
DERIVACION_INSISTENCIA = "Creo que te va a ayudar mejor una persona. 🔔 Te derivé con un vendedor."
AVISO_FUERA_DE_HORARIO = "🌙 Ahora estamos fuera de horario: te responden apenas abramos."
MAX_FALLOS = 2  # después de tantos "no te entendí" seguidos se deriva a un vendedor


# =====================================================================
# 2) CONFIGURACIÓN TÉCNICA (no hace falta tocar)
# =====================================================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
GRAPH = "https://graph.facebook.com/" + os.environ.get("GRAPH_VERSION", "v21.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOGO_DIR = os.path.join(BASE_DIR, "catalogo")


def _preparar_datos():
    pedida = os.environ.get("DATA_DIR", "").strip()
    for carpeta in (pedida, os.path.join(BASE_DIR, "datos")):
        if not carpeta:
            continue
        try:
            os.makedirs(os.path.join(carpeta, "media"), exist_ok=True)
            return carpeta
        except OSError as e:
            log.warning("No pude usar la carpeta %s: %s", carpeta, e)
    raise RuntimeError("No hay ninguna carpeta donde guardar los datos")


DATA_DIR = _preparar_datos()
MEDIA_DIR = os.path.join(DATA_DIR, "media")
DB_PATH = os.path.join(DATA_DIR, "ferreteria.db")

TZ_AR = timezone(timedelta(hours=-3))  # Argentina no usa horario de verano
QUITAR_9_ARGENTINA = True  # Meta manda 549..., pero para responder va sin el 9
VENTANA_SEGUNDOS = 24 * 3600  # WhatsApp solo deja escribir texto libre 24 hs después del último mensaje del cliente
MAX_BAJADA = 20 * 1024 * 1024
MAX_FOTO = 5 * 1024 * 1024
MAX_PDF = 16 * 1024 * 1024

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("ferreteria|" + (PANEL_PASSWORD or secrets.token_hex(16))).encode()
).hexdigest()
app.permanent_session_lifetime = timedelta(hours=12)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)

if not APP_SECRET:
    log.warning(
        "⚠️ Falta APP_SECRET: cualquiera que conozca la dirección del webhook podría "
        "mandar mensajes falsos. Cargala en Render (Environment)."
    )
if not PANEL_PASSWORD:
    log.warning("⚠️ Falta PANEL_PASSWORD: el panel queda desactivado.")


# =====================================================================
# 3) BASE DE DATOS (SQLite)
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversaciones (
    numero         TEXT PRIMARY KEY,
    nombre         TEXT NOT NULL DEFAULT '',
    perfil         TEXT NOT NULL DEFAULT '',
    humano         INTEGER NOT NULL DEFAULT 0,
    pide_vendedor  INTEGER NOT NULL DEFAULT 0,
    pide_desde     REAL NOT NULL DEFAULT 0,
    no_leidos      INTEGER NOT NULL DEFAULT 0,
    fallos         INTEGER NOT NULL DEFAULT 0,
    ultimo         REAL NOT NULL DEFAULT 0,
    ultimo_cliente REAL NOT NULL DEFAULT 0,
    ult_texto      TEXT NOT NULL DEFAULT '',
    creada         REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mensajes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    numero         TEXT NOT NULL,
    quien          TEXT NOT NULL,
    tipo           TEXT NOT NULL DEFAULT 'text',
    texto          TEXT NOT NULL DEFAULT '',
    archivo        TEXT NOT NULL DEFAULT '',
    nombre_archivo TEXT NOT NULL DEFAULT '',
    ts             REAL NOT NULL,
    wamid          TEXT NOT NULL DEFAULT '',
    estado         TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mensajes_numero ON mensajes (numero, id);
CREATE INDEX IF NOT EXISTS idx_mensajes_wamid ON mensajes (wamid);
CREATE TABLE IF NOT EXISTS vistos (wamid TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS eventos (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    tipo    TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos (ts);
"""


@contextmanager
def tx():
    """Abre la base, confirma los cambios al salir y la cierra siempre."""
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=20000")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def iniciar_db():
    with tx() as con:
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        con.executescript(SCHEMA)
        con.execute("DELETE FROM vistos WHERE ts < ?", (time.time() - 3 * 86400,))
    log.info("Base de datos lista en %s", DB_PATH)


iniciar_db()


# =====================================================================
# 4) UTILIDADES
# =====================================================================
def ahora():
    return datetime.now(TZ_AR)


def fmt(ts):
    t = datetime.fromtimestamp(ts, TZ_AR)
    return t.strftime("%H:%M") if t.date() == ahora().date() else t.strftime("%d/%m %H:%M")


def numero_valido(numero):
    return bool(re.fullmatch(r"[0-9]{8,20}", numero or ""))


def normalizar(texto):
    """minúsculas, sin tildes ni signos: '¿Horários?' -> 'horarios'."""
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tiene(t, palabras):
    """True si alguna palabra o frase aparece COMPLETA en el texto ya normalizado."""
    envuelto = f" {t} "
    return any(f" {normalizar(p)} " in envuelto for p in palabras)


def abierto_ahora():
    t = ahora()
    if t.strftime("%Y-%m-%d") in FERIADOS:
        return False
    hm = t.strftime("%H:%M")
    return any(t.weekday() in dias and desde <= hm < hasta for _, dias, desde, hasta in TURNOS)


def texto_horarios(con_pregunta=False):
    lineas = ["🕒 *¡Te esperamos en nuestro local!* 🛠️", ""]
    for nombre, _dias, desde, hasta in TURNOS:
        lineas.append(f"📅 *{nombre}:* {desde} a {hasta} hs")
    if DIAS_CERRADO:
        lineas.append(f"❌ *{DIAS_CERRADO}:* Cerrado")
    lineas.append("")
    lineas.append("✅ ¡Ahora estamos abiertos!" if abierto_ahora() else "🌙 Ahora estamos cerrados.")
    if con_pregunta:
        lineas += ["", "¿Necesitás consultar por alguna herramienta o material en particular?"]
    return "\n".join(lineas)


def texto_productos():
    if not PRODUCTOS:
        return "Para consultar por productos escribí *VENDEDOR*."
    lista = "\n".join(f"• {p['nombre']}" for p in PRODUCTOS.values())
    return (
        "🧰 Estos son los productos con info y foto:\n"
        f"{lista}\n\n"
        "Escribí el nombre del producto para verlo. Para cualquier otra consulta escribí *VENDEDOR*."
    )


_locks = {}
_locks_guardia = threading.Lock()


def lock_de(numero):
    """Un candado por cliente: sus mensajes se procesan de a uno, en orden."""
    with _locks_guardia:
        return _locks.setdefault(numero, threading.Lock())


# =====================================================================
# 5) GUARDAR Y LEER MENSAJES
# =====================================================================
def vista_previa(quien, tipo, texto):
    iconos = {
        "image": "📷 Foto", "document": "📄 Documento", "audio": "🎙️ Audio",
        "video": "🎬 Video", "sticker": "Sticker", "location": "📍 Ubicación",
    }
    base = iconos.get(tipo, "")
    if base and texto:
        base += ": " + texto
    cuerpo = (base or texto).replace("\n", " ")[:80]
    if quien == "bot":
        return "🤖 " + cuerpo
    if quien == "vendedor":
        return "Vos: " + cuerpo
    return cuerpo


def registrar(numero, quien, tipo="text", texto="", archivo="", nombre_archivo="",
              wamid="", estado="", error=""):
    t = time.time()
    with tx() as con:
        con.execute(
            "INSERT OR IGNORE INTO conversaciones (numero, creada, ultimo) VALUES (?,?,?)",
            (numero, t, t),
        )
        cur = con.execute(
            "INSERT INTO mensajes (numero, quien, tipo, texto, archivo, nombre_archivo, ts, wamid, estado, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (numero, quien, tipo, texto, archivo, nombre_archivo, t, wamid, estado, error),
        )
        prev = vista_previa(quien, tipo, texto)
        if quien == "cliente":
            con.execute(
                "UPDATE conversaciones SET ultimo=?, ultimo_cliente=?, no_leidos=no_leidos+1, ult_texto=? "
                "WHERE numero=?",
                (t, t, prev, numero),
            )
        else:
            con.execute(
                "UPDATE conversaciones SET ultimo=?, ult_texto=? WHERE numero=?", (t, prev, numero)
            )
        return cur.lastrowid


def conv(numero):
    with tx() as con:
        return con.execute("SELECT * FROM conversaciones WHERE numero=?", (numero,)).fetchone()


def marcar(numero, **campos):
    claves = ", ".join(f"{k}=?" for k in campos)  # los nombres salen de este archivo, nunca del usuario
    with tx() as con:
        con.execute(f"UPDATE conversaciones SET {claves} WHERE numero=?", (*campos.values(), numero))


def evento(tipo, detalle=""):
    """Anota qué consultan los clientes (sin número de teléfono) para el reporte semanal."""
    try:
        with tx() as con:
            con.execute(
                "INSERT INTO eventos (ts, tipo, detalle) VALUES (?,?,?)",
                (time.time(), tipo, (detalle or "")[:200]),
            )
    except Exception:
        log.exception("No se pudo anotar el evento")


def marcar_visto(wamid):
    with tx() as con:
        cur = con.execute("INSERT OR IGNORE INTO vistos (wamid, ts) VALUES (?,?)", (wamid, time.time()))
        return cur.rowcount == 1


def es_primer_mensaje(numero):
    with tx() as con:
        n = con.execute(
            "SELECT COUNT(*) FROM mensajes WHERE numero=? AND quien!='cliente'", (numero,)
        ).fetchone()[0]
    return n == 0


def sumar_fallo(numero):
    with tx() as con:
        con.execute("UPDATE conversaciones SET fallos=fallos+1 WHERE numero=?", (numero,))
        return con.execute("SELECT fallos FROM conversaciones WHERE numero=?", (numero,)).fetchone()[0]


ORDEN_ESTADO = {"sent": 1, "delivered": 2, "read": 3, "failed": 9}


def actualizar_estado(st):
    """Meta avisa si un mensaje se entregó, se leyó o falló."""
    wamid = st.get("id")
    nuevo = st.get("status")
    if not wamid or nuevo not in ORDEN_ESTADO:
        return
    error = ""
    if nuevo == "failed":
        errs = st.get("errors") or [{}]
        e = errs[0]
        error = f"{e.get('title') or e.get('message') or 'No se pudo entregar'} ({e.get('code', '?')})"
        log.warning("WhatsApp no pudo entregar %s: %s", wamid, error)
    with tx() as con:
        fila = con.execute("SELECT id, estado FROM mensajes WHERE wamid=?", (wamid,)).fetchone()
        if fila is None:
            return
        if nuevo != "failed" and ORDEN_ESTADO.get(fila["estado"], 0) >= ORDEN_ESTADO[nuevo]:
            return
        con.execute("UPDATE mensajes SET estado=?, error=? WHERE id=?", (nuevo, error, fila["id"]))


# =====================================================================
# 6) ENVÍO A WHATSAPP (API de Meta)
# =====================================================================
def cabecera():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}


def a_destino(numero):
    if QUITAR_9_ARGENTINA and numero.startswith("549"):
        return "54" + numero[3:]
    return numero


def detalle_error(r):
    if r.status_code == 401:
        return "El token de WhatsApp venció o no es válido: hay que generar uno nuevo en Meta."
    try:
        return r.json()["error"]["message"]
    except Exception:
        return f"Meta respondió {r.status_code}"


def meta_post(payload):
    """Devuelve (ok, id_del_mensaje, error)."""
    try:
        r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/messages", headers=cabecera(), json=payload, timeout=15)
    except requests.RequestException as e:
        log.error("No se pudo conectar con WhatsApp: %r", e)
        return False, "", "No se pudo conectar con WhatsApp"
    log.info("ENVIO -> %s %s", r.status_code, r.text[:300])
    if r.ok:
        try:
            return True, r.json()["messages"][0]["id"], ""
        except Exception:
            return True, "", ""
    return False, "", detalle_error(r)


def enviar_texto(numero, texto):
    return meta_post({
        "messaging_product": "whatsapp", "to": a_destino(numero),
        "type": "text", "text": {"body": texto[:4000]},
    })


def enviar_botones(numero, cuerpo):
    botones = [
        ("horarios", "🕒 Horarios"),
        ("productos", "🧱 Productos"),
        ("vendedor", "👤 Vendedor"),
    ]
    return meta_post({
        "messaging_product": "whatsapp", "to": a_destino(numero), "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": i, "title": t}} for i, t in botones
            ]},
        },
    })


def subir_media(ruta, mime):
    """Sube un archivo a Meta. Devuelve (id_del_archivo, error)."""
    try:
        with open(ruta, "rb") as f:
            r = requests.post(
                f"{GRAPH}/{PHONE_NUMBER_ID}/media", headers=cabecera(),
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (os.path.basename(ruta), f, mime)}, timeout=40,
            )
        log.info("SUBIDA -> %s %s", r.status_code, r.text[:200])
        if r.ok:
            return r.json().get("id", ""), ""
        return "", detalle_error(r)
    except requests.RequestException as e:
        log.error("No se pudo subir el archivo: %r", e)
        return "", "No se pudo conectar con WhatsApp"
    except Exception as e:
        log.exception("Error subiendo archivo")
        return "", str(e)


def mime_por_extension(ruta):
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(
        os.path.splitext(ruta)[1].lower(), "")


MEDIA_IDS = {}  # fotos del catálogo ya subidas a Meta: nombre -> (id, hora). Meta las guarda ~30 días.


def enviar_foto_catalogo(numero, foto, texto):
    ruta = os.path.join(CATALOGO_DIR, foto)
    mime = mime_por_extension(ruta)
    if not mime or not os.path.isfile(ruta):
        return False, "", "no hay foto"
    error = ""
    for intento in (1, 2):
        guardado = MEDIA_IDS.get(foto)
        if guardado and intento == 1 and time.time() - guardado[1] < 20 * 86400:
            media_id = guardado[0]
        else:
            media_id, error = subir_media(ruta, mime)
            if not media_id:
                return False, "", error
            MEDIA_IDS[foto] = (media_id, time.time())
        ok, wamid, error = meta_post({
            "messaging_product": "whatsapp", "to": a_destino(numero), "type": "image",
            "image": {"id": media_id, "caption": texto[:1024]},
        })
        if ok:
            return True, wamid, ""
        MEDIA_IDS.pop(foto, None)  # por si Meta ya la borró: la volvemos a subir
    return False, "", error


# ---- mensajes del bot (se envían Y se anotan en el panel) ------------
def decir(numero, texto, quien="bot"):
    ok, wamid, error = enviar_texto(numero, texto)
    registrar(numero, quien, "text", texto, wamid=wamid, estado="sent" if ok else "failed", error=error)
    return ok


def decir_menu(numero, intro):
    """Menú con 3 botones. Si Meta no lo acepta, manda el menú escrito."""
    ok, wamid, error = enviar_botones(numero, intro + "\n\nElegí una opción 👇")
    if ok:
        registrar(numero, "bot", "text", intro + "\n🔘 Menú: Horarios · Productos · Vendedor",
                  wamid=wamid, estado="sent")
        return
    log.warning("Los botones no funcionaron (%s): mando el menú escrito", error)
    decir(numero, intro + "\n\n" + MENU_ESCRITO)


def enviar_producto(numero, clave):
    p = PRODUCTOS[clave]
    foto = p.get("foto")
    if foto:
        ok, wamid, error = enviar_foto_catalogo(numero, foto, p["texto"])
        if ok:
            registrar(numero, "bot", "image", p["texto"], archivo="c/" + foto, wamid=wamid, estado="sent")
            return
        log.warning("No pude mandar la foto %s (%s): mando solo el texto", foto, error)
    decir(numero, p["texto"])


# =====================================================================
# 7) LÓGICA DEL BOT
# =====================================================================
PALABRAS_VENDEDOR = (
    "vendedor", "vendedora", "asesor", "asesora", "humano", "operador", "operadora", "persona",
    "hablar con alguien", "atencion personalizada",
)
PALABRAS_PRECIO = (
    "precio", "precios", "cuanto sale", "cuanto salen", "cuanto cuesta", "cuanto cuestan",
    "cuanto vale", "cuanto esta", "cuanto me sale", "presupuesto", "presupuestos", "cotizacion",
    "cotizar", "costo",
)
PALABRAS_HORARIOS = ("horario", "horarios", "hora", "abren", "abre", "abierto", "abiertos",
                     "cierran", "cierra", "atienden")
PALABRAS_DIRECCION = ("direccion", "ubicacion", "donde estan", "donde queda", "donde quedan",
                      "donde esta", "como llego")
PALABRAS_PAGO = ("medios de pago", "formas de pago", "pagar", "pago", "tarjeta", "tarjetas",
                 "efectivo", "transferencia", "cuotas", "mercado pago", "debito", "credito")
PALABRAS_PRODUCTOS = ("productos", "catalogo", "que venden", "que vende", "que tienen")
PALABRAS_MENU = ("menu", "opciones", "ayuda", "inicio", "info", "informacion")
PALABRAS_SALUDO = ("hola", "buenas", "buen dia", "buenos dias", "buenas tardes", "buenas noches", "hey")
PALABRAS_GRACIAS = ("gracias", "muchas gracias", "mil gracias")
BOTONES = {"horarios": "horarios", "productos": "productos", "vendedor": "vendedor"}


def productos_en(t):
    return [clave for clave, p in PRODUCTOS.items() if tiene(t, p["palabras"])]


def derivar(numero, motivo, mensaje):
    """Pasa la conversación a una persona: el bot se calla y el panel marca el 🔔."""
    evento("vendedor", motivo)
    marcar(numero, humano=1, pide_vendedor=1, pide_desde=time.time(), fallos=0)
    if not abierto_ahora():
        mensaje += "\n\n" + AVISO_FUERA_DE_HORARIO
    decir(numero, mensaje)


def responder(numero, texto, primer_mensaje):
    t = normalizar(texto)
    claves = productos_en(t)
    for clave in claves:
        evento("producto", clave)

    if tiene(t, PALABRAS_VENDEDOR):
        return derivar(numero, "pedido", DERIVACION)
    if tiene(t, PALABRAS_PRECIO):
        return derivar(numero, "precio", DERIVACION_PRECIOS)

    if tiene(t, PALABRAS_HORARIOS):
        evento("horarios")
        decir(numero, texto_horarios(con_pregunta=True))
    elif tiene(t, PALABRAS_DIRECCION):
        if not DIRECCION:
            return derivar(numero, "direccion", DERIVACION_SIN_DATO)
        decir(numero, DIRECCION)
    elif tiene(t, PALABRAS_PAGO):
        if not MEDIOS_DE_PAGO:
            return derivar(numero, "pago", DERIVACION_SIN_DATO)
        decir(numero, MEDIOS_DE_PAGO)
    elif claves:
        for clave in claves[:3]:
            enviar_producto(numero, clave)
    elif tiene(t, PALABRAS_PRODUCTOS):
        decir(numero, texto_productos())
    elif tiene(t, PALABRAS_MENU) or tiene(t, PALABRAS_SALUDO):
        evento("saludo")
        decir_menu(numero, SALUDO)
    elif tiene(t, PALABRAS_GRACIAS):
        decir(numero, "¡De nada! 🙌 Cualquier cosa, escribinos.")
    elif primer_mensaje:
        evento("no_entendido", texto)  # lo anotamos, pero primero lo saludamos
        decir_menu(numero, SALUDO)
        return
    else:
        evento("no_entendido", texto)
        if sumar_fallo(numero) >= MAX_FALLOS:
            return derivar(numero, "insistencia", DERIVACION_INSISTENCIA)
        decir_menu(numero, "No te entendí del todo 🙈")
        return
    marcar(numero, fallos=0)


EXT_POR_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "audio/amr": ".amr", "video/mp4": ".mp4", "video/3gpp": ".3gp", "application/pdf": ".pdf",
}
MIME_POR_EXT = {v: k for k, v in EXT_POR_MIME.items()}
MEDIA_TIPOS = ("image", "document", "audio", "video", "sticker")


def bajar_media(media_id, nombre_original=""):
    """Baja el archivo del cliente y lo guarda en una carpeta privada. Devuelve 'm/archivo' o ''."""
    if not media_id:
        return ""
    try:
        r = requests.get(f"{GRAPH}/{media_id}", headers=cabecera(), timeout=15)
        r.raise_for_status()
        info = r.json()
        url = info.get("url")
        if not url:
            return ""
        mime = (info.get("mime_type") or "").split(";")[0].strip().lower()
        d = requests.get(url, headers=cabecera(), timeout=40)
        d.raise_for_status()
        if len(d.content) > MAX_BAJADA:
            raise ValueError("archivo demasiado grande")
        ext = EXT_POR_MIME.get(mime)
        if not ext:
            candidata = os.path.splitext(nombre_original or "")[1].lower()
            ext = candidata if re.fullmatch(r"\.[a-z0-9]{1,5}", candidata) else ".bin"
        nombre = uuid.uuid4().hex + ext
        with open(os.path.join(MEDIA_DIR, nombre), "wb") as f:
            f.write(d.content)
        return "m/" + nombre
    except Exception as e:
        log.error("No se pudo bajar el archivo %s: %r", media_id, e)
        return ""


def procesar_mensaje(msg, perfil=""):
    numero = str(msg.get("from", ""))
    wamid = msg.get("id", "")
    if not numero_valido(numero):
        return
    if wamid and not marcar_visto(wamid):
        return  # Meta reenvió un mensaje que ya procesamos
    tipo = msg.get("type", "")
    if tipo == "reaction":
        return  # un 👍 del cliente no merece respuesta

    with lock_de(numero):
        texto, entrada = "", ""
        archivo, nombre_archivo, es_archivo, tipo_guardado = "", "", False, "text"

        if tipo == "text":
            texto = entrada = (msg.get("text") or {}).get("body", "")
        elif tipo == "interactive":
            inter = msg.get("interactive") or {}
            resp = inter.get("button_reply") or inter.get("list_reply") or {}
            texto = resp.get("title", "")
            entrada = BOTONES.get(resp.get("id"), texto)
        elif tipo in MEDIA_TIPOS:
            dato = msg.get(tipo) or {}
            texto = dato.get("caption", "")
            nombre_archivo = secure_filename(dato.get("filename", "") or "")[:80]
            archivo = bajar_media(dato.get("id", ""), nombre_archivo)
            if not archivo:
                texto = (texto + "\n" if texto else "") + "⚠️ No se pudo descargar el archivo."
            es_archivo, tipo_guardado = True, tipo
        elif tipo == "location":
            loc = msg.get("location") or {}
            partes = [loc.get("name") or "", loc.get("address") or "",
                      f"({loc.get('latitude')}, {loc.get('longitude')})"]
            texto = " ".join(p for p in partes if p).strip()
            es_archivo, tipo_guardado = True, "location"
        else:
            registrar(numero, "cliente", "text", "[Mensaje no compatible]")
            return

        registrar(numero, "cliente", tipo_guardado, texto, archivo=archivo, nombre_archivo=nombre_archivo)
        if perfil:
            marcar(numero, perfil=perfil[:60])

        if conv(numero)["humano"]:
            return  # atiende una persona: el bot se queda callado

        if es_archivo:
            evento("archivo", tipo)
            if DERIVAR_ARCHIVOS:
                derivar(numero, "archivo", DERIVACION_ARCHIVO)
            else:
                decir_menu(numero, "Recibí tu archivo 🙂")
            return
        responder(numero, entrada, es_primer_mensaje(numero))


def procesar_valor(valor):
    perfiles = {
        c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in valor.get("contacts") or []
    }
    for st in valor.get("statuses") or []:
        try:
            actualizar_estado(st)
        except Exception:
            log.exception("Error actualizando estado")
    for msg in valor.get("messages") or []:
        try:
            procesar_mensaje(msg, perfiles.get(msg.get("from"), ""))
        except Exception:
            log.exception("Error procesando un mensaje")


# =====================================================================
# 8) WEBHOOK DE META
# =====================================================================
def firma_valida():
    """Comprueba que el aviso lo mandó realmente Meta (firma con la clave secreta de la app)."""
    if not APP_SECRET:
        return True
    enviada = request.headers.get("X-Hub-Signature-256", "")
    esperada = "sha256=" + hmac.new(APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(enviada, esperada)


@app.route("/", methods=["GET"])
def inicio():
    return "Bot activo", 200


@app.route("/webhook", methods=["GET"])
def verificar():
    if (
        request.args.get("hub.mode") == "subscribe"
        and VERIFY_TOKEN
        and hmac.compare_digest(request.args.get("hub.verify_token", ""), VERIFY_TOKEN)
    ):
        return request.args.get("hub.challenge", ""), 200
    return "Token inválido", 403


@app.route("/webhook", methods=["POST"])
def recibir():
    if not firma_valida():
        log.warning("Webhook rechazado: la firma no coincide")
        return "firma inválida", 403
    body = request.get_json(silent=True) or {}
    try:
        for entrada in body.get("entry") or []:
            for cambio in entrada.get("changes") or []:
                valor = cambio.get("value") or {}
                if app.config.get("PROCESAR_SINCRONO"):
                    procesar_valor(valor)
                else:
                    threading.Thread(target=procesar_valor, args=(valor,), daemon=True).start()
    except Exception:
        log.exception("Error leyendo el webhook")
    return "ok", 200  # contestamos rápido: Meta reintenta si tardamos


# =====================================================================
# 9) ACCESO AL PANEL
# =====================================================================
INTENTOS = {}  # ip -> (fallos, desde)


def bloqueado(ip):
    fallos, desde = INTENTOS.get(ip, (0, 0))
    if time.time() - desde >= 300:
        INTENTOS.pop(ip, None)
        return False
    return fallos >= 5


def pagina_login(error=""):
    aviso = f'<p style="color:#b00020">{html.escape(error)}</p>' if error else ""
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Panel Ferretería</title></head>"
        '<body style="font-family:system-ui,sans-serif;background:#efeae2;'
        'display:flex;justify-content:center;padding:40px 16px">'
        '<form method="post" action="/login" style="background:#fff;padding:24px;'
        'border-radius:10px;max-width:340px;width:100%;box-shadow:0 1px 4px #0003">'
        '<h2 style="margin-top:0">📋 Panel de la ferretería</h2>'
        '<input type="password" name="clave" placeholder="Clave del panel" '
        'autofocus required style="width:100%;padding:10px;font-size:16px;box-sizing:border-box">'
        '<button style="margin-top:12px;width:100%;padding:10px;border:0;'
        'border-radius:8px;background:#075e54;color:#fff;font-size:16px">Entrar</button>'
        f"{aviso}</form></body></html>"
    )


@app.route("/login", methods=["POST"])
def login():
    if not PANEL_PASSWORD:
        return pagina_login("Panel desactivado: falta cargar PANEL_PASSWORD en el servidor."), 503
    ip = request.remote_addr or "?"
    if bloqueado(ip):
        return pagina_login("Demasiados intentos. Probá de nuevo en unos minutos."), 429
    clave = request.form.get("clave", "")
    if hmac.compare_digest(clave.encode(), PANEL_PASSWORD.encode()):
        INTENTOS.pop(ip, None)
        session.clear()
        session.permanent = True
        session["ok"] = True
        return redirect("/panel")
    fallos, desde = INTENTOS.get(ip, (0, time.time()))
    INTENTOS[ip] = (fallos + 1, desde)
    return pagina_login("Clave incorrecta."), 401


@app.route("/salir")
def salir():
    session.clear()
    return redirect("/panel")


def requiere_login(f):
    @functools.wraps(f)
    def envoltura(*args, **kwargs):
        if not session.get("ok"):
            return jsonify({"error": "no autorizado"}), 401
        return f(*args, **kwargs)
    return envoltura


@app.after_request
def cabeceras_seguras(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/") or request.path == "/panel":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(413)
def demasiado_grande(_e):
    return jsonify({"ok": False, "error": "El archivo es demasiado grande (máximo 20 MB)."}), 413


# =====================================================================
# 10) API DEL PANEL (todas exigen haber iniciado sesión)
# =====================================================================
def titulo(c):
    return c["nombre"] or c["perfil"] or "+" + c["numero"]


def ventana_abierta(c):
    return time.time() - c["ultimo_cliente"] < VENTANA_SEGUNDOS


MSG_VENTANA = (
    "Pasaron más de 24 hs desde el último mensaje del cliente. WhatsApp no deja escribirle "
    "primero con texto libre (haría falta una plantilla aprobada). Esperá a que él escriba."
)


@app.route("/api/config")
@requiere_login
def api_config():
    return jsonify({"rapidas": RESPUESTAS_RAPIDAS, "negocio": NOMBRE_NEGOCIO})


@app.route("/api/conversaciones")
@requiere_login
def api_conversaciones():
    with tx() as con:
        filas = con.execute(
            "SELECT * FROM conversaciones ORDER BY pide_vendedor DESC, ultimo DESC LIMIT 300"
        ).fetchall()
    ahora_ts = time.time()
    return jsonify([{
        "numero": c["numero"],
        "titulo": titulo(c),
        "texto": c["ult_texto"],
        "hora": fmt(c["ultimo"]),
        "no_leidos": c["no_leidos"],
        "humano": bool(c["humano"]),
        "pide_vendedor": bool(c["pide_vendedor"]),
        "espera_min": int((ahora_ts - c["pide_desde"]) // 60) if c["pide_vendedor"] else 0,
    } for c in filas])


@app.route("/api/conversaciones/<numero>")
@requiere_login
def api_conversacion(numero):
    if not numero_valido(numero):
        return jsonify({"error": "número inválido"}), 400
    with tx() as con:
        c = con.execute("SELECT * FROM conversaciones WHERE numero=?", (numero,)).fetchone()
        if c is None:
            return jsonify({"error": "no existe"}), 404
        con.execute("UPDATE conversaciones SET no_leidos=0 WHERE numero=?", (numero,))
        filas = con.execute(
            "SELECT * FROM (SELECT * FROM mensajes WHERE numero=? ORDER BY id DESC LIMIT 300) ORDER BY id",
            (numero,),
        ).fetchall()
    return jsonify({
        "numero": numero,
        "titulo": titulo(c),
        "nombre": c["nombre"],
        "humano": bool(c["humano"]),
        "pide_vendedor": bool(c["pide_vendedor"]),
        "ventana": ventana_abierta(c),
        "mensajes": [{
            "id": m["id"], "quien": m["quien"], "tipo": m["tipo"], "texto": m["texto"],
            "archivo": "/media/" + m["archivo"] if m["archivo"] else "",
            "nombre_archivo": m["nombre_archivo"], "hora": fmt(m["ts"]),
            "estado": m["estado"], "error": m["error"],
        } for m in filas],
    })


@app.route("/api/guardar_nombre", methods=["POST"])
@requiere_login
def api_guardar_nombre():
    data = request.get_json(silent=True) or {}
    numero = str(data.get("numero", ""))
    nombre = str(data.get("nombre", "")).strip()[:60]
    if not numero_valido(numero) or conv(numero) is None:
        return jsonify({"ok": False, "error": "Número inválido"}), 400
    marcar(numero, nombre=nombre)
    return jsonify({"ok": True})


def _despues_de_enviar(numero):
    marcar(numero, humano=1, pide_vendedor=0, fallos=0)  # ya lo atiende una persona


@app.route("/api/enviar", methods=["POST"])
@requiere_login
def api_enviar():
    data = request.get_json(silent=True) or {}
    numero = str(data.get("numero", ""))
    texto = str(data.get("texto", "")).strip()
    if not numero_valido(numero) or not texto:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400
    if len(texto) > 4000:
        return jsonify({"ok": False, "error": "Mensaje demasiado largo"}), 400
    c = conv(numero)
    if c is None:
        return jsonify({"ok": False, "error": "La conversación no existe"}), 404
    if not ventana_abierta(c):
        return jsonify({"ok": False, "error": MSG_VENTANA}), 409
    ok, wamid, error = enviar_texto(numero, texto)
    if not ok:
        return jsonify({"ok": False, "error": error}), 502
    registrar(numero, "vendedor", "text", texto, wamid=wamid, estado="sent")
    _despues_de_enviar(numero)
    return jsonify({"ok": True})


def detectar_tipo(datos):
    """Mira el contenido real del archivo (no solo el nombre)."""
    if datos.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg", "image/jpeg"
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png", "image/png"
    if datos.startswith(b"%PDF"):
        return "document", ".pdf", "application/pdf"
    return None, None, None


@app.route("/api/enviar_archivo", methods=["POST"])
@requiere_login
def api_enviar_archivo():
    numero = str(request.form.get("numero", ""))
    caption = str(request.form.get("texto", "")).strip()[:1000]
    archivo = request.files.get("archivo")
    if not numero_valido(numero) or archivo is None:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400
    c = conv(numero)
    if c is None:
        return jsonify({"ok": False, "error": "La conversación no existe"}), 404
    if not ventana_abierta(c):
        return jsonify({"ok": False, "error": MSG_VENTANA}), 409

    datos = archivo.read()
    tipo, ext, mime = detectar_tipo(datos)
    if not tipo:
        return jsonify({"ok": False, "error": "Solo se pueden enviar fotos JPG o PNG y archivos PDF."}), 400
    if len(datos) > (MAX_FOTO if tipo == "image" else MAX_PDF):
        return jsonify({"ok": False, "error": "El archivo es demasiado pesado (fotos hasta 5 MB, PDF hasta 16 MB)."}), 400

    nombre = uuid.uuid4().hex + ext
    ruta = os.path.join(MEDIA_DIR, nombre)
    with open(ruta, "wb") as f:
        f.write(datos)
    nombre_mostrado = secure_filename(archivo.filename or "")[:80] or ("documento.pdf" if tipo == "document" else "foto" + ext)

    media_id, error = subir_media(ruta, mime)
    if not media_id:
        return jsonify({"ok": False, "error": error or "No se pudo subir el archivo a WhatsApp"}), 502
    cuerpo = {"id": media_id}
    if tipo == "image":
        if caption:
            cuerpo["caption"] = caption
    else:
        cuerpo["filename"] = nombre_mostrado
        if caption:
            cuerpo["caption"] = caption
    ok, wamid, error = meta_post({
        "messaging_product": "whatsapp", "to": a_destino(numero), "type": tipo, tipo: cuerpo,
    })
    if not ok:
        return jsonify({"ok": False, "error": error}), 502
    registrar(numero, "vendedor", tipo, caption, archivo="m/" + nombre,
              nombre_archivo=nombre_mostrado if tipo == "document" else "", wamid=wamid, estado="sent")
    _despues_de_enviar(numero)
    return jsonify({"ok": True})


@app.route("/api/devolver_bot", methods=["POST"])
@requiere_login
def api_devolver_bot():
    data = request.get_json(silent=True) or {}
    numero = str(data.get("numero", ""))
    if not numero_valido(numero) or conv(numero) is None:
        return jsonify({"ok": False, "error": "Número inválido"}), 400
    marcar(numero, humano=0, pide_vendedor=0, fallos=0)
    return jsonify({"ok": True})


@app.route("/api/reporte")
@requiere_login
def api_reporte():
    try:
        dias = max(1, min(90, int(request.args.get("dias", 7))))
    except ValueError:
        dias = 7
    desde = time.time() - dias * 86400
    with tx() as con:
        nuevas = con.execute("SELECT COUNT(*) FROM conversaciones WHERE creada>=?", (desde,)).fetchone()[0]
        recibidos = con.execute(
            "SELECT COUNT(*) FROM mensajes WHERE quien='cliente' AND ts>=?", (desde,)).fetchone()[0]
        motivos = con.execute(
            "SELECT detalle, COUNT(*) n FROM eventos WHERE tipo='vendedor' AND ts>=? "
            "GROUP BY detalle ORDER BY n DESC", (desde,)).fetchall()
        prods = con.execute(
            "SELECT detalle, COUNT(*) n FROM eventos WHERE tipo='producto' AND ts>=? "
            "GROUP BY detalle ORDER BY n DESC LIMIT 10", (desde,)).fetchall()
        sin_entender = con.execute(
            "SELECT MIN(detalle) d, COUNT(*) n FROM eventos WHERE tipo='no_entendido' AND ts>=? "
            "GROUP BY lower(detalle) ORDER BY n DESC, MAX(ts) DESC LIMIT 15", (desde,)).fetchall()
        por_dia = con.execute(
            "SELECT strftime('%d/%m', ts, 'unixepoch', '-3 hours') dia, COUNT(*) n FROM mensajes "
            "WHERE quien='cliente' AND ts>=? GROUP BY dia ORDER BY MIN(ts)", (desde,)).fetchall()
    nombres_motivo = {
        "pedido": "Pidieron un vendedor", "precio": "Preguntaron precios", "archivo": "Mandaron foto/audio/archivo",
        "insistencia": "El bot no los entendió (2 veces)", "direccion": "Dirección (sin dato cargado)",
        "pago": "Medios de pago (sin dato cargado)",
    }
    return jsonify({
        "dias": dias,
        "conversaciones_nuevas": nuevas,
        "mensajes_clientes": recibidos,
        "derivaciones": sum(m["n"] for m in motivos),
        "motivos": [{"texto": nombres_motivo.get(m["detalle"], m["detalle"]), "n": m["n"]} for m in motivos],
        "productos": [{"texto": PRODUCTOS.get(p["detalle"], {}).get("nombre", p["detalle"]), "n": p["n"]} for p in prods],
        "sin_entender": [{"texto": s["d"], "n": s["n"]} for s in sin_entender],
        "por_dia": [{"texto": d["dia"], "n": d["n"]} for d in por_dia],
    })


@app.route("/media/<origen>/<nombre>")
@requiere_login
def media(origen, nombre):
    """Archivos de clientes y fotos del catálogo: solo se ven con sesión iniciada."""
    base = {"m": MEDIA_DIR, "c": CATALOGO_DIR}.get(origen)
    if base is None or secure_filename(nombre) != nombre:
        abort(404)
    ruta = os.path.join(base, nombre)
    if not os.path.isfile(ruta):
        abort(404)
    ext = os.path.splitext(nombre)[1].lower()
    mime = MIME_POR_EXT.get(ext) or {".jpeg": "image/jpeg"}.get(ext)
    resp = send_file(ruta, mimetype=mime or "application/octet-stream", as_attachment=not mime, download_name=nombre)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# =====================================================================
# 11) PANEL WEB  (se abre en /panel)
# =====================================================================
PANEL_HTML = r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel Ferretería</title>
<style nonce="__NONCE__">
:root{--verde:#075e54;--gris:#667781;--borde:#e3e3e3;--fondo:#efeae2}
*{box-sizing:border-box}
body{margin:0;height:100vh;height:100dvh;display:flex;flex-direction:column;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--fondo);color:#111}
header{background:var(--verde);color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;gap:10px}
header .der{display:flex;align-items:center;gap:8px}
header a{color:#fff;font-size:14px}
button{border:0;border-radius:8px;padding:8px 14px;background:var(--verde);color:#fff;font:inherit;cursor:pointer}
button.sec{background:var(--gris)}
button.ghost{background:rgba(255,255,255,.18);padding:6px 10px;font-size:13px}
button:disabled{opacity:.5;cursor:default}
.oculto{display:none!important}
#main{flex:1;display:flex;min-height:0}
#colLista{width:340px;background:#fff;border-right:1px solid var(--borde);display:flex;flex-direction:column;min-height:0}
.filtros{padding:8px;border-bottom:1px solid var(--borde);display:flex;flex-direction:column;gap:6px}
.filtros input[type=search]{padding:8px;border:1px solid #ccc;border-radius:8px;font:inherit}
.chk{font-size:13px;color:var(--gris);display:flex;align-items:center;gap:6px}
#lista{overflow-y:auto;flex:1}
.item{padding:11px 14px;border-bottom:1px solid #f0f0f0;cursor:pointer}
.item:hover,.item.sel{background:#f0f2f5}
.item.pide{background:#fff4d6}
.fila{display:flex;justify-content:space-between;gap:8px}
.num{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hora{font-size:12px;color:var(--gris);white-space:nowrap}
.prev{font-size:13px;color:#555;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.badge{background:#25d366;color:#fff;border-radius:10px;padding:0 7px;font-size:12px;margin-left:6px}
.vacio{padding:24px;text-align:center;color:var(--gris)}
#chat{flex:1;display:flex;flex-direction:column;min-width:0}
#vacio{margin:auto}
#panelChat{flex:1;display:flex;flex-direction:column;min-height:0}
#cab{background:#f0f2f5;padding:8px 12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--borde);flex-wrap:wrap}
#cab .atras{display:none}
#titulo{font-size:16px}
#nombreCaja{display:flex;gap:6px}
#inputNombre{padding:5px 8px;border:1px solid #ccc;border-radius:6px;font-size:13px;width:150px}
#estado{margin-left:auto;font-size:13px;color:var(--gris)}
#avisoVentana{background:#fff3cd;color:#664d03;padding:8px 12px;font-size:13px;border-bottom:1px solid #ffe69c}
#msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:6px}
.msg{max-width:78%;padding:7px 10px;border-radius:8px;box-shadow:0 1px 1px #0002;overflow-wrap:anywhere}
.msg .cuerpo{white-space:pre-wrap}
.etq{font-size:11px;font-weight:600;color:#555;display:block;margin-bottom:2px}
.cliente{background:#fff;align-self:flex-start}
.bot{background:#d9edff;align-self:flex-end}
.vendedor{background:#dcf8c6;align-self:flex-end}
.pie{display:block;font-size:11px;color:var(--gris);margin-top:3px;text-align:right}
.tick.leido{color:#34b7f1}
.falla{margin-top:4px;font-size:12px;color:#b00020}
img.foto{max-width:240px;max-height:260px;border-radius:8px;cursor:pointer;display:block;margin-bottom:4px}
video.video{max-width:260px;border-radius:8px;display:block;margin-bottom:4px}
audio{max-width:240px;display:block;margin-bottom:4px}
a.archivo{display:inline-block;padding:6px 10px;background:var(--verde);color:#fff;border-radius:6px;text-decoration:none;font-size:13px;margin-bottom:4px}
#rapidas{display:flex;gap:6px;overflow-x:auto;padding:6px 10px 0;background:#f0f2f5}
.chip{background:#fff;color:#111;border:1px solid #ccc;border-radius:14px;padding:4px 10px;font-size:12px;white-space:nowrap}
#form{display:flex;gap:8px;padding:10px;background:#f0f2f5;align-items:center}
#texto{flex:1;resize:none;border:1px solid #ccc;border-radius:8px;padding:8px;font:inherit;height:44px}
.adjuntar{cursor:pointer;font-size:22px;padding:0 6px;display:flex;align-items:center}
#toast{position:fixed;left:50%;bottom:78px;transform:translateX(-50%);background:#323232;color:#fff;padding:10px 16px;border-radius:8px;max-width:90vw;z-index:20;font-size:14px}
#toast.malo{background:#b00020}
#modal{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;padding:16px;z-index:10}
.caja{background:#fff;border-radius:10px;max-width:520px;width:100%;max-height:90vh;overflow-y:auto;padding:18px}
.caja h2{margin:0 0 6px}
.caja h3{margin:16px 0 4px;font-size:15px}
.caja ul{margin:0;padding-left:18px}
.caja li{margin:2px 0}
.cifras{display:flex;gap:10px;flex-wrap:wrap}
.cifra{background:#f0f2f5;border-radius:8px;padding:8px 12px;flex:1;min-width:110px}
.cifra b{display:block;font-size:22px}
.suave{color:var(--gris);font-size:13px}
@media(max-width:700px){
 #colLista{width:100%}
 #main.abierto #colLista{display:none}
 #main:not(.abierto) #chat{display:none}
 #cab .atras{display:inline-block}
 #inputNombre{width:110px}
 .msg{max-width:90%}
}
</style></head><body>
<header>
 <strong>📋 Panel de la ferretería</strong>
 <span class="der">
  <button class="ghost" id="btnSonido">🔕 Sonido: no</button>
  <button class="ghost" id="btnReporte">📊 Reporte</button>
  <a href="/salir">Salir</a>
 </span>
</header>
<div id="main">
 <aside id="colLista">
  <div class="filtros">
   <input type="search" id="buscar" placeholder="Buscar por nombre o número...">
   <label class="chk"><input type="checkbox" id="soloPend"> Solo los que esperan vendedor <span id="cuentaPend"></span></label>
  </div>
  <div id="lista"></div>
 </aside>
 <section id="chat">
  <div id="vacio" class="vacio">Elegí una conversación de la lista.</div>
  <div id="panelChat" class="oculto">
   <div id="cab">
    <button class="sec atras" id="atras">←</button>
    <strong id="titulo"></strong>
    <span id="nombreCaja"><input type="text" id="inputNombre" placeholder="Nombre del cliente" maxlength="60"><button class="sec" id="guardarNombre">Guardar</button></span>
    <span id="estado"></span>
    <button class="sec oculto" id="devolver">Devolver al bot</button>
   </div>
   <div id="avisoVentana" class="oculto">⏳ Pasaron más de 24 hs desde el último mensaje del cliente: WhatsApp no deja escribirle primero. Esperá a que él escriba.</div>
   <div id="msgs"></div>
   <div id="rapidas"></div>
   <div id="form">
    <label class="adjuntar" title="Adjuntar foto o PDF">📎<input type="file" id="inputArchivo" accept="image/*,.pdf,application/pdf" class="oculto"></label>
    <textarea id="texto" placeholder="Escribí tu respuesta..."></textarea>
    <button id="enviar">Enviar</button>
   </div>
  </div>
 </section>
</div>
<div id="modal" class="oculto"><div class="caja"><div id="reporteCuerpo"></div><p><button id="cerrarModal">Cerrar</button></p></div></div>
<div id="toast" class="oculto"></div>
<script nonce="__NONCE__">
(() => {
"use strict";
const $ = id => document.getElementById(id);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt !== undefined && txt !== null) e.textContent = txt;
  return e;
};
const vaciar = n => { while (n.firstChild) n.removeChild(n.firstChild); };
const norm = s => (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");

let actual = null, datosLista = [], pintados = new Map(), previo = null;
let sonidoOn = false, audioCtx = null, ventanaAbierta = true, enviando = false, cargandoChat = false;
try { sonidoOn = localStorage.getItem("sonido") === "1"; } catch (e) {}

let toastTimer = null;
function toast(texto, malo) {
  const t = $("toast");
  t.textContent = texto;
  t.className = malo ? "malo" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "oculto"; }, 6000);
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.reload(); throw new Error("sin sesión"); }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  return { ok: r.ok, status: r.status, data };
}

/* ---------- aviso sonoro ---------- */
function iniciarAudio() {
  if (audioCtx) return;
  try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { audioCtx = null; }
}
function pitido() {
  if (!sonidoOn || !audioCtx) return;
  try {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = "sine"; o.frequency.value = 880; g.gain.value = 0.15;
    o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.25);
  } catch (e) {}
}
function pintarBotonSonido() { $("btnSonido").textContent = sonidoOn ? "🔔 Sonido: sí" : "🔕 Sonido: no"; }
$("btnSonido").addEventListener("click", () => {
  sonidoOn = !sonidoOn;
  try { localStorage.setItem("sonido", sonidoOn ? "1" : "0"); } catch (e) {}
  if (sonidoOn) { iniciarAudio(); pitido(); }
  pintarBotonSonido();
});
document.addEventListener("click", iniciarAudio, { once: true });
pintarBotonSonido();

/* ---------- lista de conversaciones ---------- */
async function cargarLista() {
  const r = await api("/api/conversaciones");
  if (!r.ok || !Array.isArray(r.data)) return;
  datosLista = r.data;
  const pend = datosLista.filter(c => c.pide_vendedor).length;
  const aAtender = datosLista.filter(c => c.humano).reduce((s, c) => s + c.no_leidos, 0);
  document.title = (pend ? "(" + pend + ") 🔔 " : "") + "Panel Ferretería";
  if (previo && (pend > previo.pend || aAtender > previo.aAtender)) pitido();
  previo = { pend, aAtender };
  $("cuentaPend").textContent = pend ? "(" + pend + ")" : "";
  pintarLista();
}
function pintarLista() {
  const q = norm($("buscar").value.trim());
  const solo = $("soloPend").checked;
  const caja = $("lista");
  vaciar(caja);
  const filas = datosLista.filter(c => (!solo || c.pide_vendedor) && (!q || norm(c.titulo + " " + c.numero).includes(q)));
  if (!filas.length) {
    caja.appendChild(el("div", "vacio", datosLista.length ? "No hay resultados." : "Todavía no hay mensajes."));
    return;
  }
  filas.forEach(c => {
    const item = el("div", "item" + (c.pide_vendedor ? " pide" : "") + (c.numero === actual ? " sel" : ""));
    const f1 = el("div", "fila");
    const izq = el("span", "num", c.titulo);
    if (c.no_leidos) izq.appendChild(el("span", "badge", String(c.no_leidos)));
    f1.appendChild(izq);
    f1.appendChild(el("span", "hora", c.hora));
    item.appendChild(f1);
    let marca = "";
    if (c.pide_vendedor) marca = "🔔 Espera" + (c.espera_min ? " hace " + c.espera_min + " min" : "") + " · ";
    else if (c.humano) marca = "👤 ";
    item.appendChild(el("div", "prev", marca + c.texto));
    item.addEventListener("click", () => abrir(c.numero));
    caja.appendChild(item);
  });
}
$("buscar").addEventListener("input", pintarLista);
$("soloPend").addEventListener("change", pintarLista);

/* ---------- conversación abierta ---------- */
function abrir(numero) {
  actual = numero;
  pintados = new Map();
  vaciar($("msgs"));
  $("main").classList.add("abierto");
  $("vacio").classList.add("oculto");
  $("panelChat").classList.remove("oculto");
  cargarChat(true).catch(() => {});
  pintarLista();
}
function actualizarControles() {
  $("texto").disabled = !ventanaAbierta || enviando;
  $("enviar").disabled = !ventanaAbierta || enviando;
  $("inputArchivo").disabled = !ventanaAbierta || enviando;
}
const NOMBRES = { cliente: "Cliente", bot: "Bot", vendedor: "Vendedor" };
function pintarMensaje(m, box) {
  const b = el("div", "msg " + m.quien);
  b.appendChild(el("span", "etq", NOMBRES[m.quien] || m.quien));
  if (m.archivo) {
    if (m.tipo === "image" || m.tipo === "sticker") {
      const img = el("img", "foto");
      img.src = m.archivo; img.alt = "Imagen"; img.loading = "lazy";
      img.addEventListener("click", () => window.open(m.archivo, "_blank", "noopener"));
      b.appendChild(img);
    } else if (m.tipo === "audio") {
      const a = el("audio"); a.controls = true; a.preload = "none"; a.src = m.archivo; b.appendChild(a);
    } else if (m.tipo === "video") {
      const v = el("video", "video"); v.controls = true; v.preload = "metadata"; v.src = m.archivo; b.appendChild(v);
    } else {
      const a = el("a", "archivo", "📎 " + (m.nombre_archivo || "Archivo"));
      a.href = m.archivo; a.target = "_blank"; a.rel = "noopener";
      a.setAttribute("download", m.nombre_archivo || "archivo");
      b.appendChild(a);
    }
  }
  if (m.texto) b.appendChild(el("div", "cuerpo", m.texto));
  const pie = el("small", "pie");
  pie.appendChild(document.createTextNode(m.hora + " "));
  const tick = el("span", "tick");
  pie.appendChild(tick);
  b.appendChild(pie);
  const err = el("div", "falla oculto");
  b.appendChild(err);
  const refs = { tick, err, estado: null };
  actualizarMensaje(refs, m);
  pintados.set(m.id, refs);
  box.appendChild(b);
}
function actualizarMensaje(refs, m) {
  const clave = m.estado + "|" + m.error;
  if (refs.estado === clave) return;
  refs.estado = clave;
  if (m.quien === "cliente") return;
  refs.tick.className = "tick" + (m.estado === "read" ? " leido" : "");
  refs.tick.textContent = m.estado === "read" || m.estado === "delivered" ? "✓✓" : (m.estado === "sent" ? "✓" : "");
  if (m.estado === "failed") {
    refs.tick.textContent = "⚠️";
    refs.err.textContent = "No se entregó: " + (m.error || "error desconocido");
    refs.err.classList.remove("oculto");
  } else {
    refs.err.classList.add("oculto");
  }
}
async function cargarChat(bajar) {
  if (!actual || cargandoChat) return;
  cargandoChat = true;
  const num = actual;
  try {
    const r = await api("/api/conversaciones/" + num);
    if (!r.ok || num !== actual) return;
    const d = r.data;
    $("titulo").textContent = d.titulo;
    const inp = $("inputNombre");
    if (document.activeElement !== inp) inp.value = d.nombre || "";
    $("estado").textContent = d.humano ? "👤 Atiende una persona" : "🤖 Atiende el bot";
    $("devolver").classList.toggle("oculto", !d.humano);
    ventanaAbierta = d.ventana;
    $("avisoVentana").classList.toggle("oculto", d.ventana);
    actualizarControles();
    const box = $("msgs");
    const abajo = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    let nuevos = false;
    d.mensajes.forEach(m => {
      const ya = pintados.get(m.id);
      if (ya) { actualizarMensaje(ya, m); } else { pintarMensaje(m, box); nuevos = true; }
    });
    if (nuevos && (abajo || bajar)) box.scrollTop = box.scrollHeight;
  } finally { cargandoChat = false; }
}

/* ---------- enviar ---------- */
async function enviar() {
  const t = $("texto").value.trim();
  if (!t || !actual || enviando) return;
  enviando = true; actualizarControles();
  try {
    const r = await api("/api/enviar", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ numero: actual, texto: t })
    });
    if (r.ok && r.data && r.data.ok) { $("texto").value = ""; }
    else { toast("No se pudo enviar: " + ((r.data && r.data.error) || "error desconocido"), true); }
  } catch (e) { if (e.message !== "sin sesión") toast("Error de conexión. Probá de nuevo.", true); }
  enviando = false; actualizarControles();
  cargarChat(true).catch(() => {}); cargarLista().catch(() => {});
}
function reducirImagen(file) {
  return new Promise(resolve => {
    if (!file.type.startsWith("image/")) return resolve(file);
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      const esc = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(img.width * esc));
      c.height = Math.max(1, Math.round(img.height * esc));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      c.toBlob(b => resolve(b || file), "image/jpeg", 0.85);
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };
    img.src = url;
  });
}
async function enviarArchivo() {
  const inp = $("inputArchivo");
  if (!inp.files.length || !actual || enviando) return;
  const f = inp.files[0];
  inp.value = "";
  enviando = true; actualizarControles();
  try {
    const blob = await reducirImagen(f);
    const fd = new FormData();
    fd.append("numero", actual);
    fd.append("texto", $("texto").value.trim());
    fd.append("archivo", blob, f.type.startsWith("image/") ? "foto.jpg" : (f.name || "archivo.pdf"));
    const r = await api("/api/enviar_archivo", { method: "POST", body: fd });
    if (r.ok && r.data && r.data.ok) { $("texto").value = ""; }
    else { toast("No se pudo enviar el archivo: " + ((r.data && r.data.error) || "error desconocido"), true); }
  } catch (e) { if (e.message !== "sin sesión") toast("Error de conexión al enviar el archivo.", true); }
  enviando = false; actualizarControles();
  cargarChat(true).catch(() => {}); cargarLista().catch(() => {});
}
async function guardarNombre() {
  if (!actual) return;
  const r = await api("/api/guardar_nombre", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ numero: actual, nombre: $("inputNombre").value.trim() })
  });
  if (r.ok) { toast("Nombre guardado"); cargarChat(false).catch(() => {}); cargarLista().catch(() => {}); }
  else { toast("No se pudo guardar el nombre.", true); }
}
async function devolver() {
  if (!actual) return;
  await api("/api/devolver_bot", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ numero: actual })
  });
  cargarChat(false).catch(() => {}); cargarLista().catch(() => {});
}
$("enviar").addEventListener("click", enviar);
$("inputArchivo").addEventListener("change", enviarArchivo);
$("guardarNombre").addEventListener("click", guardarNombre);
$("inputNombre").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); guardarNombre(); } });
$("devolver").addEventListener("click", devolver);
$("texto").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); enviar(); } });
$("atras").addEventListener("click", () => {
  actual = null;
  $("main").classList.remove("abierto");
  $("panelChat").classList.add("oculto");
  $("vacio").classList.remove("oculto");
  pintarLista();
});

/* ---------- reporte semanal ---------- */
function bloqueLista(cuerpo, titulo, filas, vacio) {
  cuerpo.appendChild(el("h3", "", titulo));
  if (!filas.length) { cuerpo.appendChild(el("p", "suave", vacio)); return; }
  const ul = el("ul");
  filas.forEach(f => ul.appendChild(el("li", "", f.texto + " — " + f.n)));
  cuerpo.appendChild(ul);
}
async function verReporte() {
  const cuerpo = $("reporteCuerpo");
  vaciar(cuerpo);
  const r = await api("/api/reporte?dias=7");
  if (!r.ok || !r.data) { cuerpo.appendChild(el("p", "", "No se pudo cargar el reporte.")); }
  else {
    const d = r.data;
    cuerpo.appendChild(el("h2", "", "📊 Últimos " + d.dias + " días"));
    const cifras = el("div", "cifras");
    [["Clientes nuevos", d.conversaciones_nuevas], ["Mensajes recibidos", d.mensajes_clientes], ["Derivados a vendedor", d.derivaciones]].forEach(([t, n]) => {
      const c = el("div", "cifra"); c.appendChild(el("b", "", String(n))); c.appendChild(document.createTextNode(t)); cifras.appendChild(c);
    });
    cuerpo.appendChild(cifras);
    bloqueLista(cuerpo, "Por qué se derivó a un vendedor", d.motivos, "Todavía no hubo derivaciones.");
    bloqueLista(cuerpo, "Productos más consultados", d.productos, "Todavía no consultaron productos del catálogo.");
    bloqueLista(cuerpo, "Lo que el bot no entendió (ideas para agregar)", d.sin_entender, "Nada por ahora.");
    bloqueLista(cuerpo, "Mensajes de clientes por día", d.por_dia, "Sin datos.");
  }
  $("modal").classList.remove("oculto");
}
$("btnReporte").addEventListener("click", () => { verReporte().catch(() => {}); });
$("cerrarModal").addEventListener("click", () => $("modal").classList.add("oculto"));
$("modal").addEventListener("click", e => { if (e.target === $("modal")) $("modal").classList.add("oculto"); });
document.addEventListener("keydown", e => { if (e.key === "Escape") $("modal").classList.add("oculto"); });

/* ---------- arranque ---------- */
api("/api/config").then(r => {
  if (!r.ok || !r.data) return;
  const caja = $("rapidas");
  (r.data.rapidas || []).forEach(t => {
    const b = el("button", "chip", t.length > 30 ? t.slice(0, 30) + "…" : t);
    b.title = t;
    b.addEventListener("click", () => {
      const ta = $("texto");
      ta.value = (ta.value ? ta.value + " " : "") + t;
      ta.focus();
    });
    caja.appendChild(b);
  });
  if (!caja.children.length) caja.classList.add("oculto");
}).catch(() => {});
cargarLista().catch(() => {});
setInterval(() => { cargarLista().catch(() => {}); cargarChat(false).catch(() => {}); }, 4000);
})();
</script>
</body></html>"""


@app.route("/panel")
def panel():
    if not session.get("ok"):
        return pagina_login(), 200
    nonce = secrets.token_urlsafe(16)
    resp = app.response_class(PANEL_HTML.replace("__NONCE__", nonce), mimetype="text/html")
    resp.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "img-src 'self' blob: data:; media-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return resp


if __name__ == "__main__":
    # Solo para probar en tu compu. En Render se usa gunicorn.
    app.run(port=5000, debug=True)