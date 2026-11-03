import os
import requests
from flask import Flask, request

app = Flask(__name__)

# Credenciales actualizadas
WHATSAPP_TOKEN = "EAAeiZCeyL9koBSZB5bqaErZCVqKkmnjoloM5UZBnWHltGhuXZCCSdzMSP55TCXW0Utp6geL0llqKcbcgZANRIUJlksDQfTOwfdTE8SpZA8A4jGbios5CL188ZAAgxDck5Db3yZCeRqX41fB0fr4l2lowIs5BmTB93eG6KXPugWD70W40x5Kyiiv5fQmQoTg9t6m9BT2e4UI30qptAZCGkvdsaN6S0l0IBbj3jr9AKWFmQN4OnT7TxWduFZAYHaFZA6bXhsPiN5w34jG5gWiJOfj7QOHWHAZDZD"
PHONE_NUMBER_ID = "1328988693639342"
VERIFY_TOKEN = "ferreteria_secret_token_2026"


def enviar_whatsapp(numero, texto):
    url = f"https://graph.facebook.com/v26.0/{PHONE_NUMBER_ID}/messages"
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
    print("\n--- MENSAJE ENTRANTE ---")
    print(body)
    try:
        valor = body["entry"][0]["changes"][0]["value"]
        mensajes = valor.get("messages")
        if mensajes:
            msg = mensajes[0]
            if msg.get("type") == "text":
                numero = msg["from"]
                texto = msg["text"]["body"]
                print(f"Respondiendo a {numero}: {texto}")
                enviar_whatsapp(numero, f"Recibí tu mensaje: {texto}")
    except Exception as e:
        print("ERROR EN PROCESAMIENTO:", repr(e))
    return "ok", 200


if __name__ == "__main__":
    app.run(port=5000, debug=True)