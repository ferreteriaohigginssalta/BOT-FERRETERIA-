import requests
from flask import Flask, request

app = Flask(__name__)

# Pegá acá tus datos (y no los compartas con nadie)
WHATSAPP_TOKEN = "EAAeiZCeyL9koBS2BxlnkYhcu6jK1yJR6ZCJBMmDZCE3AlUudtCb204sZB4uBmPAGAhHkELik0ncIhdRGBIXyhjKNdUxOKGnCYhUthRm3EFPrbzredecAtorcVY403Gr6iRraeiwgmBoSeQPVdbfdy97hRaEIdC9AXTNkaSXJoiaSztgFtordEg4uzw6VTCRcA05XztrZAnokoZAnbwoquZAGwNiItjsbDyKbx9UDoUgC8i2COR8WFrCUHcM3lM4uzfE2YizNpVtd7MxKZA8PitFK"
PHONE_NUMBER_ID = "1328988693639342"
VERIFY_TOKEN = "ferreteria_secret_token_2026"


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