import os
import unicodedata

import requests
from flask import Flask, jsonify, request

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

# Memoria temporal para la bandeja multiagente web
mensajes_globales = [
    {
        "remitente": "FerretBot (Sistema)",
        "texto": "Sistema multiagente iniciado correctamente.",
        "tipo": "bot",
        "hora": "Recién",
    }
]


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
    numero_limpio = numero
    if numero_limpio.startswith("549"):
        numero_limpio = "54" + numero_limpio[3:]

    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero_limpio,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=15)
        print("ENVIO -> Código:", r.status_code, "| Respuesta Meta:", r.text)
    except Exception as e:
        print("ERROR AL ENVIAR A WHATSAPP:", repr(e))


@app.route("/", methods=["GET"])
def inicio():
    return "Bot activo y conectado a la bandeja de la ferretería", 200


# ==========================================
# ENDPOINTS PARA LA INTERFAZ WEB MULTIAGENTE
# ==========================================
@app.route("/api/mensajes", methods=["GET"])
def obtener_mensajes():
    # Devuelve la lista de chats actualizada a la interfaz web de Google Sites
    return jsonify(mensajes_globales), 200


@app.route("/api/enviar", methods=["POST"])
def enviar_desde_web():
    # Recibe el mensaje que escribe el vendedor desde el panel web
    data = request.get_json(silent=True) or {}
    texto = data.get("texto", "")
    numero_destino = data.get("numero", "")  # Número del cliente al que se le responde

    if texto:
        # 1. Guarda el mensaje del vendedor en la bandeja local
        mensajes_globales.append({
            "remitente": "Vendedor (Humano)",
            "texto": texto,
            "tipo": "vendedor",
            "hora": "Ahora"
        })
        
        # 2. Si hay un número de destino, lo manda por la API oficial de WhatsApp
        if numero_destino:
            enviar_whatsapp(numero_destino, texto)

        return jsonify({"status": "enviado", "texto": texto}), 200
    
    return jsonify({"error": "Texto vacío"}), 400
# ==========================================


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
                
                # Guardar el mensaje del cliente en la bandeja web
                mensajes_globales.append({
                    "remitente": f"Cliente ({numero})",
                    "texto": texto,
                    "tipo": "cliente",
                    "hora": "Recién"
                })

                # Generar y enviar respuesta automática del bot
                respuesta = armar_respuesta(texto)
                enviar_whatsapp(numero, respuesta)
                
                # Guardar la respuesta del bot en la bandeja web
                mensajes_globales.append({
                    "remitente": "Bot (Automático)",
                    "texto": respuesta,
                    "tipo": "bot",
                    "hora": "Recién"
                })
            else:
                # fotos, audios, etc.
                aviso = f"Por ahora solo leo texto 🙂\n{MENU}"
                enviar_whatsapp(numero, aviso)
                mensajes_globales.append({
                    "remitente": f"Cliente ({numero}) [Multimedio]",
                    "texto": "[El cliente envió una foto o archivo]",
                    "tipo": "cliente",
                    "hora": "Recién"
                })
    except Exception as e:
        print("ERROR EN PROCESAMIENTO:", repr(e))
    return "ok", 200


if __name__ == "__main__":
    app.run(port=5000, debug=True)