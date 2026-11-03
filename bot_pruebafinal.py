"""
Bot de WhatsApp de la ferretería + panel web para vendedores  (versión 3)
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
# 1) DATOS DE LA FERRETERÍA
# =====================================================================
NOMBRE_NEGOCIO = "Ferretería O'Higgins"

TURNOS = [
    ("Lunes a Viernes", (0, 1, 2, 3, 4), "08:30", "19:30"),
    ("Sábados", (5,), "09:00", "13:00"),
]
DIAS_CERRADO = "Domingos"
FERIADOS = []

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
        "palabras": ("thinner", "tiner", "disolvente"),
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
SALUDO = f"¡Hola! 👋 Bienvenido a *{NOMBRE_NEGOCIO}*. Es un gusto saludarte.\nSoy el asistente virtual (un bot) y estoy acá para ayudarte rápido."
MENU_ESCRITO = "Escribí lo que necesites:\n• *HORARIOS* para ver cuándo abrimos\n• *PRODUCTOS* para ver info y fotos\n• *VENDEDOR* para hablar con una persona del equipo"
DERIVACION = "🔔 Te acabo de derivar con un vendedor. En un momento te responderá por acá."
DERIVACION_PRECIOS = "Los precios y presupuestos te los pasa una persona del equipo, así son siempre los valores actualizados.\n🔔 Ya te derivé con un vendedor."
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
# 4) UTILIDADES Y LÓGICA DEL BOT
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


def registrar(numero, quien, tipo="text", texto="", archivo="", nombre_archivo="", wamid="", estado="", error=""):
    t = time.time()
    with tx() as con:
        con.execute("INSERT OR IGNORE INTO conversaciones (numero, creada, ultimo) VALUES (?,?,?)", (numero, t, t))
        cur = con.execute(
            "INSERT INTO mensajes (numero, quien, tipo, texto, archivo, nombre_archivo, ts, wamid, estado, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (numero, quien, tipo, texto, archivo, nombre_archivo, t, wamid, estado, error),
        )
        prev = (texto[:80]).replace("\n", " ")
        if quien == "cliente":
            con.execute("UPDATE conversaciones SET ultimo=?, ultimo_cliente=?, no_leidos=no_leidos+1, ult_texto=? WHERE numero=?", (t, t, prev, numero))
        else:
            con.execute("UPDATE conversaciones SET ultimo=?, ult_texto=? WHERE numero=?", (t, prev, numero))
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
            con.execute("INSERT INTO eventos (ts, tipo, detalle) VALUES (?,?,?)", (time.time(), tipo, (detalle or "")[:200]))
    except Exception:
        pass


def marcar_visto(wamid):
    with tx() as con:
        cur = con.execute("INSERT OR IGNORE INTO vistos (wamid, ts) VALUES (?,?)", (wamid, time.time()))
        return cur.rowcount == 1


def es_primer_mensaje(numero):
    with tx() as con:
        return con.execute("SELECT COUNT(*) FROM mensajes WHERE numero=? AND quien!='cliente'", (numero,)).fetchone()[0] == 0


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
        error = f"{e.get('title') or e.get('message') or 'No se pudo'} ({e.get('code', '?')})"
    with tx() as con:
        fila = con.execute("SELECT id, estado FROM mensajes WHERE wamid=?", (wamid,)).fetchone()
        if fila is None:
            return
        if nuevo != "failed" and ORDEN_ESTADO.get(fila["estado"], 0) >= ORDEN_ESTADO[nuevo]:
            return
        con.execute("UPDATE mensajes SET estado=?, error=? WHERE id=?", (nuevo, error, fila["id"]))


# =====================================================================
# 5) ENVÍO A WHATSAPP Y COMUNICACIÓN
# =====================================================================
def cabecera():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}


def a_destino(numero):
    if QUITAR_9_ARGENTINA and numero.startswith("549"):
        return "54" + numero[3:]
    return numero


def meta_post(payload):
    try:
        r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/messages", headers=cabecera(), json=payload, timeout=15)
        if r.ok:
            try:
                return True, r.json()["messages"][0]["id"], ""
            except Exception:
                return True, "", ""
        return False, "", r.json().get("error", {}).get("message", f"Error {r.status_code}")
    except Exception as e:
        return False, "", str(e)


def enviar_texto(numero, texto):
    return meta_post({"messaging_product": "whatsapp", "to": a_destino(numero), "type": "text", "text": {"body": texto[:4000]}})


def enviar_botones(numero, cuerpo):
    botones = [("horarios", "🕒 Horarios"), ("productos", "🧱 Productos"), ("vendedor", "👤 Vendedor")]
    return meta_post({
        "messaging_product": "whatsapp", "to": a_destino(numero), "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo[:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in botones]},
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
        return "", "Error subiendo archivo"
    except Exception as e:
        return "", str(e)


MEDIA_IDS = {}


def enviar_foto_catalogo(numero, foto, texto):
    ruta = os.path.join(CATALOGO_DIR, foto)
    if not os.path.isfile(ruta):
        return False, "", "no hay foto"
    mime = "image/png" if foto.endswith(".png") else "image/jpeg"
    for intento in (1, 2):
        guardado = MEDIA_IDS.get(foto)
        if guardado and intento == 1 and time.time() - guardado[1] < 20 * 86400:
            media_id = guardado[0]
        else:
            media_id, error = subir_media(ruta, mime)
            if not media_id:
                return False, "", error
            MEDIA_IDS[foto] = (media_id, time.time())
        ok, wamid, error = meta_post({"messaging_product": "whatsapp", "to": a_destino(numero), "type": "image", "image": {"id": media_id, "caption": texto[:1024]}})
        if ok:
            return True, wamid, ""
        MEDIA_IDS.pop(foto, None)
    return False, "", error


def decir(numero, texto, quien="bot"):
    ok, wamid, error = enviar_texto(numero, texto)
    registrar(numero, quien, "text", texto, wamid=wamid, estado="sent" if ok else "failed", error=error)
    return ok


def decir_menu(numero, intro):
    ok, wamid, _ = enviar_botones(numero, intro + "\n\nElegí una opción 👇")
    if ok:
        return
    decir(numero, intro + "\n\n" + MENU_ESCRITO)


def enviar_producto(numero, clave):
    p = PRODUCTOS[clave]
    foto = p.get("foto")
    if foto:
        ok, wamid, _ = enviar_foto_catalogo(numero, foto, p["texto"])
        if ok:
            return
    decir(numero, p["texto"])


# =====================================================================
# 6) LÓGICA DE RESPUESTA
# =====================================================================
PALABRAS_VENDEDOR = ("vendedor", "vendedora", "asesor", "humano", "operador", "persona", "hablar con alguien")
PALABRAS_PRECIO = ("precio", "precios", "cuanto sale", "cuanto cuesta", "cuanto vale", "cuanto esta", "presupuesto", "cotizacion", "costo")
PALABRAS_HORARIOS = ("horario", "horarios", "hora", "abren", "abre", "abierto", "cierran", "cierra", "atienden")
PALABRAS_DIRECCION = ("direccion", "ubicacion", "donde estan", "donde queda", "donde esta", "como llego")
PALABRAS_PAGO = ("pago", "pagar", "tarjeta", "tarjetas", "efectivo", "transferencia", "mercado pago", "debito", "credito")
PALABRAS_ENVIO = ("envio", "envios", "delivery", "llevan", "mandan")
PALABRAS_PRODUCTOS = ("productos", "catalogo", "que venden", "que tienen")
PALABRAS_MENU = ("menu", "opciones", "ayuda", "inicio", "info")
PALABRAS_SALUDO = ("hola", "buenas", "buen dia", "buenos dias", "buenas tardes", "hey")
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

    if tiene(t, PALABRAS_VENDEDOR):
        return derivar(numero, "pedido", DERIVACION)
    if tiene(t, PALABRAS_PRECIO):
        return derivar(numero, "precio", DERIVACION_PRECIOS)
    if tiene(t, PALABRAS_HORARIOS):
        decir(numero, texto_horarios(con_pregunta=True))
    elif tiene(t, PALABRAS_DIRECCION):
        decir(numero, DIRECCION)
    elif tiene(t, PALABRAS_PAGO):
        decir(numero, MEDIOS_DE_PAGO)
    elif tiene(t, PALABRAS_ENVIO):
        decir(numero, TEXTO_ENVIO)
    elif claves:
        for clave in claves[:3]:
            enviar_producto(numero, clave)
    elif tiene(t, PALABRAS_PRODUCTOS):
        decir(numero, texto_productos())
    elif tiene(t, PALABRAS_MENU) or tiene(t, PALABRAS_SALUDO):
        decir_menu(numero, SALUDO)
    elif tiene(t, PALABRAS_GRACIAS):
        decir(numero, "¡De nada! 🙌 Cualquier cosa, escribinos.")
    elif primer_mensaje:
        decir_menu(numero, SALUDO)
    else:
        if sumar_fallo(numero) >= MAX_FALLOS:
            return derivar(numero, "insistencia", DERIVACION_INSISTENCIA)
        decir_menu(numero, "No te entendí del todo 🙈")
    marcar(numero, fallos=0)


def procesar_mensaje(msg, perfil=""):
    numero = str(msg.get("from", ""))
    wamid = msg.get("id", "")
    if not numero_valido(numero) or (wamid and not marcar_visto(wamid)):
        return
    if msg.get("type") == "reaction":
        return

    with lock_de(numero):
        texto, entrada = "", ""
        tipo = msg.get("type", "")
        if tipo == "text":
            texto = entrada = (msg.get("text") or {}).get("body", "")
        elif tipo == "interactive":
            inter = msg.get("interactive") or {}
            resp = inter.get("button_reply") or inter.get("list_reply") or {}
            texto = resp.get("title", "")
            entrada = BOTONES.get(resp.get("id"), texto)
        else:
            registrar(numero, "cliente", "text", "[Archivo o medio recibido]")
            if conv(numero)["humano"]:
                return
            if DERIVAR_ARCHIVOS:
                derivar(numero, "archivo", DERIVACION_ARCHIVO)
            return

        registrar(numero, "cliente", "text", texto)
        if perfil:
            marcar(numero, perfil=perfil[:60])
        if conv(numero)["humano"]:
            return
        responder(numero, entrada, es_primer_mensaje(numero))


def procesar_valor(valor):
    perfiles = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in valor.get("contacts") or []}
    for msg in valor.get("messages") or []:
        try:
            procesar_mensaje(msg, perfiles.get(msg.get("from"), ""))
        except Exception:
            log.exception("Error procesando mensaje")


# =====================================================================
# 7) WEBHOOK Y RUTAS FLASK
# =====================================================================
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
    body = request.get_json(silent=True) or {}
    try:
        for entrada in body.get("entry") or []:
            for cambio in entrada.get("changes") or []:
                valor = cambio.get("value") or {}
                threading.Thread(target=procesar_valor, args=(valor,), daemon=True).start()
    except Exception:
        log.exception("Error en webhook")
    return "ok", 200


@app.route("/catalogo/<path:nombre>", methods=["GET"])
def servir_catalogo(nombre):
    return send_file(os.path.join(CATALOGO_DIR, secure_filename(nombre)))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))