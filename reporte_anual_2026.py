import os
import re
import pdfplumber
import pandas as pd

# 1. Carpeta con todos los tickets del año y archivo de salida
CARPETA_TICKETS = "TICKETS_ENERO_SEP"  # O el nombre exacto que le hayas puesto a la carpeta con los PDFs
ARCHIVO_SALIDA_EXCEL = "Consolidado_Anual_y_BCG.xlsx"

if not os.path.exists(CARPETA_TICKETS) and os.path.exists("TICKETS_ANUALES"):
    CARPETA_TICKETS = "TICKETS_ANUALES"

if not os.path.exists(CARPETA_TICKETS):
    print(f"⚠️ No se encontró la carpeta '{CARPETA_TICKETS}'. Verificá el nombre exacto.")
    exit()

archivos_pdf = [f for f in os.listdir(CARPETA_TICKETS) if f.lower().endswith('.pdf')]
total_archivos = len(archivos_pdf)

if not archivos_pdf:
    print(f"⚠️ No se encontraron archivos PDF en '{CARPETA_TICKETS}'.")
    exit()

registros = []
anomalias = []

NOMBRES_MESES = {
    "01": "01_Enero", "02": "02_Febrero", "03": "03_Marzo", "04": "04_Abril",
    "05": "05_Mayo", "06": "06_Junio", "07": "07_Julio", "08": "08_Agosto",
    "09": "09_Septiembre", "10": "10_Octubre", "11": "11_Noviembre", "12": "12_Diciembre"
}

print("============================================================")
print(f" PROCESANDO {total_archivos} TICKETS ANUALES Y DETECTANDO ANOMALÍAS")
print("============================================================\n")

for i, archivo in enumerate(archivos_pdf, 1):
    if i % 100 == 0 or i == total_archivos:
        porcentaje = (i / total_archivos) * 100
        print(f"⏳ Progreso: {i}/{total_archivos} tickets ({porcentaje:.1f}%) | Registros: {len(registros)} | Anomalías: {len(anomalias)}")

    ruta_pdf = os.path.join(CARPETA_TICKETS, archivo)
    
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto_completo = ""
            for pagina in pdf.pages:
                texto_completo += (pagina.extract_text() or "") + "\n"
            
            lineas = [l.strip() for l in texto_completo.split("\n") if l.strip()]
            
            # --- DETECTAR SI ES NOTA DE CRÉDITO PARA RESTAR ---
            es_nota_credito = any("NOTA DE CREDITO" in l.upper() or "NOTA DE CRÉDITO" in l.upper() for l in lineas[:5])
            multiplicador = -1 if es_nota_credito else 1
            
            # --- EXTRAER FECHA REAL (Descartando Inicio de Actividades 2015) ---
            match_fecha = re.search(r"FECHA:\s*(\d{2})/(\d{2})/(\d{4})", texto_completo, re.IGNORECASE)
            dia_val, num_mes, ano_val = None, None, None
            
            if not match_fecha:
                fechas_encontradas = re.findall(r"(\d{2})/(\d{2})/(\d{4})", texto_completo)
                for f_dia, f_mes, f_ano in fechas_encontradas:
                    if f_ano != "2015":  # Ignorar fecha AFIP 2015
                        dia_val, num_mes, ano_val = f_dia, f_mes, f_ano
                        break
            else:
                dia_val, num_mes, ano_val = match_fecha.group(1), match_fecha.group(2), match_fecha.group(3)

            if num_mes and ano_val:
                fecha = f"{dia_val}/{num_mes}/{ano_val}"
                mes_nombre = NOMBRES_MESES.get(num_mes, f"{num_mes}_Mes")
            else:
                fecha = "SIN FECHA"
                mes_nombre = "Sin_Fecha"
            
            # --- PROCESAMIENTO DE LÍNEAS / PRODUCTOS ---
            for idx, linea in enumerate(lineas):
                if any(k in linea.upper() for k in ["SUB-TOTAL", "TOTAL:", "DESCUENTO "]) and "DESCUENTO:" not in linea.upper():
                    continue

                partes = linea.split()
                cant_val, punit_val, total_val = None, None, None
                texto_misma_linea = ""
                tipo_anomalia = None

                # CASO 1: 3 Valores numéricos [Cant, P.Unit, Total]
                if len(partes) >= 3:
                    try:
                        c = float(partes[-3].replace(",", "."))
                        p = float(partes[-2].replace(",", "."))
                        t = float(partes[-1].replace(",", "."))
                        if c > 0 and t >= 0:
                            cant_val, punit_val, total_val = c, p, t
                            texto_misma_linea = " ".join(partes[:-3]).strip()
                    except ValueError:
                        pass

                # CASO 2: 2 Valores numéricos y Descuento en la línea siguiente
                if cant_val is None and len(partes) >= 2:
                    try:
                        c = float(partes[-2].replace(",", "."))
                        p = float(partes[-1].replace(",", "."))
                        
                        if idx + 1 < len(lineas) and "DESCUENTO:" in lineas[idx + 1].upper():
                            partes_desc = lineas[idx + 1].split()
                            t = float(partes_desc[-1].replace(",", "."))
                            if c > 0 and t >= 0:
                                cant_val, punit_val, total_val = c, p, t
                                texto_misma_linea = " ".join(partes[:-2]).strip()
                    except ValueError:
                        pass

                # CASO 3: 1 Solo valor numérico en la línea
                if cant_val is None and len(partes) == 1:
                    try:
                        val = float(partes[0].replace(",", "."))
                        if val > 0 and idx > 0 and not any(h in lineas[idx-1].upper() for h in ["TOTAL", "SUB-TOTAL", "REMITO", "CANT.", "DETALLE"]):
                            if val < 50 and float(val).is_integer():
                                cant_val = val
                                punit_val = 0.0
                                total_val = 0.0
                                tipo_anomalia = "Falta Precio (Precio $0)"
                            else:
                                cant_val = 1.0
                                punit_val = val
                                total_val = val
                                tipo_anomalia = "Falta Cantidad (Asumida = 1)"
                            texto_misma_linea = ""
                    except ValueError:
                        pass

                # Guardar el registro aplicando el multiplicador (-1 si es Nota de Crédito)
                if cant_val is not None and total_val is not None:
                    descripcion_lineas = []
                    
                    if texto_misma_linea:
                        descripcion_lineas.append(texto_misma_linea)
                        
                    j = idx - 1
                    while j >= 0:
                        prev = lineas[j]
                        prev_upper = prev.upper()
                        
                        if any(h in prev_upper for h in ["CANT.", "P.UNIT.", "TOTAL", "REMITO", "FECHA:", "CONS.FINAL", "DETALLE", "DESCUENTO:"]):
                            break
                        
                        partes_prev = prev.split()
                        if len(partes_prev) >= 1:
                            try:
                                float(partes_prev[-1].replace(",", "."))
                                break
                            except ValueError:
                                pass
                        
                        descripcion_lineas.insert(0, prev)
                        j -= 1
                    
                    detalle_completo = " ".join(descripcion_lineas).strip().upper()
                    
                    if detalle_completo:
                        item_dict = {
                            "Archivo_Origen": archivo,
                            "Fecha": fecha,
                            "Mes": mes_nombre,
                            "Cant": cant_val * multiplicador,
                            "Detalle": detalle_completo,
                            "P. Unit.": punit_val,
                            "Total": total_val * multiplicador
                        }
                        
                        registros.append(item_dict)

                        if tipo_anomalia:
                            item_anomalo = item_dict.copy()
                            item_anomalo["Tipo_Anomalia"] = tipo_anomalia
                            anomalias.append(item_anomalo)

    except Exception:
        pass

if registros:
    print("\n📊 Generando consolidado anual y Matriz BCG...")
    
    df_detalle = pd.DataFrame(registros)
    df_detalle = df_detalle[["Archivo_Origen", "Fecha", "Mes", "Cant", "Detalle", "P. Unit.", "Total"]]

    # 1. Ranking Anual por Producto
    df_ranking_anual = df_detalle.groupby("Detalle", as_index=False).agg(
        Cant_Total_Anual=("Cant", "sum"),
        Total_Recaudado_Anual=("Total", "sum"),
        Precio_Promedio=("P. Unit.", "mean")
    )

    df_ranking_anual["Cant_Total_Anual"] = df_ranking_anual["Cant_Total_Anual"].round(2)
    df_ranking_anual["Total_Recaudado_Anual"] = df_ranking_anual["Total_Recaudado_Anual"].round(2)
    df_ranking_anual["Precio_Promedio"] = df_ranking_anual["Precio_Promedio"].round(2)
    df_ranking_anual = df_ranking_anual.sort_values(by="Total_Recaudado_Anual", ascending=False)

    # 2. Cálculo para la Matriz BCG
    print("Calculando Matriz BCG...")
    matriz_bcg = df_ranking_anual.copy()
    total_general = matriz_bcg['Total_Recaudado_Anual'].sum()
    matriz_bcg['%_Participacion_Ventas'] = ((matriz_bcg['Total_Recaudado_Anual'] / total_general) * 100).round(2)

    mediana_ventas = matriz_bcg['Total_Recaudado_Anual'].median()
    mediana_cant = matriz_bcg['Cant_Total_Anual'].median()

    def clasificar_bcg(row):
        if row['Total_Recaudado_Anual'] >= mediana_ventas and row['Cant_Total_Anual'] >= mediana_cant:
            return 'Estrella (Alta Venta / Alta Rotación)'
        elif row['Total_Recaudado_Anual'] >= mediana_ventas and row['Cant_Total_Anual'] < mediana_cant:
            return 'Vaca Lechera (Alta Venta / Baja Rotación)'
        elif row['Total_Recaudado_Anual'] < mediana_ventas and row['Cant_Total_Anual'] >= mediana_cant:
            return 'Interrogante (Baja Venta / Alta Rotación)'
        else:
            return 'Perro (Baja Venta / Baja Rotación)'

    matriz_bcg['Categoria_BCG'] = matriz_bcg.apply(clasificar_bcg, axis=1)

    # DataFrame de Anomalías
    if anomalias:
        df_anomalias = pd.DataFrame(anomalias)
        df_anomalias = df_anomalias[["Archivo_Origen", "Fecha", "Tipo_Anomalia", "Detalle", "Cant", "P. Unit.", "Total"]]
    else:
        df_anomalias = pd.DataFrame(columns=["Archivo_Origen", "Fecha", "Tipo_Anomalia", "Detalle", "Cant", "P. Unit.", "Total"])

    # Guardar en Libro Excel con múltiples pestañas
    with pd.ExcelWriter(ARCHIVO_SALIDA_EXCEL, engine='openpyxl') as writer:
        df_ranking_anual.to_excel(writer, sheet_name="Ranking_Anual", index=False)
        matriz_bcg.to_excel(writer, sheet_name="Matriz_BCG", index=False)
        df_detalle.to_excel(writer, sheet_name="Detalle_Transacciones_Anual", index=False)
        df_anomalias.to_excel(writer, sheet_name="Anomalias_Auditadas", index=False)
        
    print(f"\n✅ ¡Análisis Anual y Matriz BCG completado con éxito!")
    print(f"📁 Archivo generado: '{ARCHIVO_SALIDA_EXCEL}'")
    print(f"⚠️ Total de anomalías auditadas: {len(anomalias)}")
else:
    print("\n⚠️ No se encontraron registros válidos para procesar.")