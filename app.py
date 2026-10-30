@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        verify_token = "ferreteria_token_123"
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        if mode and token:
            if mode == 'subscribe' and token == verify_token:
                return challenge, 200
            else:
                return 'Token incorrecto', 403
        return 'Verificación fallida', 403

    elif request.method == 'POST':
        body = request.json
        try:
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