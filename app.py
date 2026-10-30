from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Activa CORS para permitir conexiones externas desde Google Sites u otras webs

# Mensajes iniciales de prueba de la ferretería
mensajes_db = [
    {
        "hora": "15:00",
        "remitente": "Cliente (5493872127887)",
        "texto": "Hola! Escribí *HORARIOS* para ver cuando atendemos",
        "tipo": "cliente"
    }
]

@app.route('/')
@app.route('/api/mensajes', methods=['GET', 'POST'])
def manejar_mensajes():
    if request.method == 'POST':
        nuevo_msg = request.json
        if nuevo_msg:
            mensajes_db.append(nuevo_msg)
        return jsonify({"status": "ok", "mensaje": "Guardado correctamente"})
    
    return jsonify(mensajes_db)

if __name__ == '__main__':
    app.run(debug=True)