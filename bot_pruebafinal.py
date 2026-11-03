"""
Bot de WhatsApp de la ferretería + panel web para vendedores  (versión 4 con Supabase)
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

# =====================================================================
# 3) BASE DE DATOS (Supabase PostgreSQL con Pool)
# =====================================================================
class DictRow:
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
    numero             TEXT PRIMARY KEY,
    nombre             TEXT NOT NULL DEFAULT '',
    perfil             TEXT NOT NULL DEFAULT '',
    humano             INTEGER NOT NULL DEFAULT 0,
    pide_vendedor      INTEGER NOT NULL DEFAULT 0,
    pide_desde         DOUBLE PRECISION NOT NULL DEFAULT 0,
    no_leidos          INTEGER NOT NULL DEFAULT 0,
    fallos             INTEGER NOT NULL DEFAULT 0,
    ultimo             DOUBLE PRECISION NOT NULL DEFAULT 0,
    ultimo_cliente     DOUBLE PRECISION NOT NULL DEFAULT 0,
    ult_texto          TEXT NOT NULL DEFAULT '',
    creada             DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mensajes (
    id                 SERIAL PRIMARY KEY,
    numero             TEXT NOT NULL,
    quien              TEXT NOT NULL,
    tipo               TEXT NOT NULL DEFAULT 'text',
    texto              TEXT NOT NULL DEFAULT '',
    archivo            TEXT NOT NULL DEFAULT '',
    nombre_archivo     TEXT NOT NULL DEFAULT '',
    ts                 DOUBLE PRECISION NOT NULL,
    wamid              TEXT NOT NULL DEFAULT '',
    estado             TEXT NOT NULL DEFAULT '',
    error              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mensajes_numero ON mensajes (numero, id);
CREATE INDEX IF NOT EXISTS idx_mensajes_wamid ON mensajes (wamid);
CREATE TABLE IF NOT EXISTS vistos (wamid TEXT PRIMARY KEY, ts DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS eventos (
    id        SERIAL PRIMARY KEY,
    ts        DOUBLE PRECISION NOT NULL,
    tipo      TEXT NOT NULL,
    detalle   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos (ts);
"""

@contextmanager
def tx():
    if not db_pool:
        raise RuntimeError("No hay conexión a la base de datos configurada.")
    con = db_pool.getconn()
    try:
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
# 4) UTILIDADES Y FUNCIONES DEL BOT
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

def registrar(numero, quien, tipo="text", texto="", archivo="", nombre_archivo="", wamid="", estado="", error=""):
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
        if quien == "cliente":
            cur.execute(
                "UPDATE conversaciones SET ultimo=%s, ultimo_cliente=%s, no_leidos=no_leidos+1 WHERE numero=%s",
                (t, t, numero),
            )
        else:
            cur.execute("UPDATE conversaciones SET ultimo=%s WHERE numero=%s", (t, numero))
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
        pass

def marcar_visto(wamid):
    with tx() as cur:
        cur.execute("INSERT INTO vistos (wamid, ts) VALUES (%s,%s) ON CONFLICT (wamid) DO NOTHING", (wamid, time.time()))
        return True

def es_primer_mensaje(numero):
    with tx() as cur:
        cur.execute("SELECT COUNT(*) FROM mensajes WHERE numero=%s AND quien!='cliente'", (numero,))
        n = cur.fetchone()[0]
    return n == 0

def sumar_fallo(numero):
    with tx() as cur:
        cur.execute("UPDATE conversaciones SET fallos=fallos+1 WHERE numero=%s", (numero,))
        cur.execute("SELECT fallos FROM conversaciones WHERE numero=%s", (numero,))
        return cur.fetchone()[0]

# =====================================================================
# 5) ENVÍO A WHATSAPP
# =====================================================================
def cabecera():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

def a_destino(numero):
    if QUITAR_9_ARGENTINA and numero.startswith("549"):
        return "54" + numero[3:]
    return numero

def detalle_error(r):
    try:
        return r.json()["error"]["message"]
    except Exception:
        return f"Meta respondió {r.status_code}"

def meta_post(payload):
    try:
        r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/messages", headers=cabecera(), json=payload, timeout=15)
    except requests.RequestException:
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

def decir(numero, texto, quien="bot"):
    ok, wamid, error = enviar_texto(numero, texto)
    registrar(numero, quien, "text", texto, wamid=wamid, estado="sent" if ok else "failed", error=error)
    return ok

def decir_menu(numero, intro):
    ok, wamid, error = enviar_botones(numero, intro + "\n\nElegí una opción 👇")
    if ok:
        registrar(numero, "bot", "text", intro + "\n🔘 Menú", wamid=wamid, estado="sent")
        return
    decir(numero, intro + "\n\n" + MENU_ESCRITO)

# =====================================================================
# 6) LÓGICA DEL BOT Y WEBHOOK
# =====================================================================
PALABRAS_VENDEDOR = ("vendedor", "vendedora", "asesor", "humano", "persona")
PALABRAS_PRECIO = ("precio", "precios", "cuanto sale", "cuanto cuesta", "presupuesto")
PALABRAS_HORARIOS = ("horario", "horarios", "abren", "abre", "abierto")
PALABRAS_SALUDO = ("hola", "buenas", "buen dia")

def responder(numero, texto, primer_mensaje):
    t = normalizar(texto)
    if tiene(t, PALABRAS_VENDEDOR) or tiene(t, PALABRAS_PRECIO):
        marcar(numero, humano=1, pide_vendedor=1)
        return decir(numero, DERIVACION)
    if tiene(t, PALABRAS_HORARIOS):
        return decir(numero, texto_horarios(con_pregunta=True))
    if tiene(t, PALABRAS_SALUDO) or primer_mensaje:
        return decir_menu(numero, SALUDO)
    
    if sumar_fallo(numero) >= MAX_FALLOS:
        marcar(numero, humano=1)
        return decir(numero, DERIVACION_INSISTENCIA)
    decir_menu(numero, "No te entendí del todo 🙈")

def procesar_mensaje(msg):
    numero = str(msg.get("from", ""))
    wamid = msg.get("id", "")
    if not numero_valido(numero) or (wamid and not marcar_visto(wamid)):
        return
    tipo = msg.get("type", "")
    if tipo != "text":
        return
    texto = (msg.get("text") or {}).get("body", "")
    
    with lock_de(numero):
        registrar(numero, "cliente", "text", texto)
        c = conv(numero)
        if c and c["humano"]:
            return
        responder(numero, texto, es_primer_mensaje(numero))

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
    body = request.get_json(silent=True) or {}
    try:
        for entrada in body.get("entry") or []:
            for cambio in entrada.get("changes") or []:
                valor = cambio.get("value") or {}
                for msg in valor.get("messages") or []:
                    threading.Thread(target=procesar_mensaje, args=(msg,), daemon=True).start()
    except Exception:
        pass
    return "ok", 200

# =====================================================================
# 7) PANEL WEB PARA VENDEDORES
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
        '</form></body></html>'
    )

@app.route("/login", methods=["GET"])
def login_get():
    if session.get("auth"):
        return redirect("/panel")
    return pagina_login()

@app.route("/login", methods=["POST"])
def login_post():
    ip = request.remote_addr
    if bloqueado(ip):
        return pagina_login("Demasiados intentos fallidos. Esperá 5 minutos.")
    clave = request.form.get("clave", "")
    if PANEL_PASSWORD and hmac.compare_digest(clave, PANEL_PASSWORD):
        INTENTOS.pop(ip, None)
        session.permanent = True
        session["auth"] = True
        return redirect("/panel")
    
    fallos, desde = INTENTOS.get(ip, (0, time.time()))
    if time.time() - desde >= 300:
        fallos, desde = 0, time.time()
    INTENTOS[ip] = (fallos + 1, desde)
    return pagina_login("Clave incorrecta")

@app.route("/panel")
def panel():
    if not session.get("auth") or not PANEL_PASSWORD:
        return redirect("/login")
    return "<h1>Panel de Control de la Ferretería</h1><p>¡Conectado a Supabase correctamente y funcionando!</p>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)