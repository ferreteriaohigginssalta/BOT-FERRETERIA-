import os
import random
import re
import pdfplumber

CARPETA_TICKETS = "TICKETS_ENERO_AGOSTO"

if not os.path.exists(CARPETA_TICKETS):
    print(f"⚠️ No se encontró la carpeta '{CARPETA_TICKETS}'. Revisa el nombre.")
    exit()

archivos_pdf = [f for f in os.listdir(CARPETA_TICKETS) if f.lower().endswith('.pdf')]

if not archivos_pdf:
    print(f"⚠️ No se encontraron archivos PDF dentro de la carpeta '{CARPETA_TICKETS}'.")
    exit()

# Tomamos hasta 10 tickets aleatorios
muestras = random.sample(archivos_pdf, min(10, len(archivos_pdf)))

print("============================================================")
print(f" INSPECCIONANDO {len(muestras)} TICKETS ALEATORIOS")
print("============================================================\n")

for i, archivo in enumerate(muestras, 1):
    ruta_pdf = os.path.join(CARPETA_TICKETS, archivo)
    print(f"=================== MUESTRA {i}: {archivo} ===================")
    
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = ""
            for pagina in pdf.pages:
                texto += (pagina.extract_text() or "") + "\n"
                
            match_fecha = re.search(r"(\d{2}/\d{2}/\d{4})", texto)
            fecha = match_fecha.group(1) if match_fecha else "NO DETECTADA"
            
            print(f"FECHA DETECTADA: {fecha}")
            print("TEXTO COMPLETO EXTRAÍDO:")
            print("-" * 50)
            print(texto.strip())
            print("-" * 50 + "\n")
    except Exception as e:
        print(f"Error al leer {archivo}: {e}\n")