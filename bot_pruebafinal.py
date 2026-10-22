import os
import unicodedata

import requests
from flask import Flask, request

app = Flask(__name__)

# Los datos secretos NO van en el código: se cargan como variables de entorno
# en el servidor (Render / Railway).
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")

# ===== COMPLETAR CON LOS DATOS REALES DE LA FERRETERÍA =====
HORARIOS = (
    "🕒 Nuestros horarios:\n"
    "Lunes a viernes: COMPLETAR\n"
    "Sábados: COMPLETAR\n"
    "Domingos: cerrado"
)
SALUDO = "¡Hola! 👋 Gracias por escribir a la ferretería."
MENU = "Escribí *HORARIOS* para ver cuándo atendemos."
# ============================================================


def normalizar(texto):
    """Pasa a minúsculas y saca tildes: 'Horários' -> 'horarios'."""
    texto = texto.lower().strip()
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def armar_respuesta(texto):
    t = normalizar(texto)
    if any(p in t for p in ("horario", "hora", "abren", "abierto", "cierran")):
        return HORARIOS
    if any(p in t for p in ("hola", "buenas", "buen dia", "buenos dias", "buenas tardes")):
        return f"{SALUDO}\n{MENU}"
    return f"No te entendí 🙈\n{MENU}"


def enviar_whatsapp(numero, texto):
    # Argentina: Meta manda 549..., pero para responder va sin el 9
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
    r = requests.post(url, headers=headers, json=data, timeout=15)
    print("ENVIO -> Código:", r.status_code, "| Respuesta Meta:", r.text)


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
        mensajes = valor.get("messages")
        if mensajes:
            msg = mensajes[0]
            numero = msg["from"]
            if msg.get("type") == "text":
                texto = msg["text"]["body"]
                print(f"Mensaje de {numero}: {texto}")
                enviar_whatsapp(numero, armar_respuesta(texto))
            else:
                # fotos, audios, etc.
                enviar_whatsapp(numero, f"Por ahora solo leo texto 🙂\n{MENU}")
    except Exception as e:
        print("ERROR EN PROCESAMIENTO:", repr(e))
    return "ok", 200


if __name__ == "__main__":
    # Solo para probar en tu compu. En la nube se usa gunicorn.
    app.run(port=5000, debug=True)