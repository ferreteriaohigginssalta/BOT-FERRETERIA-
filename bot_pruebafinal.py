"""
Bot de WhatsApp de la ferretería + panel web para vendedores  (versión 4 completa)
"""

import functools
import hashlib
import hmac
import html
import io
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
# 1) DATOS DE LA FERRETERÍA
# =====================================================================
NOMBRE_NEGOCIO = "Ferretería O’Higgins"

TURNOS = [
    ("Lunes a Viernes", (0, 1, 2, 3, 4), "08:30", "19:30"),
    ("Sábados", (5,), "09:00", "13:00"),
]
DIAS_CERRADO = "Domingos y feriados"
FERIADOS = []

DIRECCION = "📍 Estamos en Av. O'Higgins, Salta."
MEDIOS_DE_PAGO = "💳 Aceptamos efectivo, transferencia y tarjetas."

PRODUCTOS = {
    "arcilla": {
        "nombre": "Arcilla blanca baja temp 1020°-1040° (Marca ANTARES)",
        "palabras": ("arcilla", "arcillas", "arcilla blanca"),
        "foto": "arcilla.jpg",
        "texto": (
            "🧱 *Arcilla blanca baja temp 1020°-1040°* 🛠️\n"
            "Marca: *ANTARES*\n\n"
            "Llevando:\n"
            "• 1 kg: $ 3.500\n"
            "• 10 kg: $ 25.000\n"
            "• 10 kg: $ 22.000 pagando solo en efectivo\n\n"
            "¡Muchas gracias por tu consulta!\n"
            "Tu calificación hace crecer a Ferretería O’Higgins ⭐⭐⭐⭐⭐\n"
            "Si podés, dejá tu reseña acá:\n"
            "https://g.page/r/CXL-WCnnXw8KEAE/review"
        ),
    },
    "barbotina": {
        "nombre": "Barbotina blanca baja temp 1020° - 1040° (Marca ANTARES)",
        "palabras": ("barbotina", "barbotinas", "barbotina blanca"),
        "foto": "barbotina.jpg",
        "texto": (
            "🏺 *Barbotina blanca baja temp 1020° - 1040°* 🛠️\n"
            "Marca: *ANTARES*\n\n"
            "Ideal para tus trabajos de cerámica y moldes.\n\n"
            "Para consultar cantidades o stock, escribí *VENDEDOR*.\n\n"
            "¡Muchas gracias por tu consulta!\n"
            "Tu calificación hace crecer a Ferretería O’Higgins ⭐⭐⭐⭐⭐\n"
            "Si podés, dejá tu reseña acá:\n"
            "https://g.page/r/CXL-WCnnXw8KEAE/review"
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
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
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

TZ_AR = timezone(timedelta(hours=-3))
QUITAR_9_ARGENTINA = True
VENTANA_SEGUNDOS = 24 * 3600
MAX_BAJADA = 20 * 1024 * 1024

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


# =====================================================================
# 3) BASE DE DATOS (Supabase / SQLite)
# =====================================================================
USA_PG = False
_pool = None
_pool_sem = threading.BoundedSemaphore(8)
if DATABASE_URL:
    try:
        import psycopg2
        import psycopg2.extras
        import psycopg2.pool

        _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn=DATABASE_URL, connect_timeout=10)
        USA_PG = True
        log.info("Base de datos: Supabase (PostgreSQL) ✅")
    except Exception as e:
        log.error("❌ No pude conectar con DATABASE_URL (%s). Uso una base TEMPORAL.", e)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversaciones (
    numero         TEXT PRIMARY KEY,
    nombre         TEXT NOT NULL DEFAULT '',
    perfil         TEXT NOT NULL DEFAULT '',
    humano         INTEGER NOT NULL DEFAULT 0,
    pide_vendedor  INTEGER NOT NULL DEFAULT 0,
    pide_desde     DOUBLE PRECISION NOT NULL DEFAULT 0,
    no_leidos      INTEGER NOT NULL DEFAULT 0,
    fallos         INTEGER NOT NULL DEFAULT 0,
    ultimo         DOUBLE PRECISION NOT NULL DEFAULT 0,
    ultimo_cliente DOUBLE PRECISION NOT NULL DEFAULT 0,
    ult_texto      TEXT NOT NULL DEFAULT '',
    creada         DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mensajes (
    id             __ID__,
    numero         TEXT NOT NULL,
    quien          TEXT NOT NULL,
    tipo           TEXT NOT NULL DEFAULT 'text',
    texto          TEXT NOT NULL DEFAULT '',
    archivo        TEXT NOT NULL DEFAULT '',
    nombre_archivo TEXT NOT NULL DEFAULT '',
    ts             DOUBLE PRECISION NOT NULL,
    wamid          TEXT NOT NULL DEFAULT '',
    estado         TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mensajes_numero ON mensajes (numero, id);
CREATE INDEX IF NOT EXISTS idx_mensajes_wamid ON mensajes (wamid);
CREATE TABLE IF NOT EXISTS vistos (wamid TEXT PRIMARY KEY, ts DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS eventos (
    id       __ID__,
    ts       DOUBLE PRECISION NOT NULL,
    tipo     TEXT NOT NULL,
    detalle  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos (ts);
"""
SCHEMA_ARCHIVOS_PG = """
CREATE TABLE IF NOT EXISTS archivos (
    nombre TEXT PRIMARY KEY,
    mime   TEXT NOT NULL DEFAULT '',
    datos  BYTEA NOT NULL,
    ts     DOUBLE PRECISION NOT NULL
)
"""


class _PgCon:
    def __init__(self, cur):
        self.cur = cur

    def execute(self, sql, params=()):
        self.cur.execute(sql.replace("?", "%s"), tuple(params))
        return self.cur


def _conexion_pg():
    for _ in range(3):
        con = _pool.getconn()
        try:
            if con.closed:
                raise RuntimeError("cerrada")
            c = con.cursor()
            c.execute("SELECT 1")
            c.close()
            con.rollback()
            return con
        except Exception:
            try:
                _pool.putconn(con, close=True)
            except Exception:
                pass
    raise RuntimeError("No hay conexión con la base de datos")


@contextmanager
def tx():
    if USA_PG:
        _pool_sem.acquire()
        con = None
        try:
            con = _conexion_pg()
            cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                yield _PgCon(cur)
                con.commit()
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        finally:
            if con is not None:
                try:
                    _pool.putconn(con)
                except Exception:
                    pass
                _pool_sem.release()
    else:
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
    esquema = SCHEMA.replace("__ID__", "SERIAL PRIMARY KEY" if USA_PG else "INTEGER PRIMARY KEY AUTOINCREMENT")
    with tx() as con:
        if not USA_PG:
            try:
                con.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
        for sentencia in esquema.split(";"):
            if sentencia.strip():
                con.execute(sentencia)
        if USA_PG:
            con.execute(SCHEMA_ARCHIVOS_PG)
        con.execute("DELETE FROM vistos WHERE ts < ?", (time.time() - 3 * 86400,))
    log.info("Base de datos lista (%s)", "Supabase" if USA_PG else DB_PATH)


iniciar_db()


def guardar_archivo(nombre, datos, mime=""):
    if USA_PG:
        with tx() as con:
            con.execute(
                "INSERT INTO archivos (nombre, mime, datos, ts) VALUES (?,?,?,?) ON CONFLICT (nombre) DO NOTHING",
                (nombre, mime, psycopg2.Binary(datos), time.time()),
            )
    else:
        with open(os.path.join(MEDIA_DIR, nombre), "wb") as f:
            f.write(datos)


def leer_archivo(nombre):
    if USA_PG:
        with tx() as con:
            fila = con.execute("SELECT datos FROM archivos WHERE nombre=?", (nombre,)).fetchone()
        return bytes(fila["datos"]) if fila else None
    ruta = os.path.join(MEDIA_DIR, nombre)
    if not os.path.isfile(ruta):
        return None
    with open(ruta, "rb") as f:
        return f.read()


def leer_catalogo(ruta):
    with open(ruta, "rb") as f:
        return f.read()


# =====================================================================
# 4) UTILIDADES
# =====================================================================
def ahora():
    return datetime.now(TZ_AR)


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
# 5) MENSAJES Y REGISTRO
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
            "INSERT INTO conversaciones (numero, creada, ultimo) VALUES (?,?,?) ON CONFLICT (numero) DO NOTHING",
            (numero, t, t),
        )
        con.execute(
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


def conv(numero):
    with tx() as con:
        return con.execute("SELECT * FROM conversaciones WHERE numero=?", (numero,)).fetchone()


def marcar(numero, **campos):
    claves = ", ".join(f"{k}=?" for k in campos)
    with tx() as con:
        con.execute(f"UPDATE conversaciones SET {claves} WHERE numero=?", (*campos.values(), numero))


def evento(tipo, detalle=""):
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
        cur = con.execute("INSERT INTO vistos (wamid, ts) VALUES (?,?) ON CONFLICT (wamid) DO NOTHING", (wamid, time.time()))
        return cur.rowcount == 1


def sumar_fallo(numero):
    with tx() as con:
        con.execute("UPDATE conversaciones SET fallos=fallos+1 WHERE numero=?", (numero,))
        return con.execute("SELECT fallos FROM conversaciones WHERE numero=?", (numero,)).fetchone()["fallos"]


def es_primer_mensaje(numero):
    with tx() as con:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM mensajes WHERE numero=? AND quien!='cliente'", (numero,)
        ).fetchone()["n"]
    return n == 0


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
    with tx() as con:
        fila = con.execute("SELECT id, estado FROM mensajes WHERE wamid=?", (wamid,)).fetchone()
        if fila is None:
            return
        if nuevo != "failed" and ORDEN_ESTADO.get(fila["estado"], 0) >= ORDEN_ESTADO[nuevo]:
            return
        con.execute("UPDATE mensajes SET estado=?, error=? WHERE id=?", (nuevo, error, fila["id"]))


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
        return False, "", "No se pudo conectar"
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


def subir_media(nombre, datos, mime):
    try:
        r = requests.post(
            f"{GRAPH}/{PHONE_NUMBER_ID}/media", headers=cabecera(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (nombre, datos, mime)}, timeout=40,
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
            media_id, error = subir_media(foto, leer_catalogo(ruta), mime)
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
# 7) LÓGICA DEL BOT Y PALABRAS CLAVE
# =====================================================================
PALABRAS_VENDEDOR = ("vendedor", "vendedora", "asesor", "humano", "operador", "persona")
PALABRAS_PRECIO = ("precio", "precios", "cuanto sale", "cuanto cuesta", "cuanto vale", "cuanto esta", "presupuesto", "cotizacion", "costo")
PALABRAS_HORARIOS = ("horario", "horarios", "hora", "abren", "abre", "abierto", "cierran", "cierra", "atienden")
PALABRAS_DIRECCION = ("direccion", "ubicacion", "donde estan", "donde queda", "donde esta", "como llego")
PALABRAS_PAGO = ("medios de pago", "formas de pago", "pagar", "pago", "tarjeta", "efectivo", "transferencia", "mercado pago")
PALABRAS_PRODUCTOS = ("productos", "catalogo", "que venden", "que vende", "que tienen")
PALABRAS_MENU = ("menu", "opciones", "ayuda", "inicio", "info")
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
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "application/pdf": ".pdf",
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
        ext = EXT_POR_MIME.get(mime, ".bin")
        nombre = uuid.uuid4().hex + ext
        guardar_archivo(nombre, d.content, mime)
        return "m/" + nombre
    except Exception as e:
        log.error("No se pudo bajar el archivo: %r", e)
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
        archivo, nombre_archivo, tipo_guardado = "", "", "text"

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
            tipo_guardado = tipo
        elif tipo == "location":
            loc = msg.get("location") or {}
            partes = [loc.get("name") or "", loc.get("address") or "", f"({loc.get('latitude')}, {loc.get('longitude')})"]
            texto = " ".join(p for p in partes if p).strip()
            tipo_guardado = "location"
        else:
            registrar(numero, "cliente", "text", "[Mensaje no compatible]")
            return

        registrar(numero, "cliente", tipo_guardado, texto, archivo=archivo, nombre_archivo=nombre_archivo)
        if perfil:
            marcar(numero, perfil=perfil)

        c = conv(numero)
        if c and c["humano"] == 1:
            return

        primer = (c and c["creada"] == c["ultimo"]) or es_primer_mensaje(numero)
        responder(numero, entrada or texto, primer)


# =====================================================================
# 8) RUTAS FLASK Y WEBHOOKS DE META
# =====================================================================
@app.route("/", methods=["GET"])
def index():
    return f"Ferretería O'Higgins Bot Activo 🚀 - Base: {'Supabase' if USA_PG else 'Temporal'}"


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", "")
    return "Token incorrecto", 403


@app.route("/webhook", methods=["POST"])
def recibir_webhook():
    datos = request.get_json(silent=True) or {}
    for entrada in datos.get("entry", []):
        for cambio in entrada.get("changes", []):
            valor = cambio.get("value", {})
            for st in valor.get("statuses", []):
                actualizar_estado(st)
            contactos = {c["wa_id"]: c.get("profile", {}).get("name", "") for c in valor.get("contacts", [])}
            for msg in valor.get("messages", []):
                procesar_mensaje(msg, contactos.get(str(msg.get("from", "")), ""))
    return "OK", 200


# =====================================================================
# 9) PANEL WEB PARA VENDEDORES
# =====================================================================
def requiere_login(f):
    @functools.wraps(f)
    def decorada(*args, **kwargs):
        if not PANEL_PASSWORD:
            return abort(404)
        if not session.get("logueado"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorada


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PANEL_PASSWORD:
        return abort(404)
    error = ""
    if request.method == "POST":
        if request.form.get("password") == PANEL_PASSWORD:
            session.permanent = True
            session["logueado"] = True
            return redirect("/panel")
        error = "Contraseña incorrecta"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Login - Ferretería O'Higgins</title>
<style>body{{font-family:sans-serif;background:#f4f6f8;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}}
.box{{background:white;padding:30px;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,0.1);width:300px}}
input{{width:100%;padding:10px;margin:10px 0;box-sizing:border-box}}button{{width:100%;padding:10px;background:#25d366;color:white;border:none;border-radius:4px;cursor:pointer}}
.err{{color:red;font-size:14px}}</style></head>
<body><div class="box"><h3>Panel Ferretería O'Higgins</h3>
{f'<p class="err">{error}</p>' if error else ''}
<form method="POST"><input type="password" name="password" placeholder="Contraseña" required><button type="submit">Entrar</button></form>
</div></body></html>"""


@app.route("/panel", methods=["GET"])
@requiere_login
def panel():
    db_estado = "Supabase (PostgreSQL) 🟢" if USA_PG else "Base temporal ⚠️"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Panel Ferretería</title>
<style>
body{{font-family:sans-serif;margin:0;background:#eee}}
header{{background:#075e54;color:white;padding:15px;display:flex;justify-content:space-between;align-items:center}}
.badge{{background:#25d366;padding:5px 10px;border-radius:4px;font-size:12px}}
</style></head>
<body>
<header><h2>Panel de la ferretería O'Higgins</h2><span>{db_estado}</span></header>
<div style="padding:20px;">
  <h3>¡Sistema Operativo!</h3>
  <p>Las conversaciones de WhatsApp y el bot están funcionando con normalidad.</p>
</div>
</body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))