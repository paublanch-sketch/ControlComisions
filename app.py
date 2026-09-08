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
            raise ValueError(f"El PDF té {page_count} pàgines. El màxim permès és {MAX_PAGES}.")

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
        r"Factura\s*#?\s*([FES]\w+)",
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
        r"Fecha\s*:?\s*(\d{2}-\d{2}-\d{4})",
        r"Fecha\s*:?\s*(\d{2}\.\d{2}\.\d{4})",
        r"\b(\d{2}/\d{2}/\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_total(text):
    patterns = [
        r"Total\s*:?\s*([0-9\.,]+)\s*€",
        r"TOTAL\s*:?\s*([0-9\.,]+)",
        r"Total factura\s*:?\s*([0-9\.,]+)",
        r"Importe total\s*:?\s*([0-9\.,]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_patients(text):
    patients = []
    excluded = {
        "TOTAL", "IVA", "IGIC", "BASE", "IMPORTE",
        "CANTIDAD", "PRECIO", "ARTICULO", "ARTÍCULO", "FACTURA", "FECHA"
    }

    for line in text.splitlines():
        line = line.strip()
        date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", line)
        if not date_match:
            continue

        parts = line.split()
        for part in parts:
            clean_part = re.sub(r"[^\w]", "", part)
            if (
                clean_part.isupper()
                and clean_part not in excluded
                and len(clean_part) >= 2
                and clean_part not in patients
                and not clean_part.isdigit()
            ):
                patients.append(clean_part)

    return patients


def extract_table_rows(text):
    rows = []
    last_date = extract_invoice_date(text) or ""
    decimal_pattern = r"(\d{1,4}[.,]\d{2})\s*€?"
    excluded_keywords = ["TOTAL", "SUBTOTAL", "BASE IMPONIBLE", "VALOR", "IMPORTE TOTAL"]

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Evitar procesar las líneas finales de sumatorio
        if any(keyword in line.upper() for keyword in excluded_keywords):
            continue

        # Si la línea tiene fecha, actualizar last_date
        date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", line)
        if date_match:
            last_date = date_match.group(1)

        decimal_values = re.findall(decimal_pattern, line)
        
        # Flexibilización: procesar si la línea contiene al menos 1 importe económico
        if not decimal_values:
            continue

        if len(decimal_values) >= 3:
            precio = decimal_values[-3] + " €"
            iva_valor = decimal_values[-2] + " €"
            importe = decimal_values[-1] + " €"
        elif len(decimal_values) == 2:
            precio = decimal_values[0] + " €"
            iva_valor = ""
            importe = decimal_values[1] + " €"
        else:
            precio = decimal_values[0] + " €"
            iva_valor = ""
            importe = decimal_values[0] + " €"

        iva_match = re.search(r"(\d{1,2})\s*%", line)
        iva = iva_match.group(1) + " %" if iva_match else ""

        # Limpiar precios e IVA del texto para aislar la descripción
        content = re.sub(r"\b\d{2}/\d{2}/\d{4}\b", "", line)
        content = re.sub(decimal_pattern, " ", content)
        content = re.sub(r"\d{1,2}\s*%", " ", content)
        words = re.sub(r"\s+", " ", content).strip().split()

        if not words:
            continue

        paciente = ""
        # Buscar si alguna palabra coincide con un nombre de persona (en mayúsculas)
        for word in words:
            clean_w = re.sub(r"[^\w]", "", word)
            if clean_w.isupper() and len(clean_w) >= 2 and not clean_w.isdigit():
                paciente = clean_w
                break

        cantidad = "1"
        rest_words = [w for w in words if w != paciente]

        for i in range(len(rest_words) - 1, -1, -1):
            if re.fullmatch(r"\d{1,3}", rest_words[i]):
                cantidad = rest_words[i]
                rest_words = rest_words[:i] + rest_words[i + 1:]
                break

        articulo = " ".join(rest_words).strip()
        if not articulo:
            articulo = "Concepto general"

        rows.append({
            "fecha": last_date,
            "paciente": paciente,
            "articulo": articulo,
            "precio": precio,
            "cantidad": cantidad,
            "iva_igic": iva,
            "iva_valor": iva_valor,
            "importe": importe
        })

    return rows


def extract_table_data(pdf_path):
    print("\n========================================\n        INICIANT PROCESSAMENT\n========================================\n")
    
    full_text = normalize_text(extract_all_text(pdf_path))

    print("\n========== TEXT EXTRET ==========\n")
    print(full_text)
    print("\n========== FI TEXT ===============\n")

    table_rows = extract_table_rows(full_text)
    patients = extract_patients(full_text)

    for row in table_rows:
        patient = row.get("paciente", "").strip()
        if patient and patient not in patients:
            patients.append(patient)

    return {
        "invoice_number": extract_invoice_number(full_text),
        "invoice_date": extract_invoice_date(full_text),
        "total": extract_total(full_text),
        "patients": patients,
        "table_rows": table_rows
    }


# ============================================================
# ENDPOINTS
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