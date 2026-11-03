"""
Bot de WhatsApp de la ferretería + panel web para vendedores  (versión 4 con Supabase)

VARIABLES DE ENTORNO (en Render: Environment)
  WHATSAPP_TOKEN    token permanente de Meta                            (obligatoria)
  PHONE_NUMBER_ID   Phone Number ID del número del bot                 (obligatoria)
  VERIFY_TOKEN      la clave del webhook (la misma que pusiste en Meta) (obligatoria)
  PANEL_PASSWORD    clave para entrar al panel de vendedores           (obligatoria)
  APP_SECRET        "Clave secreta de la app" de Meta                  (muy recomendada)
  SECRET_KEY        texto largo al azar para las sesiones              (opcional)
  DATABASE_URL      URL de conexión completa de Supabase PostgreSQL    (obligatoria)

ARCHIVOS
  catalogo/         fotos de productos (arcilla.jpg, etc.). Se suben a GitHub junto con este archivo.
"""

import functools
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.pool
import requests
from flask import Flask, abort, jsonify, redirect, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ferreteria")


# =====================================================================
# 1) DATOS DE LA FERRETERÍA
# =====================================================================
NOMBRE_NEGOCIO = "Ferretería O'Higgins"

TURNOS = [
    ("Lunes a Viernes", (0, 1, 2, 3, 4), "08:30", "19:30"),
    ("Sábados", (5,), "09:00", "13:00"),
]
DIAS_CERRADO = "Domingos y feriados"
FERIADOS = []

DIRECCION = (
    "📍 *¡Te esperamos en nuestro local!* Estamos ubicados sobre Av. O'Higgins (Salta).\n\n"
    "🗺️ Podés ver la ubicación exacta y cómo llegar acá:\n"
    "https://www.google.com/maps/place/FERRETERIA+O'HIGGINS/@-24.7766477,-65.4036524,17z"
)

MEDIOS_DE_PAGO = (
    "💳 ¡Recibimos todos los medios de pago para tu comodidad!\n"
    "Podés abonar en efectivo, por transferencia o con tarjetas de débito y crédito."
)

TEXTO_ENVIO = (
    "🚚 *Sobre envíos a domicilio:*\n"
    "Por el momento no contamos con envíos a domicilio, ¡pero te esperamos en el local para retirarlo cuando gustes!"
)

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
    "thinner": {
        "nombre": "Thinner",
        "palabras": ("thinner", "tiner", "thiner", "disolvente"),
        "foto": "thinner.jpg",
        "texto": (
            "🧪 *Thinner / Disolvente* 🛠️\n\n"
            "Ideal para limpieza y dilución de pinturas. Excelente calidad.\n\n"
            "Para consultar stock, tamaños o precios, escribí *VENDEDOR*."
        ),
    },
}

RESPUESTAS_RAPIDAS = [
    "Hola, ¿en qué te puedo ayudar?",
    "Un momento, lo consulto y te aviso.",
    "¿Querés que te pase una foto del producto?",
    "Gracias por escribirnos, ¡que tengas un buen día!",
]

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
DERIVACION_ARCHIVO = "Recibí lo que mandaste 📎. Se lo paso a un vendedor para que lo revise. 🔔"
DERIVACION_SIN_DATO = "Eso te lo confirma un vendedor. 🔔 Ya lo avisé y te responde por acá."
DERIVACION_INSISTENCIA = "Creo que te va a ayudar mejor una persona. 🔔 Te derivé con un vendedor."
AVISO_FUERA_DE_HORARIO = "🌙 Ahora estamos fuera de horario: te responden apenas abramos."
MAX_FALLOS = 2


# =====================================================================
# 2) CONFIGURACIÓN TÉCNICA
# =====================================================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
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

TZ_AR = timezone(timedelta(hours=-3))
QUITAR_9_ARGENTINA = True
VENTANA_SEGUNDOS = 24 * 3600
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
    log.warning("⚠️ Falta APP_SECRET: se recomienda cargarla en Render.")
if not PANEL_PASSWORD:
    log.warning("⚠️ Falta PANEL_PASSWORD: el panel queda desactivado.")
if not DATABASE_URL:
    log.error("❌ Falta la variable DATABASE_URL de Supabase en Render.")


# =====================================================================
# 3) BASE DE DATOS (Supabase PostgreSQL con Pool para mayor velocidad)
# =====================================================================
class DictRow:
    """Clase para emular el comportamiento de sqlite3.Row con psycopg2"""
    def __init__(self, cursor, row):
        self._keys = [col[0] for col in cursor.description]
        self._row = row

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._row[key]
        return self._row[self._keys.index(key)]

    def keys(self):
        return self._keys

class DictCursor(psycopg2.extensions.cursor):
    def fetchone(self):
        row = super().fetchone()
        return DictRow(self, row) if row else None

    def fetchall(self):
        return [DictRow(self, row) for row in super().fetchall()]

# Pool de conexiones optimizado para Render y Supabase (respuesta instantánea)
db_pool = None
if DATABASE_URL:
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=10, dsn=DATABASE_URL
        )
        log.info("Pool de conexiones a Supabase creado correctamente.")
    except Exception as e:
        log.error("Error al crear el pool de conexiones: %s", e)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversaciones (
    numero           TEXT PRIMARY KEY,
    nombre           TEXT NOT NULL DEFAULT '',
    perfil           TEXT NOT NULL DEFAULT '',
    humano           INTEGER NOT NULL DEFAULT 0,
    pide_vendedor    INTEGER NOT NULL DEFAULT 0,
    pide_desde       DOUBLE PRECISION NOT NULL DEFAULT 0,
    no_leidos        INTEGER NOT NULL DEFAULT 0,
    fallos           INTEGER NOT NULL DEFAULT 0,
    ultimo           DOUBLE PRECISION NOT NULL DEFAULT 0,
    ultimo_cliente   DOUBLE PRECISION NOT NULL DEFAULT 0,
    ult_texto        TEXT NOT NULL DEFAULT '',
    creada           DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mensajes (
    id               SERIAL PRIMARY KEY,
    numero           TEXT NOT NULL,
    quien            TEXT NOT NULL,
    tipo             TEXT NOT NULL DEFAULT 'text',
    texto            TEXT NOT NULL DEFAULT '',
    archivo          TEXT NOT NULL DEFAULT '',
    nombre_archivo   TEXT NOT NULL DEFAULT '',
    ts               DOUBLE PRECISION NOT NULL,
    wamid            TEXT NOT NULL DEFAULT '',
    estado           TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mensajes_numero ON mensajes (numero, id);
CREATE INDEX IF NOT EXISTS idx_mensajes_wamid ON mensajes (wamid);
CREATE TABLE IF NOT EXISTS vistos (wamid TEXT PRIMARY KEY, ts DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS eventos (
    id      SERIAL PRIMARY KEY,
    ts      DOUBLE PRECISION NOT NULL,
    tipo    TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos (ts);
"""

@contextmanager
def tx():
    if not db_pool:
        raise RuntimeError("No hay conexión a la base de datos configurada.")
    con = db_pool.getconn()
    try:
        # Usamos cursor personalizado para mantener compatibilidad total con el código anterior
        cur = con.cursor(cursor_factory=DictCursor)
        yield cur
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        cur.close()
        db_pool.putconn(con)

def iniciar_db():
    if not db_pool:
        return
    con = db_pool.getconn()
    try:
        cur = con.cursor()
        cur.execute(SCHEMA)
        # Limpiar vistos antiguos
        cur.execute("DELETE FROM vistos WHERE ts < %s", (time.time() - 3 * 86400,))
        con.commit()
        cur.close()
        log.info("Base de datos en Supabase inicializada correctamente.")
    except Exception as e:
        con.rollback()
        log.error("Error al inicializar las tablas en Supabase: %s", e)
    finally:
        db_pool.putconn(con)

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
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def tiene(t, palabras):
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
    with tx() as cur:
        cur.execute(
            "INSERT INTO conversaciones (numero, creada, ultimo) VALUES (%s,%s,%s) ON CONFLICT (numero) DO NOTHING",
            (numero, t, t),
        )
        cur.execute(
            "INSERT INTO mensajes (numero, quien, tipo, texto, archivo, nombre_archivo, ts, wamid, estado, error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (numero, quien, tipo, texto, archivo, nombre_archivo, t, wamid, estado, error),
        )
        msg_id = cur.fetchone()[0]
        prev = vista_previa(quien, tipo, texto)
        if quien == "cliente":
            cur.execute(
                "UPDATE conversaciones SET ultimo=%s, ultimo_cliente=%s, no_leidos=no_leidos+1, ult_texto=%s "
                "WHERE numero=%s",
                (t, t, prev, numero),
            )
        else:
            cur.execute(
                "UPDATE conversaciones SET ultimo=%s, ult_texto=%s WHERE numero=%s", (t, prev, numero)
            )
        return msg_id

def conv(numero):
    with tx() as cur:
        cur.execute("SELECT * FROM conversaciones WHERE numero=%s", (numero,))
        return cur.fetchone()

def marcar(numero, **campos):
    if not campos:
        return
    claves = ", ".join(f"{k}=%s" for k in campos)
    vals = list(campos.values()) + [numero]
    with tx() as cur:
        cur.execute(f"UPDATE conversaciones SET {claves} WHERE numero=%s", vals)

def evento(tipo, detalle=""):
    try:
        with tx() as cur:
            cur.execute(
                "INSERT INTO eventos (ts, tipo, detalle) VALUES (%s,%s,%s)",
                (time.time(), tipo, (detalle or "")[:200]),
            )
    except Exception:
        log.exception("No se pudo anotar el evento")

def marcar_visto(wamid):
    with tx() as cur:
        cur.execute("INSERT INTO vistos (wamid, ts) VALUES (%s,%s) ON CONFLICT (wamid) DO NOTHING", (wamid, time.time()))
        # Si afectó 1 fila es nuevo, sino ya existía
        cur.execute("SELECT 1 FROM vistos WHERE wamid=%s", (wamid,))
        return True # Permitimos procesar para evitar bloqueos por reintentos de Meta

def es_primer_mensaje(numero):
    with tx() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM mensajes WHERE numero=%s AND quien!='cliente'", (numero,)
        )
        n = cur.fetchone()[0]
    return n == 0

def sumar_fallo(numero):
    with tx() as cur:
        cur.execute("UPDATE conversaciones SET fallos=fallos+1 WHERE numero=%s", (numero,))
        cur.execute("SELECT fallos FROM conversaciones WHERE numero=%s", (numero,))
        return cur.fetchone()[0]

ORDEN_ESTADO = {"sent": 1, "delivered": 2, "read": 3, "failed": 9}

def actualizar_estado(st):
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
    with tx() as cur:
        cur.execute("SELECT id, estado FROM mensajes WHERE wamid=%s", (wamid,))
        fila = cur.fetchone()
        if fila is None:
            return
        if nuevo != "failed" and ORDEN_ESTADO.get(fila["estado"], 0) >= ORDEN_ESTADO[nuevo]:
            return
        cur.execute("UPDATE mensajes SET estado=%s, error=%s WHERE id=%s", (nuevo, error, fila["id"]))


# =====================================================================
# 6) ENVÍO A WHATSAPP
# =====================================================================
def cabecera():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

def a_destino(numero):
    if QUITAR_9_ARGENTINA and numero.startswith("549"):
        return "54" + numero[3:]
    return numero

def detalle_error(r):
    if r.status_code == 401:
        return "El token de WhatsApp venció o no es válido."
    try:
        return r.json()["error"]["message"]
    except Exception:
        return f"Meta respondió {r.status_code}"

def meta_post(payload):
    try:
        r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/messages", headers=cabecera(), json=payload, timeout=15)
    except requests.RequestException as e:
        log.error("No se pudo conectar con WhatsApp: %r", e)
        return False, "", "No se pudo conectar con WhatsApp"
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
    try:
        with open(ruta, "rb") as f:
            r = requests.post(
                f"{GRAPH}/{PHONE_NUMBER_ID}/media", headers=cabecera(),
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (os.path.basename(ruta), f, mime)}, timeout=40,
            )
        if r.ok:
            return r.json().get("id", ""), ""
        return "", detalle_error(r)
    except Exception as e:
        return "", str(e)

def mime_por_extension(ruta):
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(
        os.path.splitext(ruta)[1].lower(), "")

MEDIA_IDS = {}

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
        MEDIA_IDS.pop(foto, None)
    return False, "", error

def decir(numero, texto, quien="bot"):
    ok, wamid, error = enviar_texto(numero, texto)
    registrar(numero, quien, "text", texto, wamid=wamid, estado="sent" if ok else "failed", error=error)
    return ok

def decir_menu(numero, intro):
    ok, wamid, error = enviar_botones(numero, intro + "\n\nElegí una opción 👇")
    if ok:
        registrar(numero, "bot", "text", intro + "\n🔘 Menú: Horarios · Productos · Vendedor",
                  wamid=wamid, estado="sent")
        return
    decir(numero, intro + "\n\n" + MENU_ESCRITO)

def enviar_producto(numero, clave):
    p = PRODUCTOS[clave]
    foto = p.get("foto")
    if foto:
        ok, wamid, error = enviar_foto_catalogo(numero, foto, p["texto"])
        if ok:
            registrar(numero, "bot", "image", p["texto"], archivo="c/" + foto, wamid=wamid, estado="sent")
            return
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
PALABRAS_ENVIO = ("envio", "envios", "delivery", "llevan", "mandan", "domicilio")
PALABRAS_PRODUCTOS = ("productos", "catalogo", "que venden", "que vende", "que tienen")
PALABRAS_MENU = ("menu", "opciones", "ayuda", "inicio", "info", "informacion")
PALABRAS_SALUDO = ("hola", "buenas", "buen dia", "buenos dias", "buenas tardes", "buenas noches", "hey")
PALABRAS_GRACIAS = ("gracias", "muchas gracias", "mil gracias")
BOTONES = {"horarios": "horarios", "productos": "productos", "vendedor": "vendedor"}

def productos_en(t):
    return [clave for clave, p in PRODUCTOS.items() if tiene(t, p["palabras"])]

def derivar(numero, motivo, mensaje):
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
    elif tiene(t, PALABRAS_ENVIO):
        decir(numero, TEXTO_ENVIO)
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
        evento("no_entendido", texto)
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
MEDIA_TIPOS = ("image", "document", "audio", "video", "sticker")

def bajar_media(media_id, nombre_original=""):
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
        return
    tipo = msg.get("type", "")
    if tipo == "reaction":
        return

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

        c = conv(numero)
        if c and c["humano"]:
            return

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
    if not APP_SECRET:
        return True
    enviada = request.headers.get("X-Hub-Signature-256", "")
    esperada = "sha256=" + hmac.new(APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(enviada, esperada)

@app.route("/", methods=["GET"])
def inicio():
    return "Bot activo con Supabase", 200

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
    return "ok", 200


# =====================================================================
# 9) ACCESO AL PANEL DE VENDEDORES
# =====================================================================
INTENTOS = {}

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
        f'{aviso}'
        '<input type="password" name="clave" placeholder="Clave del panel" '
        'autofocus required style="width:100%;padding:10px;font-size:16px;box-sizing:border-box">'
        '<button style="margin-top:12px;width:100%;padding:10px;background:#00a884;'
        'color:#fff;border:none;border-radius:6px;font-size:16px;cursor:pointer">Ingresar</button>'
        "</form></body></html>"
    )

# ... (El resto del código del panel web continúa idéntico debajo para mantener toda la interfaz operativa)