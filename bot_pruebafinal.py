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
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        return challenge, 200

    if request.method == "POST":
        data = request.json
        try:
            entry = data.get("entry", [])[0]
            change = entry.get("changes", [])[0]
            value = change.get("value", {})
            messages = value.get("messages")

            if messages:
                msg = messages[0]
                numero_remitente = msg.get("from")
                texto_mensaje = msg.get("text", {}).get("body", "")

                if numero_remitente not in historial_conversaciones:
                    historial_conversaciones[numero_remitente] = []

                historial_conversaciones[numero_remitente].append({
                    "mensaje": texto_mensaje,
                    "remitente": "cliente"
                })

                guardar_chats_locales(historial_conversaciones)

        except Exception as e:
            print(f"Error procesando el webhook: {e}")

    return "OK", 200

# HTML y JavaScript funcional para el panel de control
PANEL_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Panel Ferretería - Chats</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; display: flex; height: 100vh; background: #f4f4f9; }
        .sidebar { width: 300px; background: #2c3e50; color: white; overflow-y: auto; padding: 20px; }
        .sidebar h2 { font-size: 1.2rem; margin-bottom: 20px; }
        .chat-item { padding: 10px; margin-bottom: 8px; background: #34495e; cursor: pointer; border-radius: 4px; }
        .chat-item:hover { background: #1abc9c; }
        .chat-window { flex: 1; display: flex; flex-direction: column; padding: 20px; }
        .messages { flex: 1; background: white; padding: 20px; border-radius: 8px; overflow-y: auto; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .msg { margin-bottom: 10px; padding: 8px 12px; border-radius: 6px; max-width: 60%; }
        .msg.cliente { background: #e1f5fe; align-self: flex-start; }
    </style>
</head>
<body>
    <div class="sidebar">
        <h2>Chats de Clientes</h2>
        <div id="lista-chats">Cargando chats...</div>
    </div>
    <div class="chat-window">
        <h2 id="titulo-chat">Seleccioná un chat</h2>
        <div class="messages" id="contenedor-mensajes"></div>
    </div>

    <script>
        async function cargarChats() {
            try {
                let res = await fetch('/api/chats');
                let numeros = await res.json();
                let contenedor = document.getElementById('lista-chats');
                contenedor.innerHTML = '';
                if(numeros.length === 0) {
                    contenedor.innerHTML = '<p>No hay chats guardados aún.</p>';
                    return;
                }
                numeros.forEach(num => {
                    let div = document.createElement('div');
                    div.className = 'chat-item';
                    div.textContent = 'Cliente: ' + num;
                    div.onclick = () => cargarConversacion(num);
                    contenedor.appendChild(div);
                });
            } catch(e) {
                console.error("Error cargando chats", e);
            }
        }

        async function cargarConversacion(numero) {
            document.getElementById('titulo-chat').textContent = 'Chat con: ' + numero;
            let res = await fetch('/api/conversaciones/' + numero);
            let mensajes = await res.json();
            let msgContainer = document.getElementById('contenedor-mensajes');
            msgContainer.innerHTML = '';
            mensajes.forEach(m => {
                let d = document.createElement('div');
                d.className = 'msg ' + m.remitente;
                d.textContent = m.mensaje;
                msgContainer.appendChild(d);
            });
        }

        cargarChats();
        setInterval(cargarChats, 5000); // Actualiza cada 5 segundos
    </script>
</body>
</html>
"""

def not_logueado():
    return False

def pagina_login():
    return "Por favor inicie sesión.", 401

@app.route("/panel", methods=["GET"])
def panel():
    if not_logueado():
        return pagina_login(), 200
    return PANEL_HTML, 200

# API para listar todos los números con chats activos
@app.route("/api/chats", methods=["GET"])
def obtener_chats():
    return json.dumps(list(historial_conversaciones.keys()), ensure_ascii=False), 200

# API para devolver el historial de un número específico
@app.route("/api/conversaciones/<numero>", methods=["GET"])
def obtener_conversacion(numero):
    if numero in historial_conversaciones:
        return json.dumps(historial_conversaciones[numero], ensure_ascii=False), 200
    return json.dumps([], ensure_ascii=False), 200

@app.route("/api/devolver_bot", methods=["POST"])
def devolver_bot():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)