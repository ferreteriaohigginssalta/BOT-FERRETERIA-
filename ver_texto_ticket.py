import pdfplumber
import os

CARPETA = "TICKETS_ENERO_AGOSTO"
archivos = [f for f in os.listdir(CARPETA) if f.lower().endswith('.pdf')]

if archivos:
    ruta = os.path.join(CARPETA, archivos[0])
    print(f"=== LEYENDO TICKET: {archivos[0]} ===\n")
    with pdfplumber.open(ruta) as pdf:
        for p in pdf.pages:
            print(p.extract_text())
else:
    print("No hay archivos PDF.")