"""
Bot de WhatsApp de la ferretería + panel web con soporte multimedia (Fotos, PDFs, Audios).
"""

import html
import os
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from hmac import compare_digest

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session

app = Flask(__name__)

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")

# Configuración de carpeta para archivos multimedia (imágenes, PDFs, audios)
UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=12)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)

# ===== COMPLETAR CON LOS DATOS REALES DE LA FERRETERÍA =====
HORARIOS = (
    "🕒 *¡Te esperamos en nuestro local!* 🛠️\n\n"
    "📅 *Lunes a Viernes:* 08:30 a 19:30 hs\n"
    "📅 *Sábados:* 09:00 a 13:00 hs\n"
    "❌ *Domingos y feriados:* Cerrado\n\n"
    "¿Necesitás consultar por alguna herramienta o material en particular?"
)
SALUDO = "¡Hola! 👋 Soy el asistente virtual (un bot) de la ferretería."
MENU = (
    "Escribí *HORARIOS* para ver cuándo atendemos, "
    "o *VENDEDOR* para hablar con una persona."
)
DERIVACION = "🔔 Te paso con un vendedor, en un momento te atiende."
# ============================================================

PALABRAS_VENDEDOR = (
    "vendedor", "vendedora", "asesor", "persona", "humano",
    "operador", "hablar con alguien",
)

TZ_AR = timezone(timedelta(hours=-3))  # Argentina
MAX_CONVERSACIONES = 200
MAX_MENSAJES = 200

LOCK = threading.Lock()
CONVERSACIONES = OrderedDict()  # numero -> datos de la conversación
VISTOS = OrderedDict()  # ids de mensajes de Meta ya procesados


# ---------------------------------------------------------------
# Utilidades y Multimedia (Fotos, PDFs, Audios)
# ---------------------------------------------------------------
def ahora():
    return datetime.now(TZ_AR)


def normalizar(texto):
    texto = texto.lower().strip()
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def conversacion(numero):
    c = CONVERSACIONES.get(numero)
    if c is None:
        c = {
            "numero": numero,
            "mensajes": [],
            "humano": False,
            "pide_vendedor": False,
            "no_leidos": 0,
            "ultimo": 0.0,
        }
        CONVERSACIONES[numero] = c
        while len(CONVERSACIONES) > MAX_CONVERSACIONES:
            CONVERSACIONES.popitem(last=False)
    return c


def agregar(numero, quien, texto):
    with LOCK:
        c = conversacion(numero)
        t = ahora()
        c["mensajes"].append({
            "quien": quien,
            "texto": texto,
            "hora": t.strftime("%d/%m %H:%M"),
        })
        del c["mensajes"][:-MAX_MENSAJES]
        c["ultimo"] = t.timestamp()
        if quien == "cliente":
            c["no_leidos"] += 1
        CONVERSACIONES.move_to_end(numero)


def numero_valido(numero):
    return numero.isdigit() and 8 <= len(numero) <= 20


def descargar_multimedia_whatsapp(media_id, tipo_archivo, whatsapp_token):
    try:
        headers = {"Authorization": f"Bearer {whatsapp_token}"}
        url_media = f"https://graph.facebook.com/v21.0/{media_id}"
        r = requests.get(url_media, headers=headers)
        media_url = r.json().get("url")
        
        if not media_url:
            return None

        file_data = requests.get(media_url, headers=headers).content
        
        # Extensiones según el tipo de archivo recibido
        ext = "jpg"
        if tipo_archivo == "document":
            ext = "pdf"
        elif tipo_archivo in ["audio", "voice"]:
            ext = "ogg"

        nombre_archivo = f"{media_id}.{ext}"
        ruta_guardado = os.path.join(UPLOAD_FOLDER, nombre_archivo)
        
        with open(ruta_guardado, "wb") as f:
            f.write(file_data)
            
        return f"/static/uploads/{nombre_archivo}", ext
    except Exception as e:
        print("Error descargando multimedia:", e)
        return None, None


# ---------------------------------------------------------------
# Lógica del bot
# ---------------------------------------------------------------
def pide_vendedor(texto):
    t = normalizar(texto)
    return any(p in t for p in PALABRAS_VENDEDOR)


def armar_respuesta(texto):
    t = normalizar(texto)
    if any(p in t for p in ("horario", "hora", "abren", "abierto", "cierran")):
        return HORARIOS
    if any(p in t for p in ("hola", "buenas", "buen dia", "buenos dias", "buenas tardes")):
        return f"{SALUDO}\n{MENU}"
    return f"No te entendí 🙈\n{MENU}"


def enviar_whatsapp(numero, texto):
    if numero.startswith("549"):
        numero = "54" + numero[3:]

    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=15)
        if r.ok:
            return True, ""
        try:
            detalle = r.json()["error"]["message"]
        except Exception:
            detalle = f"Meta respondió {r.status_code}"
        return False, detalle
    except Exception as e:
        print("ERROR AL ENVIAR A WHATSAPP:", repr(e))
        return False, "No se pudo conectar con WhatsApp"


def responder_como_bot(numero, texto):
    ok, _ = enviar_whatsapp(numero, texto)
    agregar(numero, "bot", texto if ok else texto + "\n⚠️ (no se pudo enviar)")


def procesar_mensaje(msg):
    mid = msg.get("id")
    with LOCK:
        if mid:
            if mid in VISTOS:
                return
            VISTOS[mid] = True
            while len(VISTOS) > 1000:
                VISTOS.popitem(last=False)

    numero = msg["from"]
    tipo_msg = msg.get("type")
    
    if tipo_msg == "text":
        texto = msg["text"]["body"]
        es_texto = True
    elif tipo_msg in ["image", "document", "audio", "voice"]:
        media_id = msg[tipo_msg]["id"]
        url_archivo, ext = descargar_multimedia_whatsapp(media_id, tipo_msg, WHATSAPP_TOKEN)
        
        if url_archivo:
            if ext == "jpg":
                texto = f'<div style="margin-bottom:4px;"><img src="{url_archivo}" style="max-width:220px; border-radius:8px; cursor:pointer;" onclick="window.open(this.src)"></div><a href="{url_archivo}" download style="font-size:12px; color:#075e54; text-decoration:underline; font-weight:bold;">📥 Descargar Imagen</a>'
            elif ext == "pdf":
                texto = f'📄 <b>Documento PDF recibido</b><br><a href="{url_archivo}" download style="display:inline-block; margin-top:6px; padding:6px 10px; background:#075e54; color:#fff; border-radius:6px; text-decoration:none; font-size:13px;">📥 Descargar Archivo PDF</a>'
            else:
                texto = f'🎙️ <b>Nota de voz / Audio</b><br><audio controls style="max-width:220px; margin-top:5px;"><source src="{url_archivo}" type="audio/ogg"></audio><br><a href="{url_archivo}" download style="font-size:12px; color:#075e54; text-decoration:underline;">📥 Descargar Audio</a>'
        else:
            texto = "[Archivo multimedia recibido]"
        es_texto = True
    else:
        texto = "[Archivo no compatible]"
        es_texto = False

    print(f"Mensaje de {numero}: {texto[:40]}")
    agregar(numero, "cliente", texto)

    with LOCK:
        humano = conversacion(numero)["humano"]
    if humano:
        return

    if not es_texto:
        responder_como_bot(numero, f"Recibí tu archivo 🙂\n{MENU}")
    elif pide_vendedor(texto):
        with LOCK:
            c = conversacion(numero)
            c["humano"] = True
            c["pide_vendedor"] = True
        responder_como_bot(numero, DERIVACION)
    else:
        responder_como_bot(numero, armar_respuesta(texto))


# ---------------------------------------------------------------
# Webhook de Meta
# ---------------------------------------------------------------
@app.route("/", methods=["GET"])
def inicio():
    return "Bot activo", 200


@app.route("/webhook", methods=["GET"])
def verificar():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge"), 200
    return "Token inválido", 403


@app.route("/webhook", methods=["POST"])
def recibir():
    body = request.get_json(silent=True) or {}
    try:
        valor = body["entry"][0]["changes"][0]["value"]
        for msg in valor.get("messages") or []:
            procesar_mensaje(msg)
    except Exception as e:
        print("ERROR EN PROCESAMIENTO:", repr(e))
    return "ok", 200


# ---------------------------------------------------------------
# Acceso al panel (clave + sesión)
# ---------------------------------------------------------------
INTENTOS = {}


def ip_cliente():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def bloqueado(ip):
    fallos, desde = INTENTOS.get(ip, (0, 0))
    if time.time() - desde >= 300:
        INTENTOS.pop(ip, None)
        return False
    return fallos >= 5


def pagina_login(error=""):
    aviso = (
        f'<p style="color:#b00020">{html.escape(error)}</p>' if error else ""
    )
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
        'autofocus required style="width:100%;padding:10px;font-size:16px;'
        'box-sizing:border-box">'
        '<button style="margin-top:12px;width:100%;padding:10px;border:0;'
        'border-radius:8px;background:#075e54;color:#fff;font-size:16px">'
        "Entrar</button>"
        f"{aviso}</form></body></html>"
    )


@app.route("/login", methods=["POST"])
def login():
    if not PANEL_PASSWORD:
        return pagina_login(
            "Panel desactivado: falta cargar PANEL_PASSWORD en el servidor."
        ), 503
    ip = ip_cliente()
    if bloqueado(ip):
        return pagina_login("Demasiados intentos. Probá de nuevo en unos minutos."), 429
    clave = request.form.get("clave", "")
    if compare_digest(clave.encode(), PANEL_PASSWORD.encode()):
        INTENTOS.pop(ip, None)
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


def logueado():
    return bool(session.get("ok"))


@app.after_request
def sin_cache(resp):
    if request.path.startswith("/api/") or request.path == "/panel":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------
# API del panel y Descarga de Archivos
# ---------------------------------------------------------------
@app.route("/api/conversaciones", methods=["GET"])
def api_conversaciones():
    if not logueado():
        return jsonify({"error": "no autorizado"}), 401
    with LOCK:
        items = []
        for c in CONVERSACIONES.values():
            ultimo = c["mensajes"][-1] if c["mensajes"] else {"texto": "", "hora": ""}
            texto_preview = ultimo["texto"]
            if "<img" in texto_preview:
                texto_preview = "[Imagen]"
            elif "PDF" in texto_preview:
                texto_preview = "[Documento PDF]"
            elif "Audio" in texto_preview:
                texto_preview = "[Audio / Nota de voz]"
            items.append({
                "numero": c["numero"],
                "texto": texto_preview[:70],
                "hora": ultimo["hora"],
                "no_leidos": c["no_leidos"],
                "humano": c["humano"],
                "pide_vendedor": c["pide_vendedor"],
                "_orden": c["ultimo"],
            })
    items.sort(key=lambda i: (i["pide_vendedor"], i["_orden"]), reverse=True)
    for i in items:
        del i["_orden"]
    return jsonify(items), 200


@app.route("/api/conversaciones/<numero>", methods=["GET"])
def api_conversacion(numero):
    if not logueado():
        return jsonify({"error": "no autorizado"}), 401
    if not numero_valido(numero):
        return jsonify({"error": "número inválido"}), 400
    with LOCK:
        c = CONVERSACIONES.get(numero)
        if c is None:
            return jsonify({"error": "no existe"}), 404
        c["no_leidos"] = 0
        return jsonify({
            "numero": numero,
            "mensajes": list(c["mensajes"]),
            "humano": c["humano"],
            "pide_vendedor": c["pide_vendedor"],
        }), 200


@app.route("/api/enviar", methods=["POST"])
def api_enviar():
    if not logueado():
        return jsonify({"error": "no autorizado"}), 401
    data = request.get_json(silent=True) or {}
    numero = str(data.get("numero", ""))
    texto = str(data.get("texto", "")).strip()
    if not numero_valido(numero) or not texto:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400
    if len(texto) > 4000:
        return jsonify({"ok": False, "error": "Mensaje demasiado largo"}), 400
    ok, detalle = enviar_whatsapp(numero, texto)
    if not ok:
        return jsonify({"ok": False, "error": detalle}), 502
    agregar(numero, "vendedor", texto)
    with LOCK:
        c = conversacion(numero)
        c["humano"] = True
        c["pide_vendedor"] = False
    return jsonify({"ok": True}), 200


@app.route("/api/enviar_imagen", methods=["POST"])
def api_enviar_imagen():
    if not logueado():
        return jsonify({"error": "no autorizado"}), 401
    
    numero = str(request.form.get("numero", ""))
    archivo = request.files.get("imagen")
    
    if not numero_valido(numero) or not archivo:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400
        
    nombre_archivo = f"enviada_{int(time.time())}_{archivo.filename}"
    ruta_fisica = os.path.join(UPLOAD_FOLDER, nombre_archivo)
    archivo.save(ruta_fisica)
    
    url_publica = request.host_url.rstrip('/') + f"/static/uploads/{nombre_archivo}"
    
    num_envio = numero
    if num_envio.startswith("549"):
        num_envio = "54" + num_envio[3:]
        
    # Validar si es PDF o Imagen para mandar el tipo correcto a Meta
    es_pdf = archivo.filename.lower().endswith('.pdf')
    tipo_meta = "document" if es_pdf else "image"
    
    url_meta = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    if es_pdf:
        payload = {
            "messaging_product": "whatsapp",
            "to": num_envio,
            "type": "document",
            "document": {"link": url_publica, "filename": archivo.filename}
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": num_envio,
            "type": "image",
            "image": {"link": url_publica}
        }
    
    try:
        r = requests.post(url_meta, json=payload, headers=headers, timeout=15)
        if not r.ok:
            return jsonify({"ok": False, "error": "Error al enviar archivo por Meta"}), 502
    except Exception:
        return jsonify({"ok": False, "error": "No se pudo conectar con WhatsApp"}), 502
        
    if es_pdf:
        texto_html = f'📄 <b>Documento enviado</b><br><a href="{url_publica}" download style="display:inline-block; margin-top:6px; padding:6px 10px; background:#075e54; color:#fff; border-radius:6px; text-decoration:none; font-size:13px;">📥 Descargar PDF</a>'
    else:
        texto_html = f'<div style="margin-bottom:4px;"><img src="{url_publica}" style="max-width:220px; border-radius:8px; cursor:pointer;" onclick="window.open(this.src)"></div><a href="{url_publica}" download style="font-size:12px; color:#075e54; text-decoration:underline; font-weight:bold;">📥 Descargar Imagen</a>'

    agregar(numero, "vendedor", texto_html)
    with LOCK:
        c = conversacion(numero)
        c["humano"] = True
        c["pide_vendedor"] = False
    return jsonify({"ok": True, "url": url_publica}), 200


@app.route("/api/devolver_bot", methods=["POST"])
def api_devolver_bot():
    if not logueado():
        return jsonify({"error": "no autorizado"}), 401
    data = request.get_json(silent=True) or {}
    numero = str(data.get("numero", ""))
    if not numero_valido(numero):
        return jsonify({"ok": False, "error": "Número inválido"}), 400
    with LOCK:
        c = CONVERSACIONES.get(numero)
        if c is None:
            return jsonify({"ok": False, "error": "No existe"}), 404
        c["humano"] = False
        c["pide_vendedor"] = False
    return jsonify({"ok": True}), 200


# ---------------------------------------------------------------
# Panel web (se abre en /panel)
# ---------------------------------------------------------------
PANEL_HTML = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel Ferretería</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#efeae2;height:100vh;height:100dvh;display:flex;flex-direction:column;color:#111}
header{background:#075e54;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center}
header a{color:#fff;font-size:14px}
#main{flex:1;display:flex;min-height:0}
#lista{width:330px;background:#fff;overflow-y:auto;border-right:1px solid #ddd}
.item{padding:12px 14px;border-bottom:1px solid #eee;cursor:pointer}
.item:hover,.item.sel{background:#f0f2f5}
.item.pide{background:#fff4d6}
.fila{display:flex;justify-content:space-between;gap:8px}
.num{font-weight:600}
.hora{font-size:12px;color:#667;white-space:nowrap}
.prev{font-size:13px;color:#555;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.badge{background:#25d366;color:#fff;border-radius:10px;padding:0 7px;font-size:12px;margin-left:6px}
.vacio{margin:auto;color:#667;padding:20px;text-align:center}
#chat{flex:1;display:flex;flex-direction:column;min-width:0}
#cab,#form{display:none}
#chat.activo #cab{display:flex}
#chat.activo #form{display:flex}
#chat.activo #vacio{display:none}
#cab{background:#f0f2f5;padding:10px 14px;align-items:center;gap:10px;border-bottom:1px solid #ddd;flex-wrap:wrap}
#cab .atras{display:none}
#msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:6px}
.msg{max-width:78%;padding:7px 10px;border-radius:8px;box-shadow:0 1px 1px #0002;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.msg small{display:block;font-size:11px;color:#667;margin-top:3px;text-align:right}
.etq{font-size:11px;font-weight:600;color:#555;display:block;margin-bottom:2px}
.cliente{background:#fff;align-self:flex-start}
.bot{background:#d9edff;align-self:flex-end}
.vendedor{background:#dcf8c6;align-self:flex-end}
#form{gap:8px;padding:10px;background:#f0f2f5;align-items:center}
#texto{flex:1;resize:none;border:1px solid #ccc;border-radius:8px;padding:8px;font:inherit;height:44px}
.btn-attach{cursor:pointer;font-size:22px;padding:0 6px;display:flex;align-items:center}
button{border:0;border-radius:8px;padding:8px 14px;background:#075e54;color:#fff;font:inherit;cursor:pointer}
button.sec{background:#667}
button:disabled{opacity:.5}
@media(max-width:700px){
 #lista{width:100%}
 #main.abierto #lista{display:none}
 #main:not(.abierto) #chat{display:none}
 #cab .atras{display:inline-block}
}
</style></head><body>
<header><strong>📋 Panel de la ferretería</strong><a href="/salir">Salir</a></header>
<div id="main">
 <div id="lista"></div>
 <div id="chat">
  <div id="cab">
   <button class="sec atras" id="atras">←</button>
   <strong id="titulo"></strong>
   <span id="estado"></span>
   <button class="sec" id="devolver" style="display:none">Devolver al bot</button>
  </div>
  <div id="msgs"><div class="vacio" id="vacio">Elegí una conversación de la lista.</div></div>
  <div id="form">
   <label class="btn-attach" title="Adjuntar Foto o PDF">📎<input type="file" id="inputFoto" accept="image/*,.pdf" style="display:none" onchange="enviarImagen()"></label>
   <textarea id="texto" placeholder="Escribí tu respuesta..."></textarea>
   <button id="enviar">Enviar</button>
  </div>
 </div>
</div>
<script>
const $ = id => document.getElementById(id);
let actual = null;
let ultimaClave = "";

function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt !== undefined) e.textContent = txt;
  return e;
}
function limpiar(n) { while (n.firstChild) n.removeChild(n.firstChild); }

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = "/panel"; throw new Error("sin sesión"); }
  return r.json();
}

async function cargarLista() {
  const data = await api("/api/conversaciones");
  const pend = data.filter(c => c.pide_vendedor).length;
  document.title = (pend ? "(" + pend + ") 🔔 " : "") + "Panel Ferretería";
  const lista = $("lista");
  limpiar(lista);
  if (!data.length) {
    lista.appendChild(el("div", "vacio", "Todavía no hay mensajes."));
    return;
  }
  data.forEach(c => {
    const item = el("div", "item" + (c.pide_vendedor ? " pide" : "") + (c.numero === actual ? " sel" : ""));
    const f1 = el("div", "fila");
    const izq = el("span", "num", "+" + c.numero);
    if (c.no_leidos) izq.appendChild(el("span", "badge", String(c.no_leidos)));
    f1.appendChild(izq);
    f1.appendChild(el("span", "hora", c.hora));
    item.appendChild(f1);
    const marca = c.pide_vendedor ? "🔔 " : (c.humano ? "👤 " : "");
    item.appendChild(el("div", "prev", marca + c.texto));
    item.onclick = () => abrir(c.numero);
    lista.appendChild(item);
  });
}

function abrir(numero) {
  actual = numero;
  ultimaClave = "";
  $("main").classList.add("abierto");
  $("chat").classList.add("activo");
  cargarChat(true).catch(() => {});
  cargarLista().catch(() => {});
}

async function cargarChat(forzar) {
  if (!actual) return;
  const d = await api("/api/conversaciones/" + actual);
  if (d.numero !== actual) return;
  $("titulo").textContent = "+" + d.numero;
  $("estado").textContent = d.humano ? "👤 Atiende una persona" : "🤖 Atiende el bot";
  $("devolver").style.display = d.humano ? "inline-block" : "none";
  const clave = d.mensajes.length + "|" + d.humano;
  if (!forzar && clave === ultimaClave) return;
  ultimaClave = clave;
  const box = $("msgs");
  const abajo = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  limpiar(box);
  const nombres = { cliente: "Cliente", bot: "Bot", vendedor: "Vendedor" };
  d.mensajes.forEach(m => {
    const b = el("div", "msg " + m.quien);
    b.appendChild(el("span", "etq", nombres[m.quien] || m.quien));
    const contenido = el("div", "", "");
    if (m.texto.includes("<img") || m.texto.includes("📄") || m.texto.includes("🎙️")) {
        contenido.innerHTML = m.texto;
    } else {
        contenido.textContent = m.texto;
    }
    b.appendChild(contenido);
    b.appendChild(el("small", "", m.hora));
    box.appendChild(b);
  });
  if (abajo || forzar) box.scrollTop = box.scrollHeight;
}

async function enviar() {
  const t = $("texto").value.trim();
  if (!t || !actual) return;
  $("enviar").disabled = true;
  try {
    const r = await fetch("/api/enviar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ numero: actual, texto: t })
    });
    if (r.status === 401) { location.href = "/panel"; return; }
    const d = await r.json();
    if (d.ok) { $("texto").value = ""; }
    else { alert("No se pudo enviar: " + (d.error || "error desconocido")); }
  } catch (e) {
    alert("Error de conexión. Probá de nuevo.");
  }
  $("enviar").disabled = false;
  await cargarChat(true).catch(() => {});
  cargarLista().catch(() => {});
}

async function enviarImagen() {
  const input = $("inputFoto");
  if (!input.files.length || !actual) return;
  const formData = new FormData();
  formData.append("numero", actual);
  formData.append("imagen", input.files[0]);
  try {
    const r = await fetch("/api/enviar_imagen", {
      method: "POST",
      body: formData
    });
    if (r.status === 401) { location.href = "/panel"; return; }
    const d = await r.json();
    if (!d.ok) { alert("Error al enviar archivo: " + (d.error || "desconocido")); }
  } catch (e) {
    alert("Error de conexión al enviar el archivo.");
  }
  input.value = "";
  await cargarChat(true).catch(() => {});
  cargarLista().catch(() => {});
}

async function devolver() {
  if (!actual) return;
  await api("/api/devolver_bot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ numero: actual })
  });
  await cargarChat(true).catch(() => {});
  cargarLista().catch(() => {});
}

$("enviar").onclick = enviar;
$("devolver").onclick = devolver;
$("atras").onclick = () => {
  actual = null;
  $("main").classList.remove("abierto");
  $("chat").classList.remove("activo");
  cargarLista().catch(() => {});
};
$("texto").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); enviar(); }
});

cargarLista().catch(() => {});
setInterval(() => {
  cargarLista().catch(() => {});
  cargarChat(false).catch(() => {});
}, 4000);
</script>
</body></html>"""


@app.route("/panel", methods=["GET"])
def panel():
    if not logueado():
        return pagina_login(), 200
    return PANEL_HTML, 200


if __name__ == "__main__":
    app.run(port=5000, debug=True)