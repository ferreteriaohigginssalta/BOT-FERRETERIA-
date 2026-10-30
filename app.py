from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Permite peticiones externas desde Google Sites sin bloqueos

# Base de datos temporal en memoria
mensajes_db = [
    {
        "remitente": "5493872127887",
        "texto": "Hola, necesito presupuesto por unos caños de luz y prensacables de 3/4.",
        "hora": "18:20",
        "tipo": "cliente"
    }
]

@app.route('/')
@app.route('/api/mensajes', methods=['GET', 'POST'])
def manejar_mensajes():
    if request.method == 'POST':
        datos = request.json
        if datos:
            mensajes_db.append(datos)
            return jsonify({"status": "ok", "mensaje": "Guardado correctamente"}), 200
            
    return jsonify(mensajes_db), 200

# Ruta del Webhook para recibir mensajes de WhatsApp Meta
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Verificación del webhook de Meta
        verify_token = "ferreteria_token_123"
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        if mode and token:
            if mode == 'subscribe' and token == verify_token:
                return challenge, 200
            else:
                return 'Token incorrecto', 403
                
    elif request.method == 'POST':
        body = request.json
        try:
            # Extraer datos de la estructura de WhatsApp Cloud API
            entry = body.get('entry', [])[0]
            changes = entry.get('changes', [])[0]
            value = changes.get('value', {})
            messages = value.get('messages', [])
            
            if messages:
                msg = messages[0]
                numero_cliente = msg.get('from', 'Desconocido')
                texto_msg = msg.get('text', {}).get('body', '')
                
                nuevo_mensaje = {
                    "remitente": numero_cliente,
                    "texto": texto_msg,
                    "hora": "Ahora",
                    "tipo": "cliente"
                }
                mensajes_db.append(nuevo_mensaje)
        except Exception as e:
            print("Error procesando webhook:", e)
            
        return jsonify({"status": "recibido"}), 200

if __name__ == '__main__':
    app.run(debug=True)