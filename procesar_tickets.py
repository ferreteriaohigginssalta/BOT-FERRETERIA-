import os
import re
import pandas as pd
import pdfplumber

CARPETA_TICKETS = "."
lista_ventas = []

archivos_pdf = [f for f in os.listdir(CARPETA_TICKETS) if f.lower().endswith('.pdf')]
total_archivos = len(archivos_pdf)

print(f"Iniciando procesamiento de {total_archivos} tickets de Agosto con progreso en tiempo real...\n")

for idx, archivo in enumerate(archivos_pdf, 1):
    # Imprime la línea de progreso en tiempo real
    print(f"[{idx}/{total_archivos}] Procesando: {archivo}...")
    ruta_pdf = os.path.join(CARPETA_TICKETS, archivo)
    
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = ""
            for page in pdf.pages:
                texto += (page.extract_text() or "") + "\n"

        match_fecha = re.search(r"(\d{2}/\d{2}/\d{4})", texto)
        fecha = match_fecha.group(1) if match_fecha else "Sin Fecha"

        lineas = [l.strip() for l in texto.split("\n") if l.strip()]
        
        for i, linea in enumerate(lineas):
            match_cant = re.match(r"^(\d+[\.,]?\d*)", linea)
            
            if match_cant:
                try:
                    cantidad = float(match_cant.group(1).replace(",", "."))
                except ValueError:
                    continue

                partes = linea.split()
                numeros = []
                
                for parte in partes:
                    limpio = re.sub(r"[^\d,\.]", "", parte)
                    if limpio:
                        try:
                            if "," in limpio and "." in limpio:
                                limpio = limpio.replace(".", "").replace(",", ".")
                            elif "," in limpio:
                                limpio = limpio.replace(",", ".")
                            
                            val = float(limpio)
                            if val > 0:
                                numeros.append(val)
                        except ValueError:
                            pass

                p_unit = 0.0
                total_linea = 0.0
                
                if len(numeros) >= 2:
                    p_unit = numeros[-2]
                    total_linea = numeros[-1]
                elif len(numeros) == 1:
                    total_linea = numeros[0]
                    p_unit = total_linea / cantidad if cantidad > 0 else 0.0

                detalle = lineas[i-1] if i > 0 else "Producto sin nombre"
                
                if not any(h in detalle.upper() for h in ["DETALLE", "CANT", "FECHA", "TOTAL", "SUB-TOTAL", "IMPORTE", "P.UNIT"]):
                    lista_ventas.append({
                        "Fecha": fecha,
                        "Cant": cantidad,
                        "Detalle": detalle,
                        "P.Unit": p_unit,
                        "Total": total_linea,
                        "Archivo": archivo
                    })
    except Exception as e:
        print(f"   ⚠️ Error en archivo {archivo}: {e}")

df_ventas = pd.DataFrame(lista_ventas)

if not df_ventas.empty:
    df_ventas.to_excel("Detalle_Ventas_Agosto.xlsx", index=False)
    
    df_ranking = df_ventas.groupby("Detalle").agg({
        "Cant": "sum",
        "Total": "sum",
        "P.Unit": "mean"
    }).reset_index()
    
    df_ranking = df_ranking.sort_values(by="Total", ascending=False)
    df_ranking.to_excel("Ranking_Productos_Mas_Vendidos_Agosto.xlsx", index=False)
    
    print("\n----------------------------------------")
    print("¡Procesamiento exitoso!")
    print(df_ranking.head(10).to_string(index=False))
    print("----------------------------------------")
else:
    print("No se encontraron datos.")