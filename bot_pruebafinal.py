"""
Bot de WhatsApp de la ferretería + panel web para vendedores  (versión 13: fotos y archivos seguidos se contestan una sola vez)

VARIABLES DE ENTORNO (en Render: Environment)
  WHATSAPP_TOKEN   token permanente de Meta                          (obligatoria)
  PHONE_NUMBER_ID  Phone Number ID del número del bot                (obligatoria)
  VERIFY_TOKEN     la clave del webhook (la misma que pusiste en Meta) (obligatoria)
  PANEL_PASSWORD   clave para entrar al panel de vendedores          (obligatoria)
  APP_SECRET       "Clave secreta de la app" de Meta                 (muy recomendada)
                   Meta > tu app > Configuración de la app > Básica
  SECRET_KEY       texto largo al azar para las sesiones             (opcional)
  DATABASE_URL     dirección de Supabase (Session pooler)            (recomendada)
                   Con esto los mensajes y fotos NO se pierden cuando Render se reinicia.
                   Sin esto, usa una base temporal que se borra al reiniciar.
  DATA_DIR         carpeta donde se guardan mensajes y archivos      (opcional)
                   Con disco persistente de Render: /data

  ESPERA_JUNTAR    segundos que espera el bot por si el cliente manda varios mensajes (2.5)
  ESPERA_ARCHIVOS  segundos que espera cuando mandan fotos/archivos seguidos, para contestar una sola vez (4)

AJUSTES (⚙️ en el panel, solo el administrador)
  Horarios, dirección, medios de pago, envíos, saludo, feriados, respuestas rápidas y
  usuarios de los vendedores se cambian desde el panel y se aplican al instante.
  Los valores de abajo (sección 1) son solo los iniciales.

CATÁLOGO (dos formas, el bot lo reconoce solo)
  1) Desde el panel: botón 📦 Catálogo. Subís foto + nombre y ya funciona, sin tocar código.
  2) Carpeta catalogo/ en GitHub: cualquier foto cuyo nombre sea el producto
     (cemento portland.jpg, thinner.jpg). Opcional: un .txt con el mismo nombre = descripción.
"""

import functools
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, abort, jsonify, redirect, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ferreteria")


# =====================================================================
# 1) DATOS DE LA FERRETERÍA  ← esta es la parte que vas a editar
# =====================================================================
NOMBRE_NEGOCIO = "Ferretería O'Higgins"

# Días: 0 = lunes ... 6 = domingo. Horas en formato 24 hs.
TURNOS = [
    ("Lunes a Viernes", (0, 1, 2, 3, 4), "08:30", "19:30"),
    ("Sábados", (5,), "09:00", "13:00"),
]
DIAS_CERRADO = "Domingos y feriados"
FERIADOS = []  # fechas en que no abrís, formato "AAAA-MM-DD". Ej: ["2026-12-08", "2026-12-25"]

# Si los dejás vacíos, el bot deriva esas consultas a un vendedor.
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
    "Por el momento no contamos con envíos a domicilio, ¡pero te esperamos en el local "
    "para retirarlo cuando gustes!"
)

# Productos con info y foto. La foto va en la carpeta "catalogo/" (jpg o png).
# Sin precios a propósito: los precios los pasa siempre una persona.
PRODUCTOS = {}  # los productos se cargan desde el panel (📦 Catálogo) o poniendo fotos en catalogo/

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
    "¡Hola! 👋 Bienvenido a *{nombre}*. Es un gusto saludarte.\n"
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
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
try:
    ESPERA_JUNTAR = float(os.environ.get("ESPERA_JUNTAR", "2.5"))
except ValueError:
    ESPERA_JUNTAR = 2.5
try:
    ESPERA_ARCHIVOS = float(os.environ.get("ESPERA_ARCHIVOS", "4"))  # espera extra cuando mandan fotos/archivos seguidos
except ValueError:
    ESPERA_ARCHIVOS = 4.0
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
# 3) BASE DE DATOS (Supabase/PostgreSQL, o SQLite si no hay DATABASE_URL)
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
        log.error("❌ No pude conectar con DATABASE_URL (%s). Uso una base TEMPORAL que se borra al reiniciar.", e)

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
    id      __ID__,
    ts      DOUBLE PRECISION NOT NULL,
    tipo    TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos (ts);
CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS carrito (
    id       __ID__,
    numero   TEXT NOT NULL,
    clave    TEXT NOT NULL DEFAULT '',
    nombre   TEXT NOT NULL,
    cantidad TEXT NOT NULL DEFAULT '',
    ts       DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_carrito_numero ON carrito (numero);
CREATE TABLE IF NOT EXISTS pedidos (
    id     __ID__,
    numero TEXT NOT NULL,
    items  TEXT NOT NULL,
    ts     DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS usuarios (
    usuario    TEXT PRIMARY KEY,
    nombre     TEXT NOT NULL DEFAULT '',
    clave_hash TEXT NOT NULL DEFAULT '',
    rol        TEXT NOT NULL DEFAULT 'vendedor',
    activo     INTEGER NOT NULL DEFAULT 1,
    creado     DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS productos (
    clave    TEXT PRIMARY KEY,
    nombre   TEXT NOT NULL,
    palabras TEXT NOT NULL DEFAULT '',
    texto    TEXT NOT NULL DEFAULT '',
    foto     TEXT NOT NULL DEFAULT '',
    ts       DOUBLE PRECISION NOT NULL DEFAULT 0
);
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
    """Hace que PostgreSQL se use igual que SQLite: con ? en vez de %s."""

    def __init__(self, cur):
        self.cur = cur

    def execute(self, sql, params=()):
        self.cur.execute(sql.replace("?", "%s"), tuple(params))
        return self.cur


def _conexion_pg():
    """Pide una conexión y comprueba que siga viva (Supabase corta las que están quietas)."""
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
    """Abre la base, confirma los cambios al salir y la cierra siempre."""
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


def _agregar_columna(con, tabla, columna, definicion):
    """Agrega una columna a una tabla que ya existe (para bases creadas con versiones viejas)."""
    if USA_PG:
        con.execute(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS {columna} {definicion}")
    elif not any(r["name"] == columna for r in con.execute(f"PRAGMA table_info({tabla})").fetchall()):
        con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")


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
        _agregar_columna(con, "productos", "fotos", "TEXT NOT NULL DEFAULT ''")   # fotos extra, separadas por coma
        _agregar_columna(con, "productos", "oculto", "INTEGER NOT NULL DEFAULT 0")  # 1 = producto quitado del catálogo
        _agregar_columna(con, "productos", "categoria", "TEXT NOT NULL DEFAULT ''")
        _agregar_columna(con, "productos", "activo", "INTEGER NOT NULL DEFAULT 1")  # 0 = pausado (no lo tenemos)
        _agregar_columna(con, "mensajes", "autor", "TEXT NOT NULL DEFAULT ''")
        _agregar_columna(con, "conversaciones", "atiende", "TEXT NOT NULL DEFAULT ''")
        _agregar_columna(con, "conversaciones", "espera", "TEXT NOT NULL DEFAULT ''")
        con.execute("DELETE FROM vistos WHERE ts < ?", (time.time() - 3 * 86400,))
        con.execute("DELETE FROM carrito WHERE ts < ?", (time.time() - 7 * 86400,))
    log.info("Base de datos lista (%s)", "Supabase" if USA_PG else DB_PATH)


iniciar_db()


# ---- archivos de clientes y fotos enviadas (en Supabase si hay, si no en disco)
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


def borrar_archivo(nombre):
    try:
        if USA_PG:
            with tx() as con:
                con.execute("DELETE FROM archivos WHERE nombre=?", (nombre,))
        else:
            os.remove(os.path.join(MEDIA_DIR, nombre))
    except Exception:
        log.warning("No pude borrar el archivo %s", nombre)


def url_media(archivo):
    origen, _, nombre = archivo.partition("/")
    return f"/media/{origen}/{quote(nombre)}"


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


# ---- AJUSTES editables desde el panel (si no hay nada guardado, valen los de la sección 1)
DIAS_NOMBRES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def _defaults():
    return {
        "nombre": NOMBRE_NEGOCIO,
        "saludo": SALUDO,
        "direccion": DIRECCION,
        "medios_pago": MEDIOS_DE_PAGO,
        "envios": TEXTO_ENVIO,
        "turnos": [{"nombre": n, "dias": list(d), "desde": a, "hasta": b} for n, d, a, b in TURNOS],
        "cerrado": DIAS_CERRADO,
        "feriados": list(FERIADOS),
        "fuera_horario": AVISO_FUERA_DE_HORARIO,
        "rapidas": list(RESPUESTAS_RAPIDAS),
        "link_resena": "",
    }


_aj_cache = [0.0, None]


def ajustes(forzar=False):
    if forzar or _aj_cache[1] is None or time.time() - _aj_cache[0] > 5:
        datos = _defaults()
        try:
            with tx() as con:
                for r in con.execute("SELECT clave, valor FROM ajustes").fetchall():
                    if r["clave"] in datos:
                        try:
                            datos[r["clave"]] = json.loads(r["valor"])
                        except ValueError:
                            pass
        except Exception:
            log.exception("No pude leer los ajustes: uso los valores del código")
        _aj_cache[0], _aj_cache[1] = time.time(), datos
    return _aj_cache[1]


def texto_resena():
    """Mensaje rápido para pedir la calificación (el link se carga en ⚙️ Ajustes)."""
    a = ajustes()
    t = f"¡Gracias por tu compra en {a['nombre']}! 🙌 Si quedaste conforme, ¿nos dejás tu calificación? ⭐⭐⭐⭐⭐ Nos ayuda muchísimo."
    link = (a.get("link_resena") or "").strip()
    return t + (f"\n{link}" if link else "")


def saludo_texto():
    a = ajustes()
    return a["saludo"].replace("{nombre}", a["nombre"])


def _hora_ok(h):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h or ""))


def _fecha_ok(txt):
    """Acepta 25/12/2026 o 2026-12-25 y devuelve 2026-12-25 (o None si no es una fecha)."""
    txt = txt.strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(txt, formato).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def validar_ajustes(d):
    """Revisa lo que mandó el panel. Devuelve (datos_limpios, error)."""
    if not isinstance(d, dict):
        return None, "Datos inválidos."
    out = {}
    campos = {
        "nombre": (60, "El nombre del local", True),
        "saludo": (900, "El saludo", True),
        "direccion": (900, "La dirección", False),
        "medios_pago": (900, "Los medios de pago", False),
        "envios": (900, "El texto de envíos", False),
        "cerrado": (60, "Los días cerrados", False),
        "fuera_horario": (300, "El aviso fuera de horario", False),
        "link_resena": (300, "El link de calificación", False),
    }
    for clave, (maximo, etiqueta, obligatorio) in campos.items():
        if clave not in d:
            continue
        v = str(d[clave]).strip()
        if obligatorio and not v:
            return None, f"{etiqueta} no puede quedar vacío."
        if len(v) > maximo:
            return None, f"{etiqueta} es demasiado largo (máximo {maximo} letras)."
        out[clave] = v
    if "turnos" in d:
        ts = d["turnos"]
        if not isinstance(ts, list) or len(ts) > 8:
            return None, "Los horarios no son válidos."
        lista = []
        for t in ts:
            if not isinstance(t, dict):
                return None, "Los horarios no son válidos."
            nombre = str(t.get("nombre", "")).strip()[:40]
            dias = sorted({x for x in (t.get("dias") or []) if isinstance(x, int) and 0 <= x <= 6})
            desde, hasta = str(t.get("desde", "")), str(t.get("hasta", ""))
            if not nombre:
                return None, "Cada horario necesita un nombre (ej: Lunes a Viernes)."
            if not dias:
                return None, f"En el horario «{nombre}» elegí al menos un día."
            if not (_hora_ok(desde) and _hora_ok(hasta)):
                return None, f"En el horario «{nombre}» las horas no son válidas."
            if desde >= hasta:
                return None, f"En el horario «{nombre}» la hora de apertura tiene que ser anterior al cierre."
            lista.append({"nombre": nombre, "dias": dias, "desde": desde, "hasta": hasta})
        out["turnos"] = lista
    if "feriados" in d:
        crudo = d["feriados"]
        lineas = crudo.splitlines() if isinstance(crudo, str) else list(crudo or [])
        fechas = []
        for linea in lineas:
            linea = str(linea).strip()
            if not linea:
                continue
            f = _fecha_ok(linea)
            if f is None:
                return None, f"No entendí la fecha «{linea[:20]}». Escribila así: 25/12/2026."
            if f not in fechas:
                fechas.append(f)
        if len(fechas) > 100:
            return None, "Hay demasiados feriados cargados."
        out["feriados"] = sorted(fechas)
    if "rapidas" in d:
        crudo = d["rapidas"]
        lineas = crudo.splitlines() if isinstance(crudo, str) else list(crudo or [])
        frases = [str(x).strip() for x in lineas if str(x).strip()]
        if len(frases) > 12 or any(len(x) > 200 for x in frases):
            return None, "Las respuestas rápidas: máximo 12 frases de hasta 200 letras."
        out["rapidas"] = frases
    return out, ""


def guardar_ajustes(datos):
    with tx() as con:
        for clave, valor in datos.items():
            js = json.dumps(valor, ensure_ascii=False)
            con.execute(
                "INSERT INTO ajustes (clave, valor) VALUES (?,?) ON CONFLICT (clave) DO UPDATE SET valor=?",
                (clave, js, js),
            )
    ajustes(True)


def abierto_ahora():
    a = ajustes()
    t = ahora()
    if t.strftime("%Y-%m-%d") in a["feriados"]:
        return False
    hm = t.strftime("%H:%M")
    return any(t.weekday() in x["dias"] and x["desde"] <= hm < x["hasta"] for x in a["turnos"])


def texto_horarios(con_pregunta=False):
    a = ajustes()
    lineas = ["🕒 *¡Te esperamos en nuestro local!* 🛠️", ""]
    for x in a["turnos"]:
        lineas.append(f"📅 *{x['nombre']}:* {x['desde']} a {x['hasta']} hs")
    if a["cerrado"]:
        lineas.append(f"❌ *{a['cerrado']}:* Cerrado")
    lineas.append("")
    lineas.append("✅ ¡Ahora estamos abiertos!" if abierto_ahora() else "🌙 Ahora estamos cerrados.")
    if con_pregunta:
        lineas += ["", "¿Necesitás consultar por alguna herramienta o material en particular?"]
    return "\n".join(lineas)


# ---- CATÁLOGO: productos del código + fotos de la carpeta catalogo/ + productos cargados en el panel
EXT_FOTO = (".jpg", ".jpeg", ".png")
MAX_FOTOS_PRODUCTO = 6  # fotos por producto (la principal + 5)
PALABRAS_VACIAS = {"de", "del", "la", "el", "los", "las", "para", "con", "y", "en", "un", "una"}
_cat_cache = [0.0, []]


def raiz(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def formas(t):
    """Todas las formas (singular/plural) de las palabras del mensaje."""
    out = set()
    for w in t.split():
        out.add(w)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])
        if len(w) > 4 and w.endswith("es"):
            out.add(w[:-2])
    return out


def palabras_clave(termino):
    return [raiz(w) for w in normalizar(termino).split() if len(w) > 1 and w not in PALABRAS_VACIAS]


def _titulo(nombre):
    return nombre[:1].upper() + nombre[1:]


def _catalogo_base():
    """Productos que vienen de la carpeta catalogo/ y de PRODUCTOS (no editables desde el panel)."""
    cat = {}
    # 1) fotos sueltas en la carpeta catalogo/
    try:
        archivos = sorted(os.listdir(CATALOGO_DIR))
    except OSError:
        archivos = []
    for f in archivos:
        base, ext = os.path.splitext(f)
        if ext.lower() not in EXT_FOTO:
            continue
        nombre = re.sub(r"[_\-]+", " ", base).strip()
        clave = normalizar(nombre)
        if not clave:
            continue
        texto = ""
        try:
            with open(os.path.join(CATALOGO_DIR, base + ".txt"), encoding="utf-8") as fh:
                texto = fh.read().strip()[:900]
        except OSError:
            pass
        cat[clave] = {"clave": clave, "nombre": _titulo(nombre), "terms": [nombre], "texto": texto,
                      "origen": "c", "foto": f, "editable": False, "fuente": "carpeta"}
    # 2) productos escritos en este archivo (PRODUCTOS)
    for p in PRODUCTOS.values():
        clave = normalizar(p["nombre"])
        previo = cat.get(clave, {})
        foto = p.get("foto") or ""
        if not (foto and os.path.isfile(os.path.join(CATALOGO_DIR, foto))):
            foto = previo.get("foto", "")
        cat[clave] = {"clave": clave, "nombre": p["nombre"], "terms": [p["nombre"], *p.get("palabras", ())],
                      "texto": p.get("texto", ""), "origen": "c", "foto": foto, "editable": False,
                      "fuente": "código"}
    return cat


def _catalogo_armar():
    """Todos los productos (activos y pausados). El bot usa catalogo_actual(), que solo trae los activos."""
    cat = _catalogo_base()
    for p in cat.values():
        p.setdefault("categoria", "")
        p.setdefault("activo", True)
    # 3) productos cargados desde el panel (los más nuevos mandan). oculto=1: el vendedor lo quitó.
    #    Si el producto también viene de GitHub, lo del panel (categoría, pausa, texto) se suma a lo de GitHub.
    try:
        with tx() as con:
            filas = [dict(r) for r in con.execute(
                "SELECT clave, nombre, palabras, texto, foto, fotos, oculto, categoria, activo FROM productos").fetchall()]
        for r in filas:
            if r["oculto"]:
                cat.pop(r["clave"], None)
                continue
            base = cat.get(r["clave"])
            extras = [x.strip() for x in (r["palabras"] or "").split(",") if x.strip()]
            terms = [r["nombre"], *extras]
            if base:
                terms += [t for t in base["terms"] if normalizar(t) not in {normalizar(x) for x in terms}]
            propia = bool(r["foto"])
            cat[r["clave"]] = {
                "clave": r["clave"], "nombre": r["nombre"], "terms": terms,
                "texto": r["texto"] or (base["texto"] if base else ""),
                "origen": "m" if (propia or not base) else base["origen"],
                "foto": r["foto"] if (propia or not base) else base["foto"],
                "editable": True, "fuente": "panel" if not base else base["fuente"] + " + panel",
                "extras": [x for x in (r["fotos"] or "").split(",") if x],
                "categoria": (r["categoria"] or "").strip(), "activo": bool(r["activo"]),
                "foto_propia": propia,
            }
    except Exception:
        log.exception("No pude leer los productos del panel")
    return sorted(cat.values(), key=lambda p: p["nombre"].lower())


def catalogo_todo(forzar=False):
    """Todos los productos, también los pausados (lo usa el panel)."""
    if forzar or time.time() - _cat_cache[0] > 5:
        _cat_cache[1] = _catalogo_armar()
        _cat_cache[0] = time.time()
    return _cat_cache[1]


def catalogo_actual(forzar=False):
    """Solo los productos activos: lo que el bot ofrece a los clientes."""
    return [p for p in catalogo_todo(forzar) if p["activo"]]


def categorias_activas():
    """[(clave, título)] de las categorías con productos activos; los sin categoría van a 'Otros productos'."""
    vistas, hay_otros = {}, False
    for p in catalogo_actual():
        c = p.get("categoria") or ""
        if c:
            vistas.setdefault(normalizar(c), c)
        else:
            hay_otros = True
    lista = sorted(vistas.items(), key=lambda kv: kv[1].lower())
    if hay_otros and lista:
        lista.append(("_otros", "Otros productos"))
    return lista


def productos_de_categoria(clave):
    return [p for p in catalogo_actual()
            if (normalizar(p.get("categoria") or "") or "_otros") == clave]


def productos_en(t, pausados=False):
    """Productos del catálogo nombrados en el mensaje (ya normalizado). pausados=True busca solo entre los pausados."""
    f = formas(t)
    hallados = []
    for p in (catalogo_todo() if pausados else catalogo_actual()):
        if pausados and p["activo"]:
            continue
        mejor = None
        for term in p["terms"]:
            ws = palabras_clave(term)
            if ws and all(w in f for w in ws) and (mejor is None or len(ws) > len(mejor)):
                mejor = ws
        if mejor:
            hallados.append((p, set(mejor)))
    # si coincide "pintura latex", no mostramos también "pintura"
    return [p for p, ws in hallados if not any(ws < otro for _, otro in hallados)]


def productos_parciales(t):
    """Productos de los que el cliente nombró SOLO UNA PARTE (ej: 'cemento' para 'Cemento portland')."""
    f = formas(t)
    out = []
    for p in catalogo_actual():
        claves = {w for term in p["terms"] for w in palabras_clave(term) if len(w) >= 4}
        if claves & f:
            out.append(p)
    return out


def texto_productos():
    cat = catalogo_actual()
    if not cat:
        return "Para consultar por productos escribí *VENDEDOR*."
    if len(categorias_activas()) >= 2:
        partes = []
        for clave, titulo_c in categorias_activas():
            partes.append(f"*{titulo_c}*\n" + "\n".join(f"• {p['nombre']}" for p in productos_de_categoria(clave)[:15]))
        lista = "\n\n".join(partes)
    else:
        lista = "\n".join(f"• {p['nombre']}" for p in cat[:30])
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
              wamid="", estado="", error="", autor=""):
    t = time.time()
    with tx() as con:
        con.execute(
            "INSERT INTO conversaciones (numero, creada, ultimo) VALUES (?,?,?) ON CONFLICT (numero) DO NOTHING",
            (numero, t, t),
        )
        con.execute(
            "INSERT INTO mensajes (numero, quien, tipo, texto, archivo, nombre_archivo, ts, wamid, estado, error, autor) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (numero, quien, tipo, texto, archivo, nombre_archivo, t, wamid, estado, error, autor),
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
        cur = con.execute("INSERT INTO vistos (wamid, ts) VALUES (?,?) ON CONFLICT (wamid) DO NOTHING", (wamid, time.time()))
        return cur.rowcount == 1


def es_primer_mensaje(numero):
    with tx() as con:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM mensajes WHERE numero=? AND quien!='cliente'", (numero,)
        ).fetchone()["n"]
    return n == 0


def sumar_fallo(numero):
    with tx() as con:
        con.execute("UPDATE conversaciones SET fallos=fallos+1 WHERE numero=?", (numero,))
        return con.execute("SELECT fallos FROM conversaciones WHERE numero=?", (numero,)).fetchone()["fallos"]


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


BTN_MENU = [("horarios", "🕒 Horarios"), ("productos", "🧱 Productos"), ("vendedor", "👤 Vendedor")]
BTN_AGREGADO = [("productos", "➕ Seguir agregando"), ("carrito", "🛒 Ver pedido"), ("pedir", "📋 Pedir presupuesto")]
BTN_PEDIDO = [("pedir", "📋 Pedir presupuesto"), ("productos", "➕ Seguir agregando"), ("vaciar", "🗑 Vaciar pedido")]
BTN_ARMAR = [("productos", "🧱 Ver productos"), ("otro", "✍️ Escribir pedido"), ("vendedor", "👤 Vendedor")]


def _payload_botones(numero, cuerpo, botones, imagen_id=None):
    inter = {
        "type": "button",
        "body": {"text": cuerpo[:1024]},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": i[:200], "title": t[:20]}} for i, t in botones[:3]
        ]},
    }
    if imagen_id:
        inter["header"] = {"type": "image", "image": {"id": imagen_id}}
    return {"messaging_product": "whatsapp", "to": a_destino(numero), "type": "interactive", "interactive": inter}


def enviar_botones(numero, cuerpo, botones=None, imagen_id=None):
    return meta_post(_payload_botones(numero, cuerpo, botones or BTN_MENU, imagen_id))


def enviar_lista(numero, cuerpo, boton, filas):
    """Lista desplegable de WhatsApp (hasta 10 filas)."""
    return meta_post({
        "messaging_product": "whatsapp", "to": a_destino(numero), "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": cuerpo[:1024]},
            "action": {"button": boton[:20], "sections": [
                {"title": "Productos", "rows": [{"id": f["id"][:200], "title": f["title"][:24]} for f in filas[:10]]}
            ]},
        },
    })


def subir_media(nombre, datos, mime):
    """Sube un archivo a Meta. Devuelve (id_del_archivo, error)."""
    try:
        r = requests.post(
            f"{GRAPH}/{PHONE_NUMBER_ID}/media", headers=cabecera(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (nombre, datos, mime)}, timeout=40,
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


MEDIA_IDS = {}  # fotos ya subidas a Meta: clave -> (id, hora). Meta las guarda ~30 días.


def _bytes_foto(origen, foto):
    if origen == "m":
        return leer_archivo(foto), mime_por_extension(foto)
    ruta = os.path.join(CATALOGO_DIR, foto)
    if not os.path.isfile(ruta):
        return None, ""
    return leer_catalogo(ruta), mime_por_extension(ruta)


def enviar_foto(numero, origen, foto, texto, botones=None):
    """Foto con texto. Si se pasan botones, va como tarjeta: foto arriba, texto y botones abajo."""
    datos, mime = _bytes_foto(origen, foto)
    if not datos or not mime:
        return False, "", "no hay foto"
    clave = f"{origen}/{foto}/{len(datos)}"  # si cambian la foto, cambia el tamaño y se vuelve a subir
    error = ""
    for intento in (1, 2):
        guardado = MEDIA_IDS.get(clave)
        if guardado and intento == 1 and time.time() - guardado[1] < 20 * 86400:
            media_id = guardado[0]
        else:
            media_id, error = subir_media(foto, datos, mime)
            if not media_id:
                return False, "", error
            MEDIA_IDS[clave] = (media_id, time.time())
        if botones:
            ok, wamid, error = meta_post(_payload_botones(numero, texto, botones, media_id))
        else:
            ok, wamid, error = meta_post({
                "messaging_product": "whatsapp", "to": a_destino(numero), "type": "image",
                "image": {"id": media_id, "caption": texto[:1024]},
            })
        if ok:
            return True, wamid, ""
        MEDIA_IDS.pop(clave, None)  # por si Meta ya la borró: la volvemos a subir
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


def decir_botones(numero, cuerpo, botones, alternativa=""):
    """Mensaje con botones. Si Meta no los acepta, manda el texto solo."""
    ok, wamid, error = enviar_botones(numero, cuerpo, botones)
    if ok:
        registrar(numero, "bot", "text", cuerpo + "\n🔘 " + " · ".join(t for _, t in botones),
                  wamid=wamid, estado="sent")
        return True
    log.warning("Los botones no funcionaron (%s): mando solo el texto", error)
    return decir(numero, cuerpo + (("\n\n" + alternativa) if alternativa else ""))


def botones_producto(p):
    return [("add:" + p["clave"], "➕ Agregar al pedido"), ("productos", "🧱 Ver productos"), ("vendedor", "👤 Vendedor")]


def enviar_fotos_extra(numero, p):
    """Las otras fotos del producto (si el vendedor cargó más de una), una detrás de otra."""
    for f in (p.get("extras") or [])[:MAX_FOTOS_PRODUCTO - 1]:
        ok, wamid, error = enviar_foto(numero, "m", f, "")
        if ok:
            registrar(numero, "bot", "image", "", archivo=f"m/{f}", wamid=wamid, estado="sent")
        else:
            log.warning("No pude mandar la foto extra %s (%s)", f, error)


def enviar_producto(numero, p):
    """Foto + descripción + botón para sumarlo al pedido (y las demás fotos del producto)."""
    enviar_producto_base(numero, p)
    if p.get("foto") and p.get("extras"):
        enviar_fotos_extra(numero, p)


def enviar_producto_base(numero, p):
    texto = p["texto"] or (
        f"🧰 *{p['nombre']}*\n\nPara consultar stock, medidas o precio, escribí *VENDEDOR*."
    )
    botones = botones_producto(p)
    alternativa = "Para armar tu pedido escribí *PEDIDO*."
    if p["foto"]:
        ok, wamid, error = enviar_foto(numero, p["origen"], p["foto"], texto, botones)
        if ok:
            registrar(numero, "bot", "image", texto + "\n🔘 " + " · ".join(t for _, t in botones),
                      archivo=f"{p['origen']}/{p['foto']}", wamid=wamid, estado="sent")
            return
        log.warning("La tarjeta con foto de %s falló (%s): pruebo con foto simple", p["foto"], error)
        ok, wamid, error = enviar_foto(numero, p["origen"], p["foto"], texto)
        if ok:
            registrar(numero, "bot", "image", texto, archivo=f"{p['origen']}/{p['foto']}",
                      wamid=wamid, estado="sent")
            decir_botones(numero, "¿Lo sumamos a tu pedido?", botones, alternativa)
            return
        log.warning("No pude mandar la foto %s (%s): mando solo el texto", p["foto"], error)
    decir_botones(numero, texto, botones, alternativa)


# =====================================================================
# 7) LÓGICA DEL BOT
# =====================================================================
PALABRAS_VENDEDOR = (
    "vendedor", "vendedora", "asesor", "asesora", "humano", "operador", "operadora", "persona",
    "hablar con alguien", "atencion personalizada",
)
PALABRAS_PRECIO = (
    "precio", "precios", "cuanto sale", "cuanto salen", "cuanto cuesta", "cuanto cuestan",
    "cuanto vale", "cuanto esta", "cuanto me sale", "costo",
)
PALABRAS_HORARIOS = ("horario", "horarios", "hora", "abren", "abre", "abierto", "abiertos",
                     "cierran", "cierra", "atienden")
PALABRAS_DIRECCION = ("direccion", "ubicacion", "donde estan", "donde queda", "donde quedan",
                      "donde esta", "como llego")
PALABRAS_ENVIO = ("envio", "envios", "delivery", "domicilio", "entregan", "mandan", "reparto")
PALABRAS_PAGO = ("medios de pago", "formas de pago", "pagar", "pago", "tarjeta", "tarjetas",
                 "efectivo", "transferencia", "cuotas", "mercado pago", "debito", "credito")
PALABRAS_PRODUCTOS = ("productos", "catalogo", "que venden", "que vende", "que tienen")
PALABRAS_MENU = ("menu", "opciones", "ayuda", "inicio", "info", "informacion")
PALABRAS_SALUDO = ("hola", "buenas", "buen dia", "buenos dias", "buenas tardes", "buenas noches", "hey")
PALABRAS_GRACIAS = ("gracias", "muchas gracias", "mil gracias")
# Mensajes de despedida: el bot NO contesta (ni "gracias", ni "igualmente", ni "ok", ni solo emojis).
CIERRE_FUERTES = {"gracias", "gracia", "ok", "oka", "okey", "okay", "okey", "dale", "listo", "perfecto",
                  "genial", "excelente", "buenisimo", "barbaro", "joya", "igualmente", "igual", "chau",
                  "chao", "adios", "saludos", "abrazo", "abrazos", "vale", "entendido", "gracais", "grax",
                  "gracias", "hablamos", "cuidate", "cuidense", "bueno", "bien", "ya", "tranqui", "tranquilo"}
CIERRE_RELLENO = {"muchas", "mil", "muchisimas", "muy", "buen", "buena", "buenas", "dia", "dias", "tarde",
                  "tardes", "noche", "noches", "finde", "semana", "tambien", "igual", "de", "nada", "por",
                  "todo", "la", "el", "lo", "te", "les", "se", "a", "vos", "ustedes", "hasta", "luego",
                  "manana", "nos", "vemos", "que", "descanses", "disfrutes", "estes", "esten", "bien",
                  "pasen", "pases", "lindo", "linda", "ahi", "estamos", "gracias", "y", "e", "un", "gran",
                  "saludo", "cordial", "cordiales", "atencion", "ayuda", "rapido", "rapida", "respuesta",
                  "info", "informacion", "entonces", "mas", "ahora", "despues", "paso", "pasamos", "voy",
                  "veo", "vemos", "avisamos", "aviso", "aviso", "ahi", "voy", "me", "mi"}


def es_cierre(texto):
    """True si el mensaje es solo una despedida/agradecimiento ('Ok, gracias', 'Igualmente', '👍').
    'Buenas tardes' solo NO es cierre (es un saludo); 'gracias, tienen cemento?' tampoco."""
    t = normalizar(texto)
    if not t:  # solo emojis o signos
        return True
    palabras = t.split()
    if len(palabras) > 8:
        return False
    if not all(w in CIERRE_FUERTES or w in CIERRE_RELLENO for w in palabras):
        return False
    return any(w in CIERRE_FUERTES for w in palabras)
BOTONES = {"horarios": "horarios", "productos": "productos", "vendedor": "vendedor"}
PALABRAS_PRESUPUESTO = ("presupuesto", "presupuestos", "cotizacion", "cotizar")
PALABRAS_PEDIDO = ("mi pedido", "ver pedido", "pedido", "carrito", "mi carrito")
PALABRAS_CANCELAR = ("no", "nada", "cancelar", "cancelo", "cancela", "olvidalo", "dejalo", "salir")
NUMEROS = {"un", "uno", "una", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
           "once", "doce", "quince", "veinte", "treinta", "cuarenta", "cincuenta", "cien", "medio", "media",
           "docena", "varios", "varias"}
UNIDADES = {"bolsa", "caja", "metro", "kilo", "litro", "unidad", "rollo", "par", "bidon", "lata", "m", "kg", "lt", "mt"}
MAX_ITEMS_PEDIDO = 30
ESPERA_VENCE = 20 * 60  # si el cliente no contesta la cantidad en 20 minutos, se olvida


# ---- pedido (carrito) del cliente: no tiene precios; el presupuesto lo arma una persona
def tiene_cantidad(t):
    return bool(re.search(r"\d", t)) or any(w in NUMEROS or raiz(w) in UNIDADES for w in t.split())


def carrito_items(numero):
    with tx() as con:
        return [dict(r) for r in con.execute(
            "SELECT clave, nombre, cantidad FROM carrito WHERE numero=? ORDER BY id", (numero,)).fetchall()]


def carrito_agregar(numero, clave, nombre, cantidad):
    with tx() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM carrito WHERE numero=?", (numero,)).fetchone()["n"]
        if n >= MAX_ITEMS_PEDIDO:
            return False
        con.execute("INSERT INTO carrito (numero, clave, nombre, cantidad, ts) VALUES (?,?,?,?,?)",
                    (numero, clave, nombre[:80], cantidad[:40], time.time()))
    return True


def carrito_vaciar(numero):
    with tx() as con:
        con.execute("DELETE FROM carrito WHERE numero=?", (numero,))


def texto_items(items):
    return "\n".join("• " + i["nombre"] + (f" — {i['cantidad']}" if i["cantidad"] else "") for i in items)


def buscar_producto(clave):
    return next((p for p in catalogo_actual() if p["clave"] == clave), None)


def poner_espera(numero, tipo):
    marcar(numero, espera=f"{tipo}@{int(time.time())}")


def espera_vigente(c):
    """Lo que el bot está esperando del cliente ('cant:clave' o 'libre'), o '' si no hay o venció."""
    crudo = (c["espera"] if c else "") or ""
    if "@" not in crudo:
        return ""
    tipo, _, ts = crudo.rpartition("@")
    try:
        return tipo if time.time() - int(ts) < ESPERA_VENCE else ""
    except ValueError:
        return ""


def mostrar_categorias(numero):
    """Primer menú de 'Ver productos' cuando hay 2 o más categorías: el cliente elige el rubro."""
    cats = categorias_activas()[:9]
    filas = [{"id": "cat:" + c, "title": t[:24]} for c, t in cats]
    filas.append({"id": "otro", "title": "✍️ Otro producto"})
    ok, wamid, error = enviar_lista(numero, "🧰 ¿Qué estás buscando? Elegí una categoría 👇", "Ver categorías", filas)
    if ok:
        registrar(numero, "bot", "text", "🧰 Lista de categorías: " + " · ".join(t for _, t in cats),
                  wamid=wamid, estado="sent")
        return True
    log.warning("La lista de categorías no funcionó (%s): mando los nombres escritos", error)
    return decir(numero, texto_productos())


def mostrar_productos(numero, pagina=0, categoria=""):
    """Lista desplegable: el cliente toca un producto y recibe su foto y detalle.
    Si hay varias categorías, primero elige la categoría (categoria='' = menú de categorías)."""
    if not categoria and len(categorias_activas()) >= 2:
        return mostrar_categorias(numero)
    cat = productos_de_categoria(categoria) if categoria else catalogo_actual()
    if not cat:
        return decir(numero, "Para consultar por productos escribí *VENDEDOR*.")
    con_cats = bool(categoria)
    sufijo = ":" + categoria if categoria else ""
    tope = 9 - (1 if con_cats else 0)          # filas libres para productos (el resto: otro, categorías)
    por_pagina = tope - 2                       # si hay que paginar: dejamos lugar para anterior/siguiente
    if len(cat) <= tope:
        visibles, pagina, hay_mas = cat, 0, False
    else:
        ultima = (len(cat) - 1) // por_pagina
        pagina = max(0, min(pagina, ultima))
        visibles = cat[pagina * por_pagina:(pagina + 1) * por_pagina]
        hay_mas = pagina < ultima
    filas = [{"id": "prod:" + p["clave"], "title": p["nombre"]} for p in visibles]
    if pagina > 0:
        filas.append({"id": f"pag:{pagina - 1}{sufijo}", "title": "⬅️ Anteriores"})
    if hay_mas:
        filas.append({"id": f"pag:{pagina + 1}{sufijo}", "title": "➡️ Ver más"})
    if con_cats:
        filas.append({"id": "cats", "title": "↩️ Categorías"})
    filas.append({"id": "otro", "title": "✍️ Otro producto"})
    titulo_c = dict(categorias_activas()).get(categoria, "")
    intro = f"🧰 *{titulo_c}*: elegí un producto para ver su foto y detalles 👇" if titulo_c else \
        "🧰 Elegí un producto para ver su foto y detalles 👇"
    ok, wamid, error = enviar_lista(numero, intro, "Ver productos", filas)
    if ok:
        registrar(numero, "bot", "text", f"🧰 Lista de productos ({len(cat)})\n🔘 Toca un producto para ver su foto",
                  wamid=wamid, estado="sent")
        return True
    log.warning("La lista no funcionó (%s): mando los nombres escritos", error)
    return decir(numero, texto_productos())


def mostrar_carrito(numero):
    items = carrito_items(numero)
    if not items:
        return decir_botones(numero, "🛒 Tu pedido está vacío todavía. Elegí productos o escribí lo que necesitás 👇",
                             BTN_ARMAR, "Escribí *PRODUCTOS* para ver el catálogo.")
    return decir_botones(
        numero,
        "🛒 *Tu pedido:*\n" + texto_items(items) +
        "\n\nCuando esté listo, pedí el presupuesto y un vendedor te pasa los precios.",
        BTN_PEDIDO, "Escribí *PRESUPUESTO* para pedirlo o *PRODUCTOS* para seguir agregando.")


def ofrecer_armar_pedido(numero):
    return decir_botones(
        numero,
        "📋 ¡Claro! Para el presupuesto armá tu pedido: elegí productos del catálogo o escribí lo que "
        "necesitás. Un vendedor te pasa los precios.",
        BTN_ARMAR, "Escribí *PRODUCTOS* para ver el catálogo o *VENDEDOR* para hablar con una persona.")


def pedir_presupuesto(numero):
    items = carrito_items(numero)
    if not items:
        return mostrar_carrito(numero)
    resumen = texto_items(items)
    with tx() as con:
        con.execute("INSERT INTO pedidos (numero, items, ts) VALUES (?,?,?)",
                    (numero, json.dumps(items, ensure_ascii=False), time.time()))
    carrito_vaciar(numero)
    marcar(numero, espera="")
    derivar(numero, "presupuesto",
            "✅ ¡Listo! Este es tu pedido:\n" + resumen +
            "\n\n🔔 Se lo pasé a un vendedor, que te prepara el presupuesto con los precios actualizados "
            "y te lo envía por acá.")


def manejar_accion(numero, bid):
    """Toques en la lista de productos y en los botones del pedido. Devuelve True si era uno de los nuestros."""
    if bid.startswith("prod:"):
        p = buscar_producto(bid[5:])
        if p is None:
            decir(numero, "Ese producto ya no está disponible.")
            return mostrar_productos(numero) or True
        evento("producto", p["clave"])
        enviar_producto(numero, p)
    elif bid.startswith("add:"):
        p = buscar_producto(bid[4:])
        if p is None:
            decir(numero, "Ese producto ya no está disponible.")
            return mostrar_productos(numero) or True
        poner_espera(numero, "cant:" + p["clave"])
        decir(numero, f"¿Cuánto querés de *{p['nombre']}*? Escribí la cantidad (ej: 2 bolsas, 10 unidades).\n"
                      "Para cancelar escribí *CANCELAR*.")
    elif bid.startswith("cat:"):
        mostrar_productos(numero, 0, bid[4:])
    elif bid == "cats":
        mostrar_categorias(numero)
    elif bid.startswith("pag:"):
        num, _, cat_clave = bid[4:].partition(":")
        try:
            mostrar_productos(numero, int(num), cat_clave)
        except ValueError:
            mostrar_productos(numero)
    elif bid == "otro":
        poner_espera(numero, "libre")
        decir(numero, "✍️ Escribime qué producto necesitás y la cantidad (ej: 10 metros de cable).\n"
                      "Para cancelar escribí *CANCELAR*.")
    elif bid == "carrito":
        mostrar_carrito(numero)
    elif bid == "vaciar":
        carrito_vaciar(numero)
        decir_botones(numero, "🗑 Listo, vaciaste tu pedido.", BTN_MENU, "Escribí *PRODUCTOS* si querés empezar de nuevo.")
    elif bid == "pedir":
        pedir_presupuesto(numero)
    else:
        return False
    return True


def manejar_espera(numero, texto, espera):
    """El cliente estaba por decirnos una cantidad (o un producto que no está en el catálogo).
    Devuelve True si este mensaje era esa respuesta; False si hay que atenderlo normalmente."""
    t = normalizar(texto)
    if es_cierre(texto):  # "ok gracias": no es un producto ni una cantidad; se cierra en silencio
        marcar(numero, espera="", fallos=0)
        evento("cierre")
        return True
    if not t or tiene(t, PALABRAS_VENDEDOR):
        marcar(numero, espera="")
        return False
    if t in PALABRAS_CANCELAR:
        marcar(numero, espera="")
        decir_menu(numero, "Sin problema 👍")
        return True
    if espera.startswith("cant:"):
        if not tiene_cantidad(t):
            marcar(numero, espera="")  # no era una cantidad: lo atendemos como cualquier mensaje
            return False
        clave = espera[5:]
        p = buscar_producto(clave)
        nombre, cantidad = (p["nombre"] if p else clave), texto.strip()[:40]
    else:  # "libre": un producto que todavía no está cargado en el catálogo
        clave, nombre, cantidad = "", texto.strip()[:80], ""
    marcar(numero, espera="")
    if not carrito_agregar(numero, clave, nombre, cantidad):
        decir(numero, "Tu pedido ya tiene muchos productos. Pedí el presupuesto y después seguimos 🙂")
        mostrar_carrito(numero)
        return True
    evento("pedido_item", clave or "libre")
    decir_botones(numero, f"✅ Agregué *{nombre}*" + (f" — {cantidad}" if cantidad else "") + " a tu pedido.",
                  BTN_AGREGADO, "Escribí *PEDIDO* para ver tu pedido o *PRESUPUESTO* para pedirlo.")
    return True


def esperando_vendedor(c):
    """Derivada pero todavía ningún vendedor la aceptó ni respondió: el bot sigue ayudando con lo básico."""
    return bool(c and c["humano"] and c["pide_vendedor"] and not c["atiende"])


def bot_callado(c):
    """True cuando ya la atiende una persona (la aceptó o respondió): el bot no dice nada."""
    return bool(c and c["humano"] and not esperando_vendedor(c))


_aviso_espera = {}  # numero -> hora del último "Ya avisamos"


def avisar_espera(numero):
    _aviso_espera[numero] = time.time()
    decir_botones(numero, "🔔 Ya avisamos a un vendedor: te responde en un momento por acá. "
                          "Mientras tanto, si querés, mirá horarios o productos 👇",
                  [("horarios", "🕒 Horarios"), ("productos", "🧱 Productos")],
                  "Mientras esperás podés escribir *HORARIOS* o *PRODUCTOS*.")


def derivar(numero, motivo, mensaje):
    """Pasa la conversación a una persona y el panel marca el 🔔. Mientras ningún vendedor la acepte,
    el bot sigue contestando horarios, productos, etc. (no vuelve a derivar ni dice 'no entendí')."""
    if esperando_vendedor(conv(numero)):
        evento("vendedor_repetido", motivo)
        if motivo == "archivo" and time.time() - _aviso_espera.get(numero, 0) < 120:
            return  # ya le dijimos "ya avisamos" hace un momento: no repetimos por cada foto
        return avisar_espera(numero)
    evento("vendedor", motivo)
    marcar(numero, humano=1, pide_vendedor=1, pide_desde=time.time(), fallos=0, atiende="")
    if not abierto_ahora():
        mensaje += "\n\n" + ajustes()["fuera_horario"]
    _aviso_espera[numero] = time.time()  # recién le avisamos: no repetirlo por cada foto que siga mandando
    decir(numero, mensaje)


def responder(numero, texto, primer_mensaje):
    t = normalizar(texto)
    c = conv(numero)
    espera = espera_vigente(c)
    if espera and manejar_espera(numero, texto, espera):
        return
    if es_cierre(texto):  # despedida / agradecimiento: el bot no contesta
        marcar(numero, fallos=0)
        evento("cierre")
        return
    prods = productos_en(t)
    aclarar = []
    if not prods:
        parciales = productos_parciales(t)
        if len(parciales) == 1:
            prods = parciales
        elif 2 <= len(parciales) <= 6:
            aclarar = parciales
    for p in prods:
        evento("producto", p["clave"])

    if tiene(t, PALABRAS_VENDEDOR):
        return derivar(numero, "pedido", DERIVACION)
    if tiene(t, PALABRAS_PEDIDO) and not prods:
        return mostrar_carrito(numero)
    if tiene(t, PALABRAS_PRESUPUESTO):
        if carrito_items(numero):
            return pedir_presupuesto(numero)
        if not prods:
            return ofrecer_armar_pedido(numero)
    if tiene(t, PALABRAS_PRECIO):
        return derivar(numero, "precio", DERIVACION_PRECIOS)

    # Si en el mismo mensaje preguntan varias cosas, se contestan todas.
    contesto = False
    if tiene(t, PALABRAS_HORARIOS):
        evento("horarios")
        decir(numero, texto_horarios(con_pregunta=not prods))
        contesto = True
    a = ajustes()
    if tiene(t, PALABRAS_DIRECCION):
        if not a["direccion"]:
            return derivar(numero, "direccion", DERIVACION_SIN_DATO)
        decir(numero, a["direccion"])
        contesto = True
    if tiene(t, PALABRAS_PAGO):
        if not a["medios_pago"]:
            return derivar(numero, "pago", DERIVACION_SIN_DATO)
        decir(numero, a["medios_pago"])
        contesto = True
    if tiene(t, PALABRAS_ENVIO):
        if not a["envios"]:
            return derivar(numero, "envio", DERIVACION_SIN_DATO)
        decir(numero, a["envios"])
        contesto = True
    for p in prods[:3]:
        enviar_producto(numero, p)
        contesto = True
    for p in (productos_en(t, pausados=True) if not prods else [])[:2]:
        evento("producto_pausado", p["clave"])
        decir_botones(numero, f"😕 Por el momento no tenemos *{p['nombre']}* disponible. "
                              "Si querés, un vendedor te recomienda una alternativa o te avisa cuando vuelva a haber.",
                      [("vendedor", "👤 Hablar con vendedor"), ("productos", "🧱 Ver productos")],
                      "Escribí *VENDEDOR* para consultar por una alternativa.")
        contesto = True
    if aclarar:
        lista = "\n".join(f"• {p['nombre']}" for p in aclarar)
        decir(numero, f"Tenemos varias opciones 🧰\n{lista}\n\nEscribime el nombre completo y te paso la foto.")
        contesto = True
    if contesto:
        marcar(numero, fallos=0)
        return

    if tiene(t, PALABRAS_PRODUCTOS):
        mostrar_productos(numero)
    elif tiene(t, PALABRAS_MENU) or tiene(t, PALABRAS_SALUDO):
        evento("saludo")
        if esperando_vendedor(c):
            avisar_espera(numero)
        else:
            decir_menu(numero, saludo_texto())
    elif primer_mensaje:
        evento("no_entendido", texto)  # lo anotamos, pero primero lo saludamos
        decir_menu(numero, saludo_texto())
        return
    else:
        evento("no_entendido", texto)
        if esperando_vendedor(c):
            return  # ya lo va a leer un vendedor: no lo llenamos de "no te entendí"
        if sumar_fallo(numero) >= MAX_FALLOS:
            return derivar(numero, "insistencia", DERIVACION_INSISTENCIA)
        decir_menu(numero, "No te entendí del todo 🙈")
        return
    marcar(numero, fallos=0)


# ---- junta los mensajes seguidos del mismo cliente y contesta UNA sola vez
_pendientes = {}
_pend_lock = threading.Lock()
MAX_JUNTAR = 10.0  # aunque siga escribiendo, a los 10 s se le contesta
MAX_JUNTAR_ARCHIVOS = 30.0  # con fotos/archivos seguidos damos más margen (tardan en subir)


def encolar(numero, texto="", archivos=0):
    """Junta lo que el cliente manda seguido (textos y/o fotos/archivos) para contestar UNA sola vez."""
    with _pend_lock:
        p = _pendientes.get(numero)
        nuevo = p is None
        if nuevo:
            p = _pendientes[numero] = {"textos": [], "archivos": 0, "desde": time.time(), "ultimo": 0.0}
        if texto:
            p["textos"].append(texto)
        p["archivos"] += archivos
        p["ultimo"] = time.time()
    if nuevo:
        threading.Thread(target=_esperar_y_responder, args=(numero,), daemon=True).start()


def texto_archivos(n):
    if n <= 1:
        return DERIVACION_ARCHIVO
    return f"Recibí lo que me mandaste ({n} archivos) 📎. Se lo paso a un vendedor para que lo revise. 🔔"


def responder_archivos(numero, n):
    if DERIVAR_ARCHIVOS:
        derivar(numero, "archivo", texto_archivos(n))
    else:
        decir_menu(numero, "Recibí tu archivo 🙂" if n <= 1 else f"Recibí tus {n} archivos 🙂")


def _esperar_y_responder(numero):
    try:
        while True:
            time.sleep(0.25)
            with _pend_lock:
                p = _pendientes[numero]
                ahora_ts = time.time()
                espera = max(ESPERA_JUNTAR, ESPERA_ARCHIVOS) if p["archivos"] else ESPERA_JUNTAR
                tope = MAX_JUNTAR_ARCHIVOS if p["archivos"] else MAX_JUNTAR
                if ahora_ts - p["ultimo"] >= espera or ahora_ts - p["desde"] >= tope:
                    del _pendientes[numero]
                    textos, archivos = p["textos"], p["archivos"]
                    break
        with lock_de(numero):
            c = conv(numero)
            if bot_callado(c):
                return  # mientras tanto lo tomó un vendedor
            if archivos:
                responder_archivos(numero, archivos)  # una sola respuesta por tanda (si hubo texto, lo lee el vendedor)
            else:
                responder(numero, " ".join(textos), es_primer_mensaje(numero))
    except Exception:
        log.exception("Error contestando a %s", numero)
        with _pend_lock:
            _pendientes.pop(numero, None)


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
        guardar_archivo(nombre, d.content, mime)
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
        texto, entrada, accion = "", "", ""
        archivo, nombre_archivo, es_archivo, tipo_guardado = "", "", False, "text"

        if tipo == "text":
            texto = entrada = (msg.get("text") or {}).get("body", "")
        elif tipo == "interactive":
            inter = msg.get("interactive") or {}
            resp = inter.get("button_reply") or inter.get("list_reply") or {}
            texto = resp.get("title", "")
            bid = resp.get("id", "")
            if bid in BOTONES:
                entrada = BOTONES[bid]
            elif bid.split(":")[0] in ("prod", "add", "pag", "cat") or bid in ("otro", "carrito", "vaciar", "pedir", "cats"):
                accion = bid
                entrada = texto
            else:
                entrada = texto
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

        if bot_callado(conv(numero)):
            return  # ya lo atiende una persona: el bot se queda callado

        if es_archivo:
            evento("archivo", tipo)
            if ESPERA_JUNTAR > 0:
                encolar(numero, "", archivos=1)  # si manda varios seguidos, se contesta una sola vez
            else:
                responder_archivos(numero, 1)
            return
        if accion:
            manejar_accion(numero, accion)  # toques de botones: se contestan al instante
            return
        if tipo == "text" and ESPERA_JUNTAR > 0:
            encolar(numero, entrada)  # espera un instante por si manda más mensajes
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
        '<input type="text" name="usuario" placeholder="Usuario (vacío = administrador)" '
        'autocomplete="username" autocapitalize="none" '
        'style="width:100%;padding:10px;font-size:16px;box-sizing:border-box;margin-bottom:8px">'
        '<input type="password" name="clave" placeholder="Clave" autocomplete="current-password" '
        'autofocus required style="width:100%;padding:10px;font-size:16px;box-sizing:border-box">'
        '<button style="margin-top:12px;width:100%;padding:10px;border:0;'
        'border-radius:8px;background:#075e54;color:#fff;font-size:16px">Entrar</button>'
        f"{aviso}</form></body></html>"
    )


@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr or "?"
    if bloqueado(ip):
        return pagina_login("Demasiados intentos. Probá de nuevo en unos minutos."), 429
    usuario = request.form.get("usuario", "").strip().lower() or "admin"
    clave = request.form.get("clave", "")
    datos = None
    if usuario == "admin":
        if not PANEL_PASSWORD:
            return pagina_login("Panel desactivado: falta cargar PANEL_PASSWORD en el servidor."), 503
        if hmac.compare_digest(clave.encode(), PANEL_PASSWORD.encode()):
            datos = ("admin", "Administrador", "admin")
    else:
        fila = None
        try:
            with tx() as con:
                fila = con.execute(
                    "SELECT usuario, nombre, clave_hash, rol, activo FROM usuarios WHERE usuario=?", (usuario,)
                ).fetchone()
        except Exception:
            log.exception("No pude consultar los usuarios")
        if fila and fila["activo"] and check_password_hash(fila["clave_hash"], clave):
            datos = (fila["usuario"], fila["nombre"] or fila["usuario"], fila["rol"])
    if datos:
        INTENTOS.pop(ip, None)
        session.clear()
        session.permanent = True
        session["ok"] = True
        session["usuario"], session["nombre"], session["rol"] = datos
        _activos.pop(datos[0], None)
        return redirect("/panel")
    fallos, desde = INTENTOS.get(ip, (0, time.time()))
    INTENTOS[ip] = (fallos + 1, desde)
    return pagina_login("Usuario o clave incorrectos."), 401


@app.route("/login", methods=["GET"])
def login_get():
    return redirect("/panel")


@app.route("/salir")
def salir():
    session.clear()
    return redirect("/panel")


_activos = {}  # usuario -> (hora, está_activo)


def usuario_activo(usuario):
    """Si desactivás a un vendedor, deja de poder usar el panel en menos de 30 segundos."""
    hit = _activos.get(usuario)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    activo = True  # si la base falla un instante, no dejamos a todos afuera
    try:
        with tx() as con:
            f = con.execute("SELECT activo FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
        activo = bool(f and f["activo"])
    except Exception:
        log.exception("No pude comprobar el usuario")
    _activos[usuario] = (time.time(), activo)
    return activo


def requiere_login(f):
    @functools.wraps(f)
    def envoltura(*args, **kwargs):
        if not session.get("ok"):
            return jsonify({"error": "no autorizado"}), 401
        usuario = session.get("usuario", "admin")  # las sesiones anteriores a los usuarios eran del administrador
        if usuario != "admin" and not usuario_activo(usuario):
            session.clear()
            return jsonify({"error": "no autorizado"}), 401
        return f(*args, **kwargs)
    return envoltura


def requiere_admin(f):
    @requiere_login
    @functools.wraps(f)
    def envoltura(*args, **kwargs):
        if session.get("rol", "admin") != "admin":
            return jsonify({"ok": False, "error": "Esto lo puede hacer solo el administrador."}), 403
        return f(*args, **kwargs)
    return envoltura


def quien():
    return session.get("nombre") or "Administrador"


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
    a = ajustes()
    return jsonify({
        "rapidas": a["rapidas"], "negocio": a["nombre"], "resena": texto_resena(), "db": "supabase" if USA_PG else "temporal",
        "usuario": session.get("usuario", "admin"), "nombre": quien(), "rol": session.get("rol", "admin"),
    })


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
        "atiende": c["atiende"],
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
        n_pedidos = con.execute("SELECT COUNT(*) AS n FROM pedidos WHERE numero=?", (numero,)).fetchone()["n"]
        con.execute("UPDATE conversaciones SET no_leidos=0 WHERE numero=?", (numero,))
        filas = con.execute(
            "SELECT * FROM (SELECT * FROM mensajes WHERE numero=? ORDER BY id DESC LIMIT 300) AS ult ORDER BY id",
            (numero,),
        ).fetchall()
    return jsonify({
        "numero": numero,
        "titulo": titulo(c),
        "nombre": c["nombre"],
        "humano": bool(c["humano"]),
        "pide_vendedor": bool(c["pide_vendedor"]),
        "atiende": c["atiende"],
        "ventana": ventana_abierta(c),
        "pedidos": n_pedidos,
        "mensajes": [{
            "id": m["id"], "quien": m["quien"], "tipo": m["tipo"], "texto": m["texto"],
            "archivo": url_media(m["archivo"]) if m["archivo"] else "",
            "nombre_archivo": m["nombre_archivo"], "hora": fmt(m["ts"]),
            "estado": m["estado"], "error": m["error"], "autor": m["autor"],
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
    marcar(numero, humano=1, pide_vendedor=0, fallos=0, atiende=quien())  # ya lo atiende una persona


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
    registrar(numero, "vendedor", "text", texto, wamid=wamid, estado="sent", autor=quien())
    _despues_de_enviar(numero)
    return jsonify({"ok": True})


OFFICE_MODERNO = (  # carpeta interna del .zip -> (extensión, tipo MIME)
    ("xl/", ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("word/", ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("ppt/", ".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
)
OFFICE_VIEJO = {  # Excel/Word/PowerPoint de antes de 2007 (se reconocen por el contenido y la extensión)
    ".xls": "application/vnd.ms-excel", ".doc": "application/msword", ".ppt": "application/vnd.ms-powerpoint",
}


def _tipo_office(datos):
    """Un .xlsx/.docx/.pptx de verdad es un zip con [Content_Types].xml y su carpeta. Sin macros."""
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            nombres = z.namelist()
    except (zipfile.BadZipFile, ValueError):
        return None
    if "[Content_Types].xml" not in nombres or any(n.lower().endswith("vbaproject.bin") for n in nombres):
        return None
    for carpeta, ext, mime in OFFICE_MODERNO:
        if any(n.startswith(carpeta) for n in nombres):
            return ext, mime
    return None


def detectar_tipo(datos, nombre=""):
    """Mira el contenido real del archivo (no solo el nombre)."""
    if datos.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg", "image/jpeg"
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png", "image/png"
    if datos.startswith(b"%PDF"):
        return "document", ".pdf", "application/pdf"
    if datos.startswith(b"PK\x03\x04"):
        t = _tipo_office(datos)
        if t:
            return "document", t[0], t[1]
    if datos.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        ext = os.path.splitext(nombre or "")[1].lower()
        if ext in OFFICE_VIEJO:
            return "document", ext, OFFICE_VIEJO[ext]
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
    tipo, ext, mime = detectar_tipo(datos, archivo.filename or "")
    if not tipo:
        return jsonify({"ok": False, "error": "Solo se pueden enviar fotos JPG o PNG, PDF, Excel (.xlsx), Word (.docx) o PowerPoint (.pptx). "
                                               "Los archivos con macros no."}), 400
    if len(datos) > (MAX_FOTO if tipo == "image" else MAX_PDF):
        return jsonify({"ok": False, "error": "El archivo es demasiado pesado (fotos hasta 5 MB, documentos hasta 16 MB)."}), 400

    nombre = uuid.uuid4().hex + ext
    guardar_archivo(nombre, datos, mime)
    nombre_mostrado = secure_filename(archivo.filename or "")[:80] or ("documento" + ext if tipo == "document" else "foto" + ext)

    media_id, error = subir_media(nombre, datos, mime)
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
              nombre_archivo=nombre_mostrado if tipo == "document" else "", wamid=wamid, estado="sent",
              autor=quien())
    _despues_de_enviar(numero)
    return jsonify({"ok": True})


@app.route("/api/enviar_producto", methods=["POST"])
@requiere_login
def api_enviar_producto():
    """El vendedor, charlando, manda un producto del catálogo: foto con nombre y descripción (+ las otras fotos)."""
    datos = request.get_json(silent=True) or {}
    numero, clave = str(datos.get("numero", "")), str(datos.get("clave", ""))
    if not numero_valido(numero):
        return jsonify({"ok": False, "error": "Faltan datos"}), 400
    c = conv(numero)
    if c is None:
        return jsonify({"ok": False, "error": "La conversación no existe"}), 404
    if not ventana_abierta(c):
        return jsonify({"ok": False, "error": MSG_VENTANA}), 409
    p = next((x for x in catalogo_todo(True) if x["clave"] == clave), None)
    if p is None:
        return jsonify({"ok": False, "error": "No encontré ese producto."}), 404
    caption = f"*{p['nombre']}*" + (f"\n{p['texto']}" if p.get("texto") else "")
    if p.get("foto"):
        ok, wamid, error = enviar_foto(numero, p["origen"], p["foto"], caption)
        if not ok:
            return jsonify({"ok": False, "error": error or "No se pudo enviar la foto"}), 502
        registrar(numero, "vendedor", "image", caption, archivo=f"{p['origen']}/{p['foto']}",
                  wamid=wamid, estado="sent", autor=quien())
        for f in (p.get("extras") or [])[:MAX_FOTOS_PRODUCTO - 1]:
            ok2, wamid2, error2 = enviar_foto(numero, "m", f, "")
            if ok2:
                registrar(numero, "vendedor", "image", "", archivo=f"m/{f}", wamid=wamid2, estado="sent", autor=quien())
            else:
                log.warning("No pude mandar la foto extra %s (%s)", f, error2)
    else:
        ok, wamid, error = enviar_texto(numero, caption)
        if not ok:
            return jsonify({"ok": False, "error": error}), 502
        registrar(numero, "vendedor", "text", caption, wamid=wamid, estado="sent", autor=quien())
    evento("producto_enviado_vendedor", clave)
    _despues_de_enviar(numero)
    return jsonify({"ok": True})


@app.route("/api/aceptar", methods=["POST"])
@requiere_login
def api_aceptar():
    """El vendedor toma la conversación: desde acá el bot se calla del todo."""
    numero = str((request.get_json(silent=True) or {}).get("numero", ""))
    if not numero_valido(numero) or conv(numero) is None:
        return jsonify({"ok": False, "error": "Número inválido"}), 400
    evento("vendedor_acepto")
    _despues_de_enviar(numero)
    return jsonify({"ok": True})


@app.route("/api/devolver_bot", methods=["POST"])
@requiere_login
def api_devolver_bot():
    data = request.get_json(silent=True) or {}
    numero = str(data.get("numero", ""))
    if not numero_valido(numero) or conv(numero) is None:
        return jsonify({"ok": False, "error": "Número inválido"}), 400
    marcar(numero, humano=0, pide_vendedor=0, fallos=0, atiende="")
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
        nuevas = con.execute("SELECT COUNT(*) AS n FROM conversaciones WHERE creada>=?", (desde,)).fetchone()["n"]
        recibidos = con.execute(
            "SELECT COUNT(*) AS n FROM mensajes WHERE quien='cliente' AND ts>=?", (desde,)).fetchone()["n"]
        motivos = con.execute(
            "SELECT detalle, COUNT(*) AS n FROM eventos WHERE tipo='vendedor' AND ts>=? "
            "GROUP BY detalle ORDER BY n DESC", (desde,)).fetchall()
        prods = con.execute(
            "SELECT detalle, COUNT(*) AS n FROM eventos WHERE tipo='producto' AND ts>=? "
            "GROUP BY detalle ORDER BY n DESC LIMIT 10", (desde,)).fetchall()
        sin_entender = con.execute(
            "SELECT MIN(detalle) AS d, COUNT(*) AS n FROM eventos WHERE tipo='no_entendido' AND ts>=? "
            "GROUP BY lower(detalle) ORDER BY n DESC, MAX(ts) DESC LIMIT 15", (desde,)).fetchall()
        tiempos = [r["ts"] for r in con.execute(
            "SELECT ts FROM mensajes WHERE quien='cliente' AND ts>=? ORDER BY ts", (desde,)).fetchall()]
    por_dia = {}
    for ts in tiempos:
        d = datetime.fromtimestamp(ts, TZ_AR).strftime("%d/%m")
        por_dia[d] = por_dia.get(d, 0) + 1
    nombres_prod = {p["clave"]: p["nombre"] for p in catalogo_todo()}
    nombres_motivo = {
        "pedido": "Pidieron un vendedor", "precio": "Preguntaron precios", "archivo": "Mandaron foto/audio/archivo",
        "insistencia": "El bot no los entendió (2 veces)", "direccion": "Dirección (sin dato cargado)",
        "pago": "Medios de pago (sin dato cargado)", "presupuesto": "Pidieron presupuesto (pedido armado)", "envio": "Envíos (sin dato cargado)",
    }
    return jsonify({
        "dias": dias,
        "conversaciones_nuevas": nuevas,
        "mensajes_clientes": recibidos,
        "derivaciones": sum(m["n"] for m in motivos),
        "motivos": [{"texto": nombres_motivo.get(m["detalle"], m["detalle"]), "n": m["n"]} for m in motivos],
        "productos": [{"texto": nombres_prod.get(p["detalle"], p["detalle"]), "n": p["n"]} for p in prods],
        "sin_entender": [{"texto": x["d"], "n": x["n"]} for x in sin_entender],
        "por_dia": [{"texto": d, "n": n} for d, n in por_dia.items()],
    })


@app.route("/api/catalogo")
@requiere_login
def api_catalogo():
    salida = []
    for p in catalogo_todo(True):
        fotos = []
        if p["foto"]:
            fotos.append({"nombre": p["foto"], "url": url_media(f"{p['origen']}/{p['foto']}"),
                          "propia": p["origen"] == "m"})
        fotos += [{"nombre": f, "url": url_media(f"m/{f}"), "propia": True} for f in (p.get("extras") or [])]
        salida.append({
            "clave": p["clave"], "nombre": p["nombre"], "texto": p["texto"],
            "palabras": ", ".join(p["terms"][1:]), "editable": p["editable"], "fuente": p["fuente"],
            "foto": fotos[0]["url"] if fotos else "", "fotos": fotos,
            "categoria": p.get("categoria", ""), "activo": p.get("activo", True),
        })
    return jsonify(salida)


@app.route("/api/catalogo", methods=["POST"])
@requiere_login
def api_catalogo_guardar():
    nombre = " ".join(str(request.form.get("nombre", "")).split())[:60]
    palabras = str(request.form.get("palabras", "")).strip()[:200]
    texto = str(request.form.get("texto", "")).strip()[:900]
    categoria = " ".join(str(request.form.get("categoria", "")).split())[:24]
    clave = normalizar(nombre)
    if not clave:
        return jsonify({"ok": False, "error": "Poné el nombre del producto."}), 400
    try:
        quitar = {str(x) for x in json.loads(request.form.get("quitar") or "[]")}
    except ValueError:
        quitar = set()
    nuevas = []
    subidas = request.files.getlist("fotos") + request.files.getlist("foto")
    for archivo in subidas:
        datos = archivo.read()
        if not datos:
            continue
        tipo, ext, mime = detectar_tipo(datos)
        if tipo != "image":
            return jsonify({"ok": False, "error": "Las fotos tienen que ser JPG o PNG."}), 400
        if len(datos) > MAX_FOTO:
            return jsonify({"ok": False, "error": "Una foto pesa demasiado (máximo 5 MB)."}), 400
        nuevas.append((uuid.uuid4().hex + ext, datos, mime))
    with tx() as con:
        previo = con.execute("SELECT foto, fotos FROM productos WHERE clave=?", (clave,)).fetchone()
        viejas = ([previo["foto"]] if previo and previo["foto"] else []) + \
                 ([x for x in (previo["fotos"] or "").split(",") if x] if previo else [])
        quedan = [f for f in viejas if f not in quitar]
        if len(quedan) + len(nuevas) > MAX_FOTOS_PRODUCTO:
            return jsonify({"ok": False, "error": f"Máximo {MAX_FOTOS_PRODUCTO} fotos por producto."}), 400
        for nom, datos, mime in nuevas:
            guardar_archivo(nom, datos, mime)
        todas = quedan + [n[0] for n in nuevas]
        principal, extras = (todas[0] if todas else ""), ",".join(todas[1:])
        ahora_ts = time.time()
        con.execute(
            "INSERT INTO productos (clave, nombre, palabras, texto, foto, fotos, oculto, categoria, ts) VALUES (?,?,?,?,?,?,0,?,?) "
            "ON CONFLICT (clave) DO UPDATE SET nombre=?, palabras=?, texto=?, foto=?, fotos=?, oculto=0, categoria=?, ts=?",
            (clave, nombre, palabras, texto, principal, extras, categoria, ahora_ts,
             nombre, palabras, texto, principal, extras, categoria, ahora_ts),
        )
    for f in viejas:
        if f in quitar:
            borrar_archivo(f)
    catalogo_actual(True)  # el bot lo reconoce desde este mismo instante
    return jsonify({"ok": True})


@app.route("/api/catalogo/borrar", methods=["POST"])
@requiere_login
def api_catalogo_borrar():
    """Quita un producto. Si venía de GitHub (carpeta/código) se oculta; si era del panel se borra con sus fotos."""
    clave = str((request.get_json(silent=True) or {}).get("clave", ""))
    base = _catalogo_base().get(clave)
    with tx() as con:
        fila = con.execute("SELECT nombre, foto, fotos FROM productos WHERE clave=?", (clave,)).fetchone()
        if fila is None and base is None:
            return jsonify({"ok": False, "error": "No encontré ese producto."}), 404
        if base is not None:
            con.execute(
                "INSERT INTO productos (clave, nombre, palabras, texto, foto, fotos, oculto, ts) VALUES (?,?,?,?,?,?,1,?) "
                "ON CONFLICT (clave) DO UPDATE SET foto='', fotos='', oculto=1, ts=?",
                (clave, base["nombre"], "", "", "", "", time.time(), time.time()),
            )
        else:
            con.execute("DELETE FROM productos WHERE clave=?", (clave,))
    if fila:
        for f in ([fila["foto"]] if fila["foto"] else []) + [x for x in (fila["fotos"] or "").split(",") if x]:
            borrar_archivo(f)
    catalogo_actual(True)
    return jsonify({"ok": True})


@app.route("/api/catalogo/activo", methods=["POST"])
@requiere_login
def api_catalogo_activo():
    """Pausa (activo=false) o reactiva un producto sin borrarlo: el bot deja de ofrecerlo y avisa que no hay."""
    datos = request.get_json(silent=True) or {}
    clave, activo = str(datos.get("clave", "")), 1 if datos.get("activo") else 0
    base = _catalogo_base().get(clave)
    with tx() as con:
        fila = con.execute("SELECT clave FROM productos WHERE clave=?", (clave,)).fetchone()
        if fila is None and base is None:
            return jsonify({"ok": False, "error": "No encontré ese producto."}), 404
        if fila is None:
            con.execute("INSERT INTO productos (clave, nombre, palabras, texto, foto, fotos, oculto, activo, ts) "
                        "VALUES (?,?,?,?,?,?,0,?,?)", (clave, base["nombre"], "", "", "", "", activo, time.time()))
        else:
            con.execute("UPDATE productos SET activo=?, oculto=0, ts=? WHERE clave=?", (activo, time.time(), clave))
    catalogo_actual(True)
    return jsonify({"ok": True, "activo": bool(activo)})


def _celda_texto(ws, ref, valor):
    """Escribe texto del cliente SIEMPRE como texto (si empieza con '=' no se convierte en fórmula)."""
    c = ws[ref]
    c.value = str(valor)
    c.data_type = "s"
    return c


def _numero_de(cantidad):
    m = re.search(r"\d+(?:[.,]\d+)?", cantidad or "")
    return float(m.group(0).replace(",", ".")) if m else None


@app.route("/api/pedidos/<numero>/excel")
@requiere_login
def api_pedido_excel(numero):
    """Excel del último pedido (o ?id=N) para que el vendedor complete los precios y arme el presupuesto."""
    if not numero_valido(numero):
        return jsonify({"error": "número inválido"}), 400
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return jsonify({"error": "Falta instalar openpyxl (agregalo a requirements.txt)."}), 500
    pid = request.args.get("id", type=int)
    with tx() as con:
        c = con.execute("SELECT * FROM conversaciones WHERE numero=?", (numero,)).fetchone()
        if pid:
            fila = con.execute("SELECT * FROM pedidos WHERE numero=? AND id=?", (numero, pid)).fetchone()
        else:
            fila = con.execute("SELECT * FROM pedidos WHERE numero=? ORDER BY id DESC LIMIT 1", (numero,)).fetchone()
    if c is None or fila is None:
        return jsonify({"error": "Este cliente todavía no armó ningún pedido."}), 404
    try:
        items = json.loads(fila["items"])
    except ValueError:
        items = []
    wb = Workbook()
    ws = wb.active
    ws.title = "Presupuesto"
    _celda_texto(ws, "A1", ajustes()["nombre"]).font = Font(bold=True, size=14)
    _celda_texto(ws, "A2", "Cliente: " + titulo(c) + "  |  +" + numero)
    _celda_texto(ws, "A3", "Pedido del " + fmt(fila["ts"]))
    cab = ["N°", "Producto", "Cantidad pedida", "Cant.", "Precio unitario", "Subtotal"]
    for col, t in enumerate(cab, 1):
        x = ws.cell(row=5, column=col, value=t)
        x.font = Font(bold=True, color="FFFFFF")
        x.fill = PatternFill("solid", fgColor="075E54")
        x.alignment = Alignment(horizontal="center")
    r = 6
    for n, it in enumerate(items, 1):
        ws.cell(row=r, column=1, value=n)
        _celda_texto(ws, f"B{r}", it.get("nombre", ""))
        _celda_texto(ws, f"C{r}", it.get("cantidad", ""))
        cant = _numero_de(it.get("cantidad", ""))
        if cant is not None:
            ws.cell(row=r, column=4, value=cant)
        ws.cell(row=r, column=6, value=f'=IF(AND(ISNUMBER(D{r}),ISNUMBER(E{r})),D{r}*E{r},"")')
        ws.cell(row=r, column=5).number_format = '"$" #,##0.00'
        ws.cell(row=r, column=6).number_format = '"$" #,##0.00'
        r += 1
    ws.cell(row=r + 1, column=5, value="TOTAL").font = Font(bold=True)
    tot = ws.cell(row=r + 1, column=6, value=f"=SUM(F6:F{max(r - 1, 6)})")
    tot.font = Font(bold=True)
    tot.number_format = '"$" #,##0.00'
    _celda_texto(ws, f"A{r + 3}", "Completá la columna «Precio unitario» y el subtotal se calcula solo. "
                                  "Si la cantidad no es un número (ej: «una bolsa»), escribila en la columna «Cant.».")
    for col, ancho in zip("ABCDEF", (6, 38, 22, 9, 16, 16)):
        ws.column_dimensions[col].width = ancho
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    limpio = re.sub(r"[^A-Za-z0-9_-]+", "_", normalizar(titulo(c)))[:30] or numero
    return send_file(buf, as_attachment=True, download_name=f"pedido_{limpio}_{datetime.fromtimestamp(fila['ts'], ahora().tzinfo).strftime('%d-%m-%Y')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---- AJUSTES, USUARIOS, COPIA DE SEGURIDAD (solo administrador) y /salud
@app.route("/api/ajustes")
@requiere_admin
def api_ajustes():
    return jsonify(ajustes(True))


@app.route("/api/ajustes", methods=["POST"])
@requiere_admin
def api_ajustes_guardar():
    datos, error = validar_ajustes(request.get_json(silent=True))
    if error:
        return jsonify({"ok": False, "error": error}), 400
    guardar_ajustes(datos)
    log.info("Ajustes cambiados por %s", quien())
    return jsonify({"ok": True})


@app.route("/api/usuarios")
@requiere_admin
def api_usuarios():
    with tx() as con:
        filas = con.execute("SELECT usuario, nombre, rol, activo FROM usuarios ORDER BY usuario").fetchall()
    return jsonify([{"usuario": f["usuario"], "nombre": f["nombre"], "rol": f["rol"], "activo": bool(f["activo"])}
                    for f in filas])


@app.route("/api/usuarios", methods=["POST"])
@requiere_admin
def api_usuarios_crear():
    d = request.get_json(silent=True) or {}
    usuario = str(d.get("usuario", "")).strip().lower()
    nombre = str(d.get("nombre", "")).strip()[:40]
    clave = str(d.get("clave", ""))
    rol = str(d.get("rol", "vendedor"))
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", usuario) or usuario == "admin":
        return jsonify({"ok": False, "error": "El usuario: 3 a 30 letras minúsculas, números, punto o guión (no puede ser «admin»)."}), 400
    if not nombre:
        return jsonify({"ok": False, "error": "Poné el nombre de la persona (es el que ve el cliente en el panel)."}), 400
    if not 6 <= len(clave) <= 100:
        return jsonify({"ok": False, "error": "La clave tiene que tener al menos 6 caracteres."}), 400
    if rol not in ("vendedor", "admin"):
        return jsonify({"ok": False, "error": "Rol inválido."}), 400
    with tx() as con:
        cur = con.execute(
            "INSERT INTO usuarios (usuario, nombre, clave_hash, rol, activo, creado) VALUES (?,?,?,?,1,?) "
            "ON CONFLICT (usuario) DO NOTHING",
            (usuario, nombre, generate_password_hash(clave), rol, time.time()),
        )
        creado = cur.rowcount == 1
    if not creado:
        return jsonify({"ok": False, "error": "Ese usuario ya existe."}), 409
    return jsonify({"ok": True})


@app.route("/api/usuarios/<usuario>/clave", methods=["POST"])
@requiere_admin
def api_usuarios_clave(usuario):
    clave = str((request.get_json(silent=True) or {}).get("clave", ""))
    if not 6 <= len(clave) <= 100:
        return jsonify({"ok": False, "error": "La clave tiene que tener al menos 6 caracteres."}), 400
    with tx() as con:
        cur = con.execute("UPDATE usuarios SET clave_hash=? WHERE usuario=?", (generate_password_hash(clave), usuario))
        n = cur.rowcount
    if n != 1:
        return jsonify({"ok": False, "error": "Ese usuario no existe."}), 404
    return jsonify({"ok": True})


@app.route("/api/usuarios/<usuario>/activo", methods=["POST"])
@requiere_admin
def api_usuarios_activo(usuario):
    activo = 1 if (request.get_json(silent=True) or {}).get("activo") else 0
    with tx() as con:
        cur = con.execute("UPDATE usuarios SET activo=? WHERE usuario=?", (activo, usuario))
        n = cur.rowcount
    _activos.pop(usuario, None)
    if n != 1:
        return jsonify({"ok": False, "error": "Ese usuario no existe."}), 404
    return jsonify({"ok": True})


@app.route("/api/backup")
@requiere_admin
def api_backup():
    """Descarga todo (chats, productos, ajustes, reportes) en un archivo. No incluye fotos ni claves."""
    datos = {}
    with tx() as con:
        for tabla in ("conversaciones", "mensajes", "eventos", "productos", "ajustes", "pedidos"):
            datos[tabla] = [dict(r) for r in con.execute(f"SELECT * FROM {tabla}").fetchall()]
        datos["usuarios"] = [dict(r) for r in con.execute(
            "SELECT usuario, nombre, rol, activo, creado FROM usuarios").fetchall()]
    nombre = "copia_ferreteria_" + ahora().strftime("%Y-%m-%d") + ".json"
    resp = app.response_class(json.dumps(datos, ensure_ascii=False, indent=1), mimetype="application/json")
    resp.headers["Content-Disposition"] = f'attachment; filename="{nombre}"'
    return resp


@app.route("/salud")
def salud():
    """Para un monitor externo (ej: UptimeRobot): mantiene despierto a Render y activa la base."""
    try:
        with tx() as con:
            con.execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "db": "supabase" if USA_PG else "temporal"}), 200
    except Exception:
        log.exception("Chequeo de salud: falló la base")
        return jsonify({"ok": False}), 503


@app.route("/media/<origen>/<nombre>")
@requiere_login
def media(origen, nombre):
    """Archivos de clientes y fotos del catálogo: solo se ven con sesión iniciada."""
    if origen == "m":
        if secure_filename(nombre) != nombre:
            abort(404)
        datos = leer_archivo(nombre)
    elif origen == "c":
        try:
            permitido = nombre in os.listdir(CATALOGO_DIR)  # solo archivos que realmente están en la carpeta
        except OSError:
            permitido = False
        datos = leer_catalogo(os.path.join(CATALOGO_DIR, nombre)) if permitido else None
    else:
        abort(404)
    if datos is None:
        abort(404)
    ext = os.path.splitext(nombre)[1].lower()
    mime = MIME_POR_EXT.get(ext) or {".jpeg": "image/jpeg"}.get(ext)
    resp = send_file(io.BytesIO(datos), mimetype=mime or "application/octet-stream",
                     as_attachment=not mime, download_name=nombre)
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
.modo{font-size:12px;opacity:.9}
header,header .der{flex-wrap:wrap}
#modalAj{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;padding:16px;z-index:10}
.caja.ancha{max-width:720px}
.campo{display:flex;flex-direction:column;gap:4px;margin:8px 0}
.campo label{font-size:13px;color:var(--gris)}
.campo input[type=text],.campo input[type=password],.campo textarea,.campo select{padding:8px;border:1px solid #ccc;border-radius:8px;font:inherit}
.campo textarea{min-height:70px;resize:vertical}
.turno{border:1px solid var(--borde);border-radius:8px;padding:8px;margin:6px 0;display:flex;flex-direction:column;gap:6px}
.turno input[type=text],.turno input[type=time]{padding:6px;border:1px solid #ccc;border-radius:6px;font:inherit}
.dias,.horas{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.dias label{display:flex;align-items:center;gap:3px;font-size:13px}
.usr{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #eee;flex-wrap:wrap}
.usr .info{flex:1;min-width:150px}
.acciones{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
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
.tick{font-weight:700;letter-spacing:-3px;margin-left:2px;color:#8696a0}
.tick.leido{color:#34b7f1}
.emojibtn{background:none;border:0;font-size:22px;padding:0 6px;cursor:pointer}
#emojis{display:flex;flex-wrap:wrap;gap:2px;padding:6px 10px;background:#fff;border-top:1px solid #e3e3e3;max-height:130px;overflow-y:auto}
#emojis button{background:none;border:0;font-size:22px;padding:2px 4px;cursor:pointer;border-radius:6px}
#emojis button:hover{background:#eee}
#catLista{max-height:300px;overflow-y:auto}
.filtrocat{display:flex;gap:8px;margin:6px 0}
.filtrocat input{flex:1;min-width:0}
.pausado .miniatura,.pausado .info{opacity:.5}
.filtrocat input,.filtrocat select{padding:8px;border:1px solid #ccc;border-radius:8px;font:inherit}
.etqcat{display:inline-block;background:#e7f3ef;color:#075e54;border-radius:10px;padding:1px 8px;font-size:11px;margin-right:4px}
.etqpausa{display:inline-block;background:#fff3cd;color:#664d03;border-radius:10px;padding:1px 8px;font-size:11px}
#selProd{background:#fff;border-top:1px solid #e3e3e3;padding:8px 10px}
#selProd input{width:100%;box-sizing:border-box;padding:7px;border:1px solid #ccc;border-radius:8px;font:inherit}
#prodLista{max-height:190px;overflow-y:auto;margin-top:6px}
.prodsel{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid #f0f0f0}
.prodsel .miniatura{width:38px;height:38px;flex:none}
.prodsel .info{flex:1;min-width:0;font-size:14px}
#catFotos{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
.fotoed{position:relative}
.fotoed img{width:64px;height:64px;object-fit:cover;border-radius:6px;display:block}
.fotoed button{position:absolute;top:-6px;right:-6px;width:20px;height:20px;border-radius:50%;padding:0;line-height:18px;font-size:12px;background:#b00020;color:#fff;border:0}
.fotoed .princ{position:absolute;bottom:0;left:0;right:0;background:#0008;color:#fff;font-size:10px;text-align:center;border-radius:0 0 6px 6px}
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
#modal,#modalCat{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;padding:16px;z-index:10}
.caja{background:#fff;border-radius:10px;max-width:520px;width:100%;max-height:90vh;overflow-y:auto;padding:18px}
.caja h2{margin:0 0 6px}
.caja h3{margin:16px 0 4px;font-size:15px}
.caja ul{margin:0;padding-left:18px}
.caja li{margin:2px 0}
.cifras{display:flex;gap:10px;flex-wrap:wrap}
.cifra{background:#f0f2f5;border-radius:8px;padding:8px 12px;flex:1;min-width:110px}
.cifra b{display:block;font-size:22px}
.suave{color:var(--gris);font-size:13px}
.prodcat{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #eee}
.prodcat .info{flex:1;min-width:0}
.miniatura{width:48px;height:48px;border-radius:6px;object-fit:cover;background:#eee;display:flex;align-items:center;justify-content:center;flex:none}
.formcat{display:flex;flex-direction:column;gap:8px}
.formcat input[type=text],.formcat textarea{padding:8px;border:1px solid #ccc;border-radius:8px;font:inherit}
.formcat textarea{height:80px;resize:vertical}
button.mini{padding:4px 10px;font-size:13px}
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
 <strong>📋 Panel de la ferretería</strong> <span class="modo" id="modoDb"></span> <span class="modo" id="quienSoy"></span>
 <span class="der">
  <button class="ghost" id="btnSonido">🔕 Sonido: no</button>
  <button class="ghost oculto" id="btnAjustes">⚙️ Ajustes</button>
  <button class="ghost" id="btnCatalogo">📦 Catálogo</button>
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
    <button class="oculto" id="btnAceptar" title="Tomar esta conversación: el bot deja de contestar">✋ Aceptar</button>
    <button class="sec oculto" id="btnExcel" title="Descargar el último pedido en Excel">📥 Pedido Excel</button>
    <button class="sec oculto" id="devolver">Devolver al bot</button>
   </div>
   <div id="avisoVentana" class="oculto">⏳ Pasaron más de 24 hs desde el último mensaje del cliente: WhatsApp no deja escribirle primero. Esperá a que él escriba.</div>
   <div id="msgs"></div>
   <div id="selProd" class="oculto">
    <input type="search" id="prodBuscar" placeholder="🔎 Buscar producto para mandar..." autocomplete="off">
    <div id="prodLista"></div>
   </div>
   <div id="emojis" class="oculto"></div>
   <div id="rapidas"></div>
   <div id="form">
    <label class="adjuntar" title="Adjuntar foto, PDF, Excel o Word">📎<input type="file" id="inputArchivo" accept="image/*,.pdf,application/pdf,.xlsx,.xls,.docx,.doc,.pptx,.ppt" class="oculto"></label>
    <button type="button" class="emojibtn" id="btnProd" title="Mandar un producto del catálogo">📦</button>
    <button type="button" class="emojibtn" id="btnEmoji" title="Emojis">😀</button>
    <textarea id="texto" placeholder="Escribí tu respuesta..."></textarea>
    <button id="enviar">Enviar</button>
   </div>
  </div>
 </section>
</div>
<div id="modal" class="oculto"><div class="caja"><div id="reporteCuerpo"></div><p><button id="cerrarModal">Cerrar</button></p></div></div>
<div id="modalCat" class="oculto"><div class="caja">
 <h2>📦 Catálogo del bot</h2>
 <p class="suave">Subí una foto con el nombre del producto y el bot lo reconoce al instante, sin tocar el código. No pongas precios.</p>
 <div class="filtrocat">
  <input type="search" id="catBuscar" placeholder="🔎 Buscar producto o categoría..." autocomplete="off">
  <select id="catFiltro"><option value="">Todas las categorías</option></select>
 </div>
 <div id="catResumen" class="suave"></div>
 <div id="catLista"></div>
 <h3 id="catTitulo">Agregar producto</h3>
 <div class="formcat">
  <input type="text" id="catNombre" placeholder="Nombre (ej: Cemento portland)" maxlength="60">
  <input type="text" id="catCategoria" list="catCategorias" placeholder="Categoría (ej: Electricidad, Ferretería, Pinturas)" maxlength="24">
  <datalist id="catCategorias"></datalist>
  <input type="text" id="catPalabras" placeholder="También se busca como, separado por comas (ej: cemento, portland)" maxlength="200">
  <textarea id="catTexto" placeholder="Descripción que ve el cliente (opcional)" maxlength="900"></textarea>
  <div class="suave">Fotos del producto (hasta 6; la primera es la principal)</div>
  <div id="catFotos"></div>
  <input type="file" id="catFoto" accept="image/png,image/jpeg" multiple>
  <button id="catGuardar">Guardar producto</button>
 </div>
 <p><button class="sec" id="cerrarCat">Cerrar</button></p>
</div></div>
<div id="modalAj" class="oculto"><div class="caja ancha">
 <h2>⚙️ Ajustes del bot</h2>
 <p class="suave">Lo que cambies acá se aplica al instante, sin tocar el código. No pongas precios.</p>
 <h3>Datos del local</h3>
 <div class="campo"><label for="ajNombre">Nombre del local</label><input type="text" id="ajNombre" maxlength="60"></div>
 <div class="campo"><label for="ajDireccion">Dirección y cómo llegar (se envía cuando preguntan dónde están)</label><textarea id="ajDireccion" maxlength="900"></textarea></div>
 <div class="campo"><label for="ajPago">Medios de pago</label><textarea id="ajPago" maxlength="900"></textarea></div>
 <div class="campo"><label for="ajEnvios">Envíos a domicilio (qué responder)</label><textarea id="ajEnvios" maxlength="900"></textarea></div>
 <h3>Horarios de atención</h3>
 <div id="ajTurnos"></div>
 <button class="sec mini" id="ajAgregarTurno">+ Agregar horario</button>
 <div class="campo"><label for="ajCerrado">Días cerrados (como lo ve el cliente)</label><input type="text" id="ajCerrado" maxlength="60"></div>
 <div class="campo"><label for="ajFeriados">Feriados o días sin atención (uno por línea, ej: 25/12/2026)</label><textarea id="ajFeriados"></textarea></div>
 <h3>Mensajes</h3>
 <div class="campo"><label for="ajSaludo">Saludo del bot (escribí {nombre} donde va el nombre del local)</label><textarea id="ajSaludo" maxlength="900"></textarea></div>
 <div class="campo"><label for="ajFuera">Aviso cuando está fuera de horario</label><textarea id="ajFuera" maxlength="300"></textarea></div>
 <div class="campo"><label for="ajResena">Link para que te califiquen (Google Maps u otro). Lo usa el botón ⭐ Pedir calificación del chat</label><input type="text" id="ajResena" maxlength="300" placeholder="https://g.page/r/..."></div>
 <div class="campo"><label for="ajRapidas">Respuestas rápidas de los vendedores (una por línea)</label><textarea id="ajRapidas"></textarea></div>
 <p><button id="ajGuardar">Guardar ajustes</button></p>
 <h3>Equipo: un usuario por vendedor</h3>
 <p class="suave">Cada vendedor entra con su usuario y clave, y en el chat se ve quién contestó. Vos entrás con la clave del administrador (usuario vacío o «admin»).</p>
 <div id="ajUsuarios"></div>
 <div class="campo"><label for="usrUsuario">Usuario nuevo (minúsculas, sin espacios)</label><input type="text" id="usrUsuario" maxlength="30" autocapitalize="none"></div>
 <div class="campo"><label for="usrNombre">Nombre de la persona</label><input type="text" id="usrNombre" maxlength="40"></div>
 <div class="campo"><label for="usrClave">Clave (mínimo 6 caracteres)</label><input type="password" id="usrClave" maxlength="100" autocomplete="new-password"></div>
 <div class="campo"><label for="usrRol">Permisos</label><select id="usrRol"><option value="vendedor">Vendedor (solo chats y catálogo)</option><option value="admin">Administrador (también ajustes y equipo)</option></select></div>
 <button class="sec" id="usrCrear">Crear usuario</button>
 <h3>Copia de seguridad</h3>
 <p class="suave">Descarga todos los chats, productos y ajustes en un archivo. No incluye las fotos ni las claves. Conviene hacerlo de vez en cuando.</p>
 <button class="sec" id="btnBackup">💾 Descargar copia</button>
 <div class="acciones"><button class="sec" id="cerrarAj">Cerrar</button></div>
</div></div>
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
function nota(freq, inicio, dur, vol) {
  const o = audioCtx.createOscillator(), g = audioCtx.createGain();
  o.type = "sine"; o.frequency.value = freq;
  const t0 = audioCtx.currentTime + inicio;
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(vol, t0 + 0.012);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  o.connect(g); g.connect(audioCtx.destination);
  o.start(t0); o.stop(t0 + dur + 0.02);
}
function pitido(tipo) {
  if (!sonidoOn || !audioCtx) return;
  try {
    if (audioCtx.state === "suspended") audioCtx.resume();
    if (tipo === "alerta") { nota(988, 0, 0.16, 0.3); nota(1319, 0.14, 0.16, 0.3); nota(1568, 0.28, 0.3, 0.3); }
    else { nota(1046, 0, 0.1, 0.25); nota(1397, 0.09, 0.22, 0.25); }
  } catch (e) {}
}
function pintarBotonSonido() { $("btnSonido").textContent = sonidoOn ? "🔔 Sonido: sí" : "🔕 Sonido: no"; }
$("btnSonido").addEventListener("click", () => {
  sonidoOn = !sonidoOn;
  try { localStorage.setItem("sonido", sonidoOn ? "1" : "0"); } catch (e) {}
  if (sonidoOn) { iniciarAudio(); pitido("mensaje"); }
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
  if (previo) {
    if (pend > previo.pend) pitido("alerta");
    else if (aAtender > previo.aAtender) pitido("mensaje");
  }
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
    else if (c.humano) marca = "👤 " + (c.atiende ? c.atiende + ": " : "");
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
  $("btnProd").disabled = !ventanaAbierta || enviando;
}
const NOMBRES = { cliente: "Cliente", bot: "Bot", vendedor: "Vendedor" };
function pintarMensaje(m, box) {
  const b = el("div", "msg " + m.quien);
  b.appendChild(el("span", "etq", (NOMBRES[m.quien] || m.quien) + (m.autor ? " · " + m.autor : "")));
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
    $("estado").textContent = d.humano
      ? (d.pide_vendedor && !d.atiende ? "🔔 Espera vendedor · el bot sigue con horarios y productos"
         : (d.atiende ? "👤 Atiende " + d.atiende : "👤 Atiende una persona"))
      : "🤖 Atiende el bot";
    $("btnAceptar").classList.toggle("oculto", !(d.humano && d.pide_vendedor));
    $("devolver").classList.toggle("oculto", !d.humano);
    $("btnExcel").classList.toggle("oculto", !d.pedidos);
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
/* ---------- mandar un producto del catálogo desde el chat ---------- */
let prodDatos = [];
function pintarProdSel() {
  const caja = $("prodLista");
  vaciar(caja);
  const q = normalizarJs($("prodBuscar").value);
  const lista = prodDatos.filter(p => !q || normalizarJs(p.nombre + " " + p.categoria + " " + p.palabras).includes(q));
  if (!lista.length) caja.appendChild(el("p", "suave", prodDatos.length ? "No hay productos con ese nombre." : "El catálogo está vacío."));
  lista.slice(0, 60).forEach(p => {
    const fila = el("div", "prodsel" + (p.activo ? "" : " pausado"));
    if (p.foto) { const im = el("img", "miniatura"); im.src = p.foto; im.alt = ""; fila.appendChild(im); }
    else fila.appendChild(el("div", "miniatura", "—"));
    const info = el("div", "info");
    info.appendChild(el("b", "", p.nombre));
    const sub = el("div", "suave", (p.categoria || "") + (p.activo ? "" : " · ⏸ pausado"));
    info.appendChild(sub);
    fila.appendChild(info);
    const b = el("button", "sec mini", "Enviar");
    b.addEventListener("click", async () => {
      if (!actual || enviando) return;
      if (!p.activo && !confirm(p.nombre + " está pausado (no lo tenemos). ¿Mandarlo igual?")) return;
      enviando = true; actualizarControles(); b.disabled = true;
      try {
        const r = await api("/api/enviar_producto", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ numero: actual, clave: p.clave }) });
        if (r.ok && r.data && r.data.ok) toast("Enviado: " + p.nombre);
        else toast("No se pudo enviar: " + ((r.data && r.data.error) || "error"), true);
      } catch (e) { if (e.message !== "sin sesión") toast("Error de conexión.", true); }
      enviando = false; actualizarControles(); b.disabled = false;
      cargarChat(true).catch(() => {}); cargarLista().catch(() => {});
    });
    fila.appendChild(b);
    caja.appendChild(fila);
  });
}
$("btnProd").addEventListener("click", async () => {
  const abrir = $("selProd").classList.contains("oculto");
  $("selProd").classList.toggle("oculto", !abrir);
  if (!abrir) return;
  $("emojis").classList.add("oculto");
  const r = await api("/api/catalogo");
  if (r.ok && Array.isArray(r.data)) { prodDatos = r.data; pintarProdSel(); $("prodBuscar").focus(); }
  else toast("No se pudo cargar el catálogo.", true);
});
$("prodBuscar").addEventListener("input", pintarProdSel);
$("btnAceptar").addEventListener("click", async () => {
  if (!actual) return;
  const r = await api("/api/aceptar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ numero: actual }) });
  if (!(r.ok && r.data && r.data.ok)) { toast("No se pudo aceptar", true); return; }
  toast("Listo, ahora la atendés vos. El bot se calla.");
  await cargarChat(false).catch(() => {}); cargarLista().catch(() => {});
});
$("btnExcel").addEventListener("click", () => { if (actual) window.location.href = "/api/pedidos/" + actual + "/excel"; });
const EMOJIS = "😀 😃 😄 😁 😊 🙂 😉 😍 🤗 😎 🤝 🙌 👍 👌 👏 🙏 💪 ✅ ❌ ⚠️ ⭐ 🔥 ❤️ 💙 🎉 😅 😂 🤔 😢 🙁 😮 👋 👉 👇 📍 🕒 📞 📦 🧱 🛠️ 🔧 🔨 🪛 🪚 🧰 🔩 🚚 💳 💵 🏠".split(" ");
EMOJIS.forEach(e => {
  const b = el("button", "", e); b.type = "button";
  b.addEventListener("click", () => {
    const ta = $("texto"), a = ta.selectionStart ?? ta.value.length, z = ta.selectionEnd ?? a;
    ta.value = ta.value.slice(0, a) + e + ta.value.slice(z);
    ta.focus(); ta.selectionStart = ta.selectionEnd = a + e.length;
  });
  $("emojis").appendChild(b);
});
$("btnEmoji").addEventListener("click", () => { $("emojis").classList.toggle("oculto"); $("selProd").classList.add("oculto"); });
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
document.addEventListener("keydown", e => { if (e.key === "Escape") { $("modal").classList.add("oculto"); $("modalCat").classList.add("oculto"); $("modalAj").classList.add("oculto"); } });

/* ---------- catálogo ---------- */
let fotosEd = [], quitarEd = new Set();
function pintarFotosEd() {
  const caja = $("catFotos");
  vaciar(caja);
  fotosEd.forEach((f, i) => {
    if (quitarEd.has(f.nombre)) return;
    const d = el("div", "fotoed");
    const im = el("img"); im.src = f.url; im.alt = "";
    const x = el("button", "", "✕"); x.type = "button"; x.title = "Quitar esta foto";
    x.addEventListener("click", () => { quitarEd.add(f.nombre); pintarFotosEd(); });
    d.appendChild(im);
    if (f.propia !== false) d.appendChild(x);
    if (i === 0) d.appendChild(el("div", "princ", "principal"));
    caja.appendChild(d);
  });
}
function limpiarFormCat() {
  $("catNombre").value = ""; $("catPalabras").value = ""; $("catTexto").value = ""; $("catFoto").value = ""; $("catCategoria").value = "";
  $("catTitulo").textContent = "Agregar producto";
  fotosEd = []; quitarEd = new Set(); pintarFotosEd();
}
let catDatos = [];
function pintarCatalogo() {
  const caja = $("catLista");
  vaciar(caja);
  const q = normalizarJs($("catBuscar").value), filtro = $("catFiltro").value;
  const lista = catDatos.filter(p => {
    if (filtro === "__pausados") { if (p.activo) return false; }
    else if (filtro === "__sin") { if (p.categoria) return false; }
    else if (filtro && p.categoria !== filtro) return false;
    return !q || normalizarJs(p.nombre + " " + p.categoria + " " + p.palabras).includes(q);
  });
  $("catResumen").textContent = lista.length === catDatos.length
    ? catDatos.length + " productos" : lista.length + " de " + catDatos.length + " productos";
  if (!lista.length) caja.appendChild(el("p", "suave", catDatos.length ? "No hay productos con ese filtro." : "Todavía no hay productos."));
  lista.forEach(p => {
    const fila = el("div", "prodcat" + (p.activo ? "" : " pausado"));
    if (p.foto) { const im = el("img", "miniatura"); im.src = p.foto; im.alt = ""; fila.appendChild(im); }
    else fila.appendChild(el("div", "miniatura", "—"));
    const info = el("div", "info");
    info.appendChild(el("b", "", p.nombre));
    const sub = el("div", "suave");
    if (p.categoria) sub.appendChild(el("span", "etqcat", p.categoria));
    if (!p.activo) sub.appendChild(el("span", "etqpausa", "⏸ Pausado: no lo ofrece el bot"));
    if (!p.categoria && p.activo) sub.appendChild(document.createTextNode(p.fuente === "panel" ? "Sin categoría" : "Sin categoría · viene de GitHub"));
    info.appendChild(sub);
    fila.appendChild(info);
    const ed = el("button", "sec mini", "Editar");
    ed.addEventListener("click", () => {
      $("catNombre").value = p.nombre; $("catPalabras").value = p.palabras; $("catTexto").value = p.texto;
      $("catCategoria").value = p.categoria || "";
      $("catTitulo").textContent = "Editando: " + p.nombre + " (podés sumar o quitar fotos)";
      fotosEd = p.fotos || []; quitarEd = new Set(); pintarFotosEd();
      $("catNombre").focus();
    });
    const pa = el("button", "sec mini", p.activo ? "Pausar" : "Activar");
    pa.title = p.activo ? "No lo tenemos por ahora: el bot deja de ofrecerlo (no se borra)" : "Volver a ofrecerlo";
    pa.addEventListener("click", async () => {
      const rr = await api("/api/catalogo/activo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clave: p.clave, activo: !p.activo }) });
      if (!(rr.ok && rr.data && rr.data.ok)) toast((rr.data && rr.data.error) || "No se pudo cambiar", true);
      cargarCatalogo().catch(() => {});
    });
    const bo = el("button", "sec mini", "Borrar");
    bo.addEventListener("click", async () => {
      if (!confirm("¿Borrar " + p.nombre + " del catálogo? (Si solo no tenés stock, usá Pausar.)")) return;
      const rr = await api("/api/catalogo/borrar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clave: p.clave }) });
      if (!(rr.ok && rr.data && rr.data.ok)) toast((rr.data && rr.data.error) || "No se pudo borrar", true);
      cargarCatalogo().catch(() => {});
    });
    fila.appendChild(ed); fila.appendChild(pa); fila.appendChild(bo);
    caja.appendChild(fila);
  });
}
function normalizarJs(t) { return (t || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, ""); }
async function cargarCatalogo() {
  const r = await api("/api/catalogo");
  if (!r.ok || !Array.isArray(r.data)) { vaciar($("catLista")); $("catLista").appendChild(el("p", "suave", "No se pudo cargar el catálogo.")); return; }
  catDatos = r.data;
  const cats = [...new Set(catDatos.map(p => p.categoria).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const actual = $("catFiltro").value;
  vaciar($("catFiltro")); vaciar($("catCategorias"));
  [["", "Todas las categorías"], ...cats.map(c => [c, c]), ["__sin", "Sin categoría"], ["__pausados", "⏸ Pausados"]].forEach(([v, t]) => {
    const o = el("option", "", t); o.value = v; $("catFiltro").appendChild(o);
  });
  cats.forEach(c => { const o = el("option"); o.value = c; $("catCategorias").appendChild(o); });
  $("catFiltro").value = [...$("catFiltro").options].some(o => o.value === actual) ? actual : "";
  pintarCatalogo();
}
$("catBuscar").addEventListener("input", pintarCatalogo);
$("catFiltro").addEventListener("change", pintarCatalogo);
async function guardarProducto() {
  const nombre = $("catNombre").value.trim();
  if (!nombre) { toast("Poné el nombre del producto", true); return; }
  $("catGuardar").disabled = true;
  try {
    const fd = new FormData();
    fd.append("nombre", nombre);
    fd.append("palabras", $("catPalabras").value.trim());
    fd.append("categoria", $("catCategoria").value.trim());
    fd.append("texto", $("catTexto").value.trim());
    const archivos = Array.from($("catFoto").files);
    for (const f of archivos) fd.append("fotos", await reducirImagen(f), "foto.jpg");
    fd.append("quitar", JSON.stringify(Array.from(quitarEd)));
    const r = await api("/api/catalogo", { method: "POST", body: fd });
    if (r.ok && r.data && r.data.ok) { toast("Listo: el bot ya reconoce \"" + nombre + "\""); limpiarFormCat(); await cargarCatalogo(); }
    else toast("No se pudo guardar: " + ((r.data && r.data.error) || "error desconocido"), true);
  } catch (e) { if (e.message !== "sin sesión") toast("Error de conexión.", true); }
  $("catGuardar").disabled = false;
}
$("btnCatalogo").addEventListener("click", () => { $("modalCat").classList.remove("oculto"); cargarCatalogo().catch(() => {}); });
$("cerrarCat").addEventListener("click", () => $("modalCat").classList.add("oculto"));
$("catGuardar").addEventListener("click", guardarProducto);

/* ---------- ajustes, equipo y copia (solo administrador) ---------- */
const DIAS_CORTOS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
let turnosEd = [];
function campoTexto(clase, valor, ph, max, tipo) {
  const i = el("input", clase); i.type = tipo || "text"; i.value = valor || ""; i.placeholder = ph || ""; i.maxLength = max || 100;
  return i;
}
function pintarTurnos() {
  const caja = $("ajTurnos"); vaciar(caja);
  turnosEd.forEach((t, idx) => {
    const fila = el("div", "turno");
    const nom = campoTexto("", t.nombre, "Nombre (ej: Lunes a Viernes)", 40);
    nom.addEventListener("input", () => { t.nombre = nom.value; });
    fila.appendChild(nom);
    const dias = el("div", "dias");
    DIAS_CORTOS.forEach((d, n) => {
      const l = el("label"); const c = el("input"); c.type = "checkbox"; c.checked = t.dias.includes(n);
      c.addEventListener("change", () => { t.dias = t.dias.filter(x => x !== n); if (c.checked) t.dias.push(n); });
      l.appendChild(c); l.appendChild(document.createTextNode(d)); dias.appendChild(l);
    });
    fila.appendChild(dias);
    const horas = el("div", "horas");
    const d1 = campoTexto("", t.desde, "", 5, "time"); d1.addEventListener("input", () => { t.desde = d1.value; });
    const h1 = campoTexto("", t.hasta, "", 5, "time"); h1.addEventListener("input", () => { t.hasta = h1.value; });
    horas.appendChild(document.createTextNode("Abre ")); horas.appendChild(d1);
    horas.appendChild(document.createTextNode(" cierra ")); horas.appendChild(h1);
    const q = el("button", "sec mini", "Quitar");
    q.addEventListener("click", () => { turnosEd.splice(idx, 1); pintarTurnos(); });
    horas.appendChild(q);
    fila.appendChild(horas);
    caja.appendChild(fila);
  });
}
function fechaLinda(iso) { const p = iso.split("-"); return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : iso; }
async function cargarAjustes() {
  const r = await api("/api/ajustes");
  if (!r.ok || !r.data) { toast("No se pudieron cargar los ajustes.", true); return; }
  const a = r.data;
  $("ajNombre").value = a.nombre || ""; $("ajDireccion").value = a.direccion || ""; $("ajPago").value = a.medios_pago || "";
  $("ajEnvios").value = a.envios || ""; $("ajCerrado").value = a.cerrado || ""; $("ajSaludo").value = a.saludo || "";
  $("ajFuera").value = a.fuera_horario || ""; $("ajResena").value = a.link_resena || ""; $("ajRapidas").value = (a.rapidas || []).join("\n");
  $("ajFeriados").value = (a.feriados || []).map(fechaLinda).join("\n");
  turnosEd = (a.turnos || []).map(t => ({ nombre: t.nombre, dias: t.dias.slice(), desde: t.desde, hasta: t.hasta }));
  pintarTurnos();
}
async function guardarAjustes() {
  $("ajGuardar").disabled = true;
  try {
    const r = await api("/api/ajustes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        nombre: $("ajNombre").value, direccion: $("ajDireccion").value, medios_pago: $("ajPago").value,
        envios: $("ajEnvios").value, cerrado: $("ajCerrado").value, saludo: $("ajSaludo").value,
        fuera_horario: $("ajFuera").value, link_resena: $("ajResena").value, rapidas: $("ajRapidas").value, feriados: $("ajFeriados").value,
        turnos: turnosEd
      })
    });
    if (r.ok && r.data && r.data.ok) { toast("Guardado. El bot ya usa los datos nuevos."); await cargarAjustes(); refrescarRapidas().catch(() => {}); }
    else toast((r.data && r.data.error) || "No se pudo guardar.", true);
  } catch (e) { if (e.message !== "sin sesión") toast("Error de conexión.", true); }
  $("ajGuardar").disabled = false;
}
async function cargarUsuarios() {
  const caja = $("ajUsuarios"); vaciar(caja);
  const r = await api("/api/usuarios");
  if (!r.ok || !Array.isArray(r.data)) return;
  if (!r.data.length) caja.appendChild(el("p", "suave", "Todavía no hay vendedores cargados."));
  r.data.forEach(u => {
    const f = el("div", "usr");
    const info = el("div", "info");
    info.appendChild(el("b", "", u.nombre + " (" + u.usuario + ")"));
    info.appendChild(el("div", "suave", (u.rol === "admin" ? "Administrador" : "Vendedor") + (u.activo ? "" : " · desactivado")));
    f.appendChild(info);
    const cl = el("button", "sec mini", "Cambiar clave");
    cl.addEventListener("click", async () => {
      const nueva = prompt("Clave nueva para " + u.nombre + " (mínimo 6 caracteres):");
      if (!nueva) return;
      const rr = await api("/api/usuarios/" + encodeURIComponent(u.usuario) + "/clave", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clave: nueva }) });
      toast(rr.ok && rr.data && rr.data.ok ? "Clave cambiada." : ((rr.data && rr.data.error) || "No se pudo cambiar."), !(rr.ok && rr.data && rr.data.ok));
    });
    const ac = el("button", "sec mini", u.activo ? "Desactivar" : "Activar");
    ac.addEventListener("click", async () => {
      await api("/api/usuarios/" + encodeURIComponent(u.usuario) + "/activo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ activo: !u.activo }) });
      cargarUsuarios().catch(() => {});
    });
    f.appendChild(cl); f.appendChild(ac);
    caja.appendChild(f);
  });
}
async function crearUsuario() {
  const r = await api("/api/usuarios", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ usuario: $("usrUsuario").value, nombre: $("usrNombre").value, clave: $("usrClave").value, rol: $("usrRol").value })
  });
  if (r.ok && r.data && r.data.ok) {
    toast("Usuario creado."); $("usrUsuario").value = ""; $("usrNombre").value = ""; $("usrClave").value = "";
    cargarUsuarios().catch(() => {});
  } else toast((r.data && r.data.error) || "No se pudo crear el usuario.", true);
}
$("btnAjustes").addEventListener("click", () => { $("modalAj").classList.remove("oculto"); cargarAjustes().catch(() => {}); cargarUsuarios().catch(() => {}); });
$("cerrarAj").addEventListener("click", () => $("modalAj").classList.add("oculto"));
$("ajAgregarTurno").addEventListener("click", () => { turnosEd.push({ nombre: "", dias: [], desde: "09:00", hasta: "13:00" }); pintarTurnos(); });
$("ajGuardar").addEventListener("click", guardarAjustes);
$("usrCrear").addEventListener("click", () => { crearUsuario().catch(() => {}); });
$("btnBackup").addEventListener("click", () => { window.location.href = "/api/backup"; });

/* ---------- mensajes rápidos (se vuelven a cargar al guardar los ajustes, así el link de calificación se actualiza) ---------- */
function pintarRapidas(cfg) {
  const caja = $("rapidas");
  vaciar(caja);
  const frases = [cfg.resena, ...(cfg.rapidas || [])].filter(Boolean);
  frases.forEach((t, i) => {
    const b = el("button", "chip", i === 0 && cfg.resena ? "⭐ Pedir calificación" : (t.length > 30 ? t.slice(0, 30) + "…" : t));
    b.title = t;
    b.addEventListener("click", () => {
      const ta = $("texto");
      ta.value = (ta.value ? ta.value + " " : "") + t;
      ta.focus();
    });
    caja.appendChild(b);
  });
  caja.classList.toggle("oculto", !caja.children.length);
}
async function refrescarRapidas() {
  const r = await api("/api/config");
  if (r.ok && r.data) pintarRapidas(r.data);
}

/* ---------- arranque ---------- */
api("/api/config").then(r => {
  if (!r.ok || !r.data) return;
  $("quienSoy").textContent = "👤 " + (r.data.nombre || "");
  if (r.data.rol === "admin") $("btnAjustes").classList.remove("oculto");
  $("modoDb").textContent = r.data.db === "supabase" ? "🟢 Datos guardados en Supabase" : "⚠️ Base temporal: se borra al reiniciar";
  pintarRapidas(r.data);
}).catch(() => {});
cargarLista().catch(() => {});
setInterval(() => { cargarLista().catch(() => {}); cargarChat(false).catch(() => {}); }, 4000);
})();
</script>
</body></html>"""


@app.route("/panel", strict_slashes=False)
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