"""
Bot de WhatsApp de la ferretería + panel web para vendedores  (versión 3)

VARIABLES DE ENTORNO (en Render: Environment)
  WHATSAPP_TOKEN    token permanente de Meta                            (obligatoria)
  PHONE_NUMBER_ID   Phone Number ID del número del bot                  (obligatoria)
  VERIFY_TOKEN      la clave del webhook (la misma que pusiste en Meta) (obligatoria)
  PANEL_PASSWORD    clave para entrar al panel de vendedores          (obligatoria)
  APP_SECRET        "Clave secreta de la app" de Meta                   (muy recomendada)
                    Meta > tu app > Configuración de la app > Básica
  SECRET_KEY        texto largo al azar para las sesiones               (opcional)
  DATA_DIR          carpeta donde se guardan mensajes y archivos        (opcional)
                    Con disco persistente de Render: /data

ARCHIVOS
  catalogo/         fotos de productos (arcilla.jpg, thinner.jpg, etc.). Se suben a GitHub junto con este archivo.
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
# 1) DATOS DE LA FERRETERÍA  ← horarios actualizados
# =====================================================================
NOMBRE_NEGOCIO = "Ferretería O'Higgins"

# Días: 0 = lunes ... 6 = domingo. Horas en formato 24 hs.
TURNOS = [
    ("Lunes a Viernes", (0, 1, 2, 3, 4), "08:30", "19:30"),
    ("Sábados", (5,), "09:00", "13:00"),
]
DIAS_CERRADO = "Domingos"
FERIADOS = []  # fechas en que no abrís, formato "AAAA-MM-DD"

# Textos automáticos del bot:
DIRECCION = (
    "📍 *¡Te esperamos en el local!* Estamos ubicados sobre Av. O'Higgins (Salta).\n\n"
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

# Productos con info y foto en la carpeta "catalogo/". Sin precios a propósito.
PRODUCTOS = {
    "arcilla": {
        "nombre": "Arcilla",
        "palabras": ("arcilla", "arcillas"),
        "foto": "arcilla.jpg",
        "texto": (
            "🧱 *Arcilla baja temperatura / común* 🛠️\n\n"
            "Viene en presentación lista para usar.\n\n"
            "Para consultar cantidades o precio, escribí *VENDEDOR*."
        ),
    },
    "thinner": {
        "nombre": "Thinner",
        "palabras": ("thinner", "tiner", "disolvente"),
        "foto": "thinner.jpg",
        "texto": (
            "🧪 *Thinner / Disolvente* 🛠️\n\n"
            "Ideal para limpieza y dilución de pinturas. Excelente calidad.\n\n"
            "Para consultar stock, tamaños o precios, escribí *VENDEDOR*."
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
    log.warning("⚠️ Falta APP_SECRET en Render.")
if not PANEL_PASSWORD:
    log.warning("⚠️ Falta PANEL_PASSWORD en Render.")


# =====================================================================
# 3) BASE DE DATOS (SQLite)
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversaciones (
    numero           TEXT PRIMARY KEY,
    nombre           TEXT NOT NULL DEFAULT '',
    perfil           TEXT NOT NULL DEFAULT '',
    humano           INTEGER NOT NULL DEFAULT 0,
    pide_vendedor    INTEGER NOT NULL DEFAULT 0,
    pide_desde       REAL NOT NULL DEFAULT 0,
    no_leidos        INTEGER NOT NULL DEFAULT 0,
    fallos           INTEGER NOT NULL DEFAULT 0,
    ultimo           REAL NOT NULL DEFAULT 0,
    ultimo_cliente   REAL NOT NULL DEFAULT 0,
    ult_texto        TEXT NOT NULL DEFAULT '',
    creada           REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mensajes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    numero           TEXT NOT NULL,
    quien            TEXT NOT NULL,
    tipo             TEXT NOT NULL DEFAULT 'text',
    texto            TEXT NOT NULL DEFAULT '',
    archivo          TEXT NOT NULL DEFAULT '',
    nombre_archivo   TEXT NOT NULL DEFAULT '',
    ts               REAL NOT NULL,
    wamid            TEXT NOT NULL DEFAULT '',
    estado           TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mensajes_numero ON mensajes (numero, id);
CREATE INDEX IF NOT EXISTS idx_mensajes_wamid ON mensajes (wamid);
CREATE TABLE IF NOT EXISTS vistos (wamid TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS eventos (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    tipo   TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos (ts);
"""


@contextmanager
def tx():
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
        return "El token de WhatsApp venció o no es válido: hay que generar uno nuevo en Meta."
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
PALABRAS_ENVIO = ("envio", "envios", "delivery", "llevan", "mandan", "alcansas", "alcanzan")
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
    return "ok", 200


# =====================================================================
# 9) ACCESO AL PANEL
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
        f"{aviso}"
        '<input type="password" name="clave" placeholder="Clave del panel" '
        'autofocus required style="width:100%;padding:10px;font-size:16px;box-sizing:border-box">'
        '<button style="margin-top:12px;width:100%;padding:10px;background:#00a884;'
        'color:#fff;border:none;border-radius:6px;font-size:16px;cursor:pointer">Ingresar</button>'
        "</form></body></html>"
    )


PANEL_HTML = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-__NONCE__'; style-src 'nonce-__NONCE__'; img-src 'self' blob: data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'">
<title>Panel Ferretería</title>
<style nonce="__NONCE__">
body { font-family: system-ui, sans-serif; margin: 0; background: #efeae2; display: flex; height: 100vh; color: #111; }
#lista { width: 340px; background: #fff; border-right: 1px solid #ddd; display: flex; flex-direction: column; }
#chat { flex: 1; display: flex; flex-direction: column; background: #efeae2; }
.header { padding: 14px 16px; background: #f0f2f5; font-weight: bold; border-bottom: 1px solid #ddd; display: flex; justify-content: space-between; align-items: center; }
.conversaciones { overflow-y: auto; flex: 1; }
.item { padding: 12px 16px; border-bottom: 1px solid #f0f2f5; cursor: pointer; display: flex; justify-content: space-between; align-items: flex-start; }
.item:hover { background: #f5f6f6; }
.item.activo { background: #f0f2f5; }
.item .num { font-weight: bold; font-size: 15px; }
.item .txt { color: #666; font-size: 13px; margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px; }
.badge { background: #25d366; color: white; border-radius: 50%; padding: 2px 6px; font-size: 11px; font-weight: bold; }
.campana { color: #b00020; font-weight: bold; font-size: 13px; display: inline-block; animation: pulse 1s infinite; }
@keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
.mensajes { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 8px; }
.msg { max-width: 65%; padding: 8px 12px; border-radius: 8px; font-size: 14px; line-height: 1.4; position: relative; word-break: break-word; }
.msg.cliente { background: #fff; align-self: flex-start; box-shadow: 0 1px 0.5px rgba(0,0,0,0.13); }
.msg.vendedor, .msg.bot { background: #d9fdd3; align-self: flex-end; box-shadow: 0 1px 0.5px rgba(0,0,0,0.13); }
.msg .ts { font-size: 11px; color: #999; text-align: right; margin-top: 4px; }
.input-area { padding: 12px; background: #f0f2f5; display: flex; gap: 8px; align-items: center; border-top: 1px solid #ddd; }
.input-area textarea { flex: 1; padding: 10px; border: 1px solid #ddd; border-radius: 8px; resize: none; height: 20px; font-family: inherit; font-size: 14px; }
.input-area button { background: #00a884; color: white; border: none; padding: 10px 16px; border-radius: 8px; cursor: pointer; font-weight: bold; }
.input-area button:hover { background: #008f72; }
.respuestas-rapidas { padding: 8px 12px; background: #e9edef; display: flex; gap: 6px; overflow-x: auto; border-top: 1px solid #ddd; }
.chip { background: #fff; border: 1px solid #d1d7db; border-align: center; border-radius: 16px; padding: 6px 12px; font-size: 12px; cursor: pointer; white-space: nowrap; }
.chip:hover { background: #f0f2f5; }
.info-bar { padding: 8px 16px; background: #fff; border-bottom: 1px solid #ddd; display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: #666; }
</style>
</head>
<body>
<div id="lista">
    <div class="header">📋 Chats <a href="/logout" style="font-size:12px;color:#00a884;text-decoration:none">Salir</a></div>
    <div class="conversaciones" id="conversaciones"></div>
</div>
<div id="chat">
    <div class="header" id="chat-header">Seleccioná un chat a la izquierda</div>
    <div class="info-bar" id="info-bar" style="display:none">
        <span id="estado-humano">Atendido por el bot</span>
        <button id="btn-toggle" onclick="toggleHumano()" style="padding:4px 8px;font-size:12px;cursor:pointer"></button>
    </div>
    <div class="mensajes" id="mensajes"></div>
    <div class="respuestas-rapidas" id="chips"></div>
    <div class="input-area" id="input-area" style="display:none">
        <textarea id="texto" placeholder="Escribí una respuesta..." rows="1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();enviar();}"></textarea>
        <button onclick="enviar()">Enviar</button>
    </div>
</div>
<script nonce="__NONCE__">
let actual = null;
let chats = [];

async function cargarChats() {
    try {
        let r = await fetch('/api/chats');
        if (!r.ok) return;
        chats = await r.json();
        let html = '';
        for (let c of chats) {
            let activo = c.numero === actual ? ' activo' : '';
            let badge = c.no_leidos > 0 ? `<span class="badge">${c.no_leidos}</span>` : '';
            let campana = c.pide_vendedor ? `<span class="campana">🔔</span> ` : '';
            let nombre = c.nombre ? `${c.nombre} (${c.numero})` : c.numero;
            html += `<div class="item${activo}" onclick="sel('${c.numero}')">
                <div>
                    <div class="num">${campana}${nombre}</div>
                    <div class="txt">${c.ult_texto || ''}</div>
                </div>
                <div>${badge}</div>
            </div>`;
        }
        document.getElementById('conversaciones').innerHTML = html;
    } catch(e) {}
}

async function sel(num) {
    actual = num;
    document.getElementById('info-bar').style.display = 'flex';
    document.getElementById('input-area').style.display = 'flex';
    await cargarMensajes();
    cargarChats();
}

async function cargarMensajes() {
    if (!actual) return;
    try {
        let r = await fetch(`/api/mensajes/${actual}`);
        if (!r.ok) return;
        let data = await r.json();
        
        let c = chats.find(x => x.numero === actual);
        if (c) {
            let nombre = c.nombre ? `${c.nombre} (${c.numero})` : c.numero;
            document.getElementById('chat-header').innerText = nombre;
            let humano = c.humano;
            document.getElementById('estado-humano').innerText = humano ? "👤 Atendido por una persona (bot silenciado)" : "🤖 Atendido por el bot";
            let btn = document.getElementById('btn-toggle');
            btn.innerText = humano ? "Volver a activar Bot" : "Pasar a Humano";
            btn.style.background = humano ? "#25d366" : "#b00020";
            btn.style.color = "#fff";
            btn.style.border = "none";
            btn.style.borderRadius = "4px";
        }

        let html = '';
        for (let m of data.mensajes) {
            let contenido = escapeHtml(m.texto);
            if (m.archivo) {
                if (m.archivo.startsWith('c/')) {
                    let fotoUrl = '/catalogo/' + m.archivo.substring(2);
                    contenido += `<br><img src="${fotoUrl}" style="max-width:200px;border-radius:6px;margin-top:6px">`;
                } else if (m.archivo.startsWith('m/')) {
                    let mediaUrl = '/media/' + m.archivo.substring(2);
                    if (m.tipo === 'image') contenido += `<br><img src="${mediaUrl}" style="max-width:200px;border-radius:6px;margin-top:6px">`;
                    else contenido += `<br><a href="${mediaUrl}" target="_blank">📄 Ver archivo adjunto</a>`;
                }
            }
            html += `<div class="msg ${m.quien}">
                <div>${contenido}</div>
                <div class="ts">${m.ts_fmt}</div>
            </div>`;
        }
        let box = document.getElementById('mensajes');
        let abajo = box.scrollTop + box.clientHeight >= box.scrollHeight - 50;
        box.innerHTML = html;
        if (abajo) box.scrollTop = box.scrollHeight;
    } catch(e) {}
}

async function enviar(txt) {
    let texto = txt || document.getElementById('texto').value.trim();
    if (!texto || !actual) return;
    if (!txt) document.getElementById('texto').value = '';
    await fetch('/api/enviar', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({numero: actual, texto: texto})
    });
    cargarMensajes();
    cargarChats();
}

async function toggleHumano() {
    if (!actual) return;
    let c = chats.find(x => x.numero === actual);
    if (!c) return;
    let nuevo = c.humano ? 0 : 1;
    await fetch('/api/humano', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({numero: actual, humano: nuevo})
    });
    cargarChats();
    cargarMensajes();
}

function escapeHtml(text) {
    return (text || '').replace(/&/g, "&").replace(/</g, "<").replace(/>/g, ">");
}

async function cargarRespuestasRapidas() {
    try {
        let r = await fetch('/api/respuestas');
        if (!r.ok) return;
        let resp = await r.json();
        let html = '';
        for (let t of resp) {
            html += `<div class="chip" onclick="enviar('${t.replace(/'/g, "\\'")}')">${t}</div>`;
        }
        document.getElementById('chips').innerHTML = html;
    } catch(e) {}
}

setInterval(() => {
    cargarChats();
    if (actual) cargarMensajes();
}, 4000);

cargarChats();
cargarRespuestasRapidas();
</script>
</body>
</html>
"""


@app.route("/panel/", methods=["GET"])
def panel():
    if not PANEL_PASSWORD:
        return "El panel está desactivado (falta PANEL_PASSWORD en Render)", 403
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


@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr or "127.0.0.1"
    if bloqueado(ip):
        return pagina_login("Demasiados intentos fallidos. Esperá 5 minutos."), 429
    clave = request.form.get("clave", "")
    if PANEL_PASSWORD and hmac.compare_digest(clave, PANEL_PASSWORD):
        INTENTOS.pop(ip, None)
        session.permanent = True
        session["ok"] = True
        return redirect("/panel/")
    fallos, _ = INTENTOS.get(ip, (0, time.time()))
    INTENTOS[ip] = (fallos + 1, time.time())
    return pagina_login("Clave incorrecta"), 403


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect("/panel/")


@app.route("/api/chats", methods=["GET"])
def api_chats():
    if not session.get("ok"):
        return jsonify({"error": "no autorizado"}), 401
    with tx() as con:
        filas = con.execute("SELECT * FROM conversaciones ORDER BY ultimo DESC").fetchall()
    return jsonify([dict(f) for f in filas])


@app.route("/api/mensajes/<numero>", methods=["GET"])
def api_mensajes(numero):
    if not session.get("ok"):
        return jsonify({"error": "no autorizado"}), 401
    with tx() as con:
        con.execute("UPDATE conversaciones SET no_leidos=0 WHERE numero=?", (numero,))
        filas = con.execute("SELECT * FROM mensajes WHERE numero=? ORDER BY id ASC", (numero,)).fetchall()
    res = []
    for f in filas:
        d = dict(f)
        d["ts_fmt"] = fmt(d["ts"])
        res.append(d)
    return jsonify({"mensajes": res})


@app.route("/api/enviar", methods=["POST"])
def api_enviar():
    if not session.get("ok"):
        return jsonify({"error": "no autorizado"}), 401
    d = request.get_json(silent=True) or {}
    numero, texto = str(d.get("numero", "")), str(d.get("texto", "")).strip()
    if not numero_valido(numero) or not texto:
        return jsonify({"error": "datos inválidos"}), 400
    with lock_de(numero):
        ok, wamid, error = enviar_texto(numero, texto)
        registrar(numero, "vendedor", "text", texto, wamid=wamid, estado="sent" if ok else "failed", error=error)
    return jsonify({"ok": ok})


@app.route("/api/humano", methods=["POST"])
def api_humano():
    if not session.get("ok"):
        return jsonify({"error": "no autorizado"}), 401
    d = request.get_json(silent=True) or {}
    numero, humano = str(d.get("numero", "")), int(d.get("humano", 0))
    if not numero_valido(numero):
        return jsonify({"error": "número inválido"}), 400
    marcar(numero, humano=humano, pide_vendedor=0, fallos=0)
    return jsonify({"ok": True})


@app.route("/api/respuestas", methods=["GET"])
def api_respuestas():
    if not session.get("ok"):
        return jsonify({"error": "no autorizado"}), 401
    return jsonify(RESPUESTAS_RAPIDAS)


@app.route("/media/<path:nombre>", methods=["GET"])
def servir_media(nombre):
    if not session.get("ok"):
        abort(401)
    seguro = secure_filename(nombre)
    return send_file(os.path.join(MEDIA_DIR, seguro))


@app.route("/catalogo/<path:nombre>", methods=["catalogo", "GET"])
def servir_catalogo(nombre):
    seguro = secure_filename(nombre)
    return send_file(os.path.join(CATALOGO_DIR, seguro))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))