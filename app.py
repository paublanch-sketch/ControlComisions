import gc
import io
import os
import re
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from PIL import Image, ImageEnhance
import pytesseract


# ============================================================
# CONFIGURACIÓ FLASK
# ============================================================

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
MAX_PAGES = 5

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE


# ============================================================
# CONFIGURACIÓ TESSERACT
# ============================================================

_TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD


# ============================================================
# RUTES BÀSIQUES
# ============================================================

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ocr": "enabled"}), 200


# ============================================================
# PROCESAMIENTO OCR
# ============================================================

def preprocess_image(image):
    image = image.convert("L")
    image = ImageEnhance.Contrast(image).enhance(2.0)
    threshold = 180
    return image.point(lambda x: 0 if x < threshold else 255, "1")


def perform_ocr(image):
    processed_img = preprocess_image(image)
    config = "--oem 3 --psm 6"
    languages_to_try = ["spa", "spa+cat", "eng"]

    for lang in languages_to_try:
        try:
            return pytesseract.image_to_string(processed_img, lang=lang, config=config)
        except Exception as error:
            print(f"No s'ha pogut utilitzar {lang}:", error)

    return ""


def extract_all_text(pdf_path):
    document = None
    all_text = []

    try:
        document = fitz.open(pdf_path)
        page_count = len(document)

        print(f"\nPDF obert: {page_count} pàgines")

        if page_count > MAX_PAGES:
            raise ValueError(f"El PDF té {page_count} pàgines. El màxim és {MAX_PAGES}.")

        for page_number in range(page_count):
            print(f"\nProcessant pàgina {page_number + 1}/{page_count}...")
            page = document.load_page(page_number)

            native_text = page.get_text("text")
            if native_text and len(native_text.strip()) >= 30:
                print(f"Pàgina {page_number + 1}: text natiu detectat → sense OCR")
                all_text.append(f"\n--- PÀGINA {page_number + 1} ---\n{native_text}")
                continue

            print(f"Pàgina {page_number + 1}: escanejada → executant OCR")
            matrix = fitz.Matrix(3.0, 3.0)
            pix = page.get_pixmap(matrix=matrix, alpha=False)

            image = Image.open(io.BytesIO(pix.tobytes("png")))
            image.load()

            text = perform_ocr(image)
            all_text.append(f"\n--- PÀGINA {page_number + 1} ---\n{text}")

            image.close()
            pix = None
            page = None
            gc.collect()

        return "\n".join(all_text)

    finally:
        if document:
            document.close()
        gc.collect()


# ============================================================
# NETEJA I EXTRACCIÓ DE DADES
# ============================================================

def normalize_text(text):
    if not text:
        return ""
    replacements = {
        " Iva": " IVA", " lva": " IVA",
        "Iva": "IVA", "lva": "IVA",
        "IGIC": "IGIC"
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"[ \t]+", " ", text)


def extract_invoice_number(text):
    patterns = [
        r"Factura\s*#?\s*([A-Z0-9]+[-/][A-Z0-9-]+)",
        r"Factura\s*#?\s*([A-Z]{2,5}\d+[-/]\d+)",
        r"\bFES\d+[-]\d+\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip() if match.lastindex else match.group(0).strip()
    return None


def extract_invoice_date(text):
    patterns = [
        r"Fecha\s*:?\s*(\d{2}/\d{2}/\d{4})",
        r"\b(\d{2}/\d{2}/\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_total(text):
    # Buscar el total final (155,81 € / 170,00 €)
    matches = re.findall(r"(\d{1,4}[.,]\d{2})\s*€", text)
    if matches:
        return matches[-1] + " €"
    return None


def extract_patients(text):
    patients = []
    # Buscar la línea: Pacientes: LLUC (1541919), Macho... / ZOE (1771852)...
    match = re.search(r"Pacientes?\s*:\s*([A-ZÀ-Úa-zà-ú]+)", text, re.IGNORECASE)
    if match:
        patients.append(match.group(1).upper().strip())

    return patients


def extract_table_rows(text):
    rows = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    
    # 1. Obtener los nombres de los pacientes de la factura
    known_patients = extract_patients(text)
    default_date = extract_invoice_date(text) or ""
    
    # Palabras clave para detener la lectura de la tabla de artículos
    stop_keywords = ["TOTAL", "SUBTOTAL", "PAGOS", "PENDIENTE DE PAGO", "TODO", "TIPO DE PAGO"]

    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Descartar cabeceras y pie de página
        if any(keyword == line.upper() or line.upper().startswith(keyword) for keyword in stop_keywords):
            break
            
        if "EXCL.IVA" in line.upper() or "ARTÍCULOS" in line.upper() or "PACIENTE" in line.upper():
            i += 1
            continue

        # Detectar si la línea contiene importes en euros
        decimal_matches = re.findall(r"(\d{1,4}[.,]\d{2})\s*€?", line)
        
        if decimal_matches:
            # Comprobar si hay una fecha al inicio de la línea o en líneas superiores
            fecha_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", line)
            fecha = fecha_match.group(1) if fecha_match else default_date
            
            # Buscar el paciente en la línea actual
            paciente = ""
            for p in known_patients:
                if p in line.upper():
                    paciente = p
                    break
            
            if not paciente and known_patients:
                paciente = known_patients[0]

            # Extraer porcentaje IVA (%)
            iva_match = re.search(r"(\d{1,2})\s*%", line)
            iva = iva_match.group(1) + " %" if iva_match else "21 %"

            # Precios
            if len(decimal_matches) >= 3:
                precio = decimal_matches[-3] + " €"
                iva_valor = decimal_matches[-2] + " €"
                importe = decimal_values = decimal_matches[-1] + " €"
            elif len(decimal_matches) == 2:
                precio = decimal_matches[0] + " €"
                iva_valor = ""
                importe = decimal_matches[1] + " €"
            else:
                precio = decimal_matches[0] + " €"
                iva_valor = ""
                importe = decimal_matches[0] + " €"

            # Limpiar importes, fechas y caracteres especiales para aislar el nombre del Artículo
            clean_text = line
            clean_text = re.sub(r"\b\d{2}/\d{2}/\d{4}\b", "", clean_text)
            clean_text = re.sub(r"\d{1,4}[.,]\d{2}\s*€?", "", clean_text)
            clean_text = re.sub(r"\d{1,2}\s*%", "", clean_text)
            
            if paciente:
                clean_text = re.sub(rf"\b{paciente}\b", "", clean_text, flags=re.IGNORECASE)

            # Limpiar barras '|' procedentes de tablas en PDF
            clean_text = clean_text.replace("|", " ")
            words = clean_text.strip().split()

            cantidad = "1"
            # Buscar cantidad (ej. 1, 2, 1 Caja...)
            for idx in range(len(words) - 1, -1, -1):
                if words[idx].isdigit():
                    cantidad = words[idx]
                    words.pop(idx)
                    break
                elif "Caja" in words[idx] or "Unidad" in words[idx]:
                    cantidad = words[idx]

            articulo = " ".join(words).strip()

            # Guardar la fila si el artículo tiene un nombre coherente
            if articulo and len(articulo) > 2 and articulo.upper() not in stop_keywords:
                rows.append({
                    "fecha": fecha,
                    "paciente": paciente,
                    "articulo": articulo,
                    "precio": precio,
                    "cantidad": cantidad,
                    "iva_igic": iva,
                    "iva_valor": iva_valor,
                    "importe": importe
                })

        i += 1

    return rows


def extract_table_data(pdf_path):
    print("\n========================================\n        INICIANT PROCESSAMENT\n========================================\n")
    
    full_text = normalize_text(extract_all_text(pdf_path))

    print("\n========== TEXT EXTRET ==========\n")
    print(full_text)
    print("\n========== FI TEXT ===============\n")

    table_rows = extract_table_rows(full_text)
    patients = extract_patients(full_text)

    return {
        "invoice_number": extract_invoice_number(full_text),
        "invoice_date": extract_invoice_date(full_text),
        "total": extract_total(full_text),
        "patients": patients,
        "table_rows": table_rows
    }


# ============================================================
# ENDPOINTS Y MANEJO DE ERRORES
# ============================================================

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"success": False, "error": "El PDF és massa gran. La mida màxima és de 20 MB."}), 413


@app.route("/upload", methods=["POST"])
def upload_file():
    filepath = None
    try:
        if "file" not in request.files or request.files["file"].filename == "":
            return jsonify({"success": False, "error": "Fitxer no proporcionat o no seleccionat."}), 400

        file = request.files["file"]
        if not file.filename.lower().endswith(".pdf"):
            return jsonify({"success": False, "error": "Només s'accepten fitxers PDF."}), 400

        original_filename = Path(file.filename).name
        unique_filename = f"{uuid.uuid4().hex}_{original_filename}"
        filepath = UPLOAD_FOLDER / unique_filename

        file.save(filepath)
        print(f"PDF rebut: {original_filename} ({filepath.stat().st_size / (1024 * 1024):.2f} MB)")

        extracted_data = extract_table_data(filepath)

        return jsonify({"success": True, "data": extracted_data, "filename": original_filename}), 200

    except Exception as error:
        print(f"\n========================================\n              ERROR\n========================================\n{error}\n")
        return jsonify({"success": False, "error": f"S'ha produït un error processant el fitxer: {str(error)}"}), 500

    finally:
        if filepath and filepath.exists():
            try:
                filepath.unlink()
                print("PDF temporal eliminat.")
            except Exception as cleanup_error:
                print(f"No s'ha pogut eliminar el PDF temporal: {cleanup_error}")
        gc.collect()


# ============================================================
# EXECUCIÓ
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\nServidor actiu a: http://localhost:{port}/")
    app.run(debug=True, host="0.0.0.0", port=port)