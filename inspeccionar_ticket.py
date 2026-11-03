import os
import pdfplumber

CARPETA_TICKETS = "TICKETS_ENERO_AGOSTO"

# -------------------------------------------------------------
# INGRESÁ AQUÍ EL NOMBRE EXACTO DEL ARCHIVO QUE QUERÉS PROBAR
# -------------------------------------------------------------
NOMBRE_ARCHIVO = "FCB_00003_00010917_CONS_FINAL.PDF"  # Cambiá esto por el nombre del ticket

ruta_completa = os.path.join(CARPETA_TICKETS, NOMBRE_ARCHIVO)

if not os.path.exists(ruta_completa):
    print(f"❌ No se encontró el archivo '{NOMBRE_ARCHIVO}' en '{CARPETA_TICKETS}'.")
    print("Asegurate de escribir el nombre exacto con la extensión .pdf")
    exit()

print(f"============================================================")
print(f" INSPECCIONANDO: {NOMBRE_ARCHIVO}")
print(f"============================================================\n")

try:
    with pdfplumber.open(ruta_completa) as pdf:
        texto_raw = ""
        for i, pagina in enumerate(pdf.pages, 1):
            texto_raw += (pagina.extract_text() or "") + "\n"

        lineas = [l.strip() for l in texto_raw.split("\n") if l.strip()]

        print("--- LÍNEAS LEÍDAS POR EL SCRIPT (NUMERADAS) ---")
        for idx, linea in enumerate(lineas):
            print(f"Línea {idx:02d}: {linea}")

        print("\n-----------------------------------------------")

except Exception as e:
    print(f"❌ Error al abrir o leer el PDF: {e}")