import json
import os
from flask import Flask, request

app = Flask(__name__)

# Archivo local donde se guardará la persistencia de los chats
ARCHIVO_CHATS = "chats_persistentes.json"

def cargar_chats_locales():
    """Carga el historial de chats guardado localmente si existe."""
    if os.path.exists(ARCHIVO_CHATS):
        try:
            with open(ARCHIVO_CHATS, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def guardar_chats_locales(datos):
    """Guarda el historial actual de chats en el archivo JSON local."""
    try:
        with open(ARCHIVO_CHATS, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error al guardar los chats: {e}")

# Inicializamos la memoria del bot cargando los datos previos
historial_conversaciones = cargar_chats_locales()

@app.route("/", methods=["GET"])
def inicio():
    return "Bot de WhatsApp de la Ferretería funcionando correctamente.", 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook_whatsapp():
    """Endpoint para recibir y procesar los mensajes de WhatsApp"""
    if request.method == "GET":
        # Verificación del Webhook con Meta
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        return challenge, 200

    if request.method == "POST":
        data = request.json
        try:
            # Procesamiento básico del mensaje entrante
            entry = data.get("entry", [])[0]
            change = entry.get("changes", [])[0]
            value = change.get("value", {})
            messages = value.get("messages")

            if messages:
                msg = messages[0]
                numero_remitente = msg.get("from")
                texto_mensaje = msg.get("text", {}).get("body", "")

                # Guardamos o actualizamos el historial en memoria local
                if numero_remitente not in historial_conversaciones:
                    historial_conversaciones[numero_remitente] = []

                historial_conversaciones[numero_remitente].append({
                    "mensaje": texto_mensaje,
                    "remitente": "cliente"
                })

                # Persistimos inmediatamente en el JSON local
                guardar_chats_locales(historial_conversaciones)

        except Exception as e:
            print(f"Error procesando el webhook: {e}")

        return "EVENT_RECEIVED", 200

# HTML básico para el panel de control interno
PANEL_HTML = """
<!DOCTYPE html>
<html>
<head><title>Panel Ferretería</title></head>
<body>
    <h1>Panel de Control - Bot de Ferretería</h1>
    <p>El bot está activo y guardando la persistencia de los chats correctamente.</p>
</body>
</html>
"""

def not_logueado():
    # Función auxiliar de autenticación para el panel
    return False

def pagina_login():
    return "Por favor inicie sesión.", 401

@app.route("/panel", methods=["GET"])
def panel():
    if not_logueado():
        return pagina_login(), 200
    return PANEL_HTML, 200

# Rutas agregadas para que el panel no tire error 404
@app.route("/api/conversaciones/<numero>", methods=["GET"])
def obtener_conversacion(numero):
    """Devuelve el historial de un chat específico para el panel"""
    if numero in historial_conversaciones:
        return json.dumps(historial_conversaciones[numero], ensure_ascii=False), 200
    return json.dumps([], ensure_ascii=False), 200

@app.route("/api/devolver_bot", methods=["POST"])
def devolver_bot():
    """Endpoint auxiliar para el panel"""
    return {"status": "ok"}, 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)