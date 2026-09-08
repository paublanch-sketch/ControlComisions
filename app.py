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
# CONFIGURACIÓ FLASK I ENTORNO
# ============================================================

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
MAX_PAGES = 5
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

_TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

# ============================================================
# RUTES
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
    return image.point(lambda x: 0 if x < 180 else 255, "1")

def perform_ocr(image):
    processed_img = preprocess_image(image)
    config = "--oem 3 --psm 6"
    for lang in ["spa", "spa+cat", "eng"]:
        try:
            return pytesseract.image_to_string(processed_img, lang=lang, config=config)
        except Exception as err:
            print(f"Error idioma {lang}:", err)
    return ""

def extract_all_text(pdf_path):
    document = fitz.open(pdf_path)
    all_text = []
    try:
        if len(document) > MAX_PAGES:
            raise ValueError(f"El PDF té {len(document)} pàgines. Màxim permès: {MAX_PAGES}.")

        for page_num in range(len(document)):
            page = document.load_page(page_num)
            native_text = page.get_text("text")

            if native_text and len(native_text.strip()) >= 30:
                all_text.append(f"\n--- PÀGINA {page_num + 1} ---\n{native_text}")
                continue

            matrix = fitz.Matrix(3.0, 3.0)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            image.load()

            text = perform_ocr(image)
            all_text.append(f"\n--- PÀGINA {page_num + 1} ---\n{text}")

            image.close()
            pix = None
            gc.collect()

        return "\n".join(all_text)
    finally:
        document.close()
        gc.collect()

# ============================================================
# PARSER Y EXTRACCIÓN DE DATOS
# ============================================================

def normalize_text(text):
    if not text:
        return ""
    text = text.replace(" Iva", " IVA").replace(" lva", " IVA")
    return re.sub(r"[ \t]+", " ", text)

def extract_invoice_number(text):
    match = re.search(r"Factura\s*#?\s*([A-Z0-9-]+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    fallback = re.search(r"\bFES\d+[-]\d+\b", text)
    return fallback.group(0).strip() if fallback else None

def extract_invoice_date(text):
    match = re.search(r"Fecha\s*:?\s*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if match:
        return match.group(1)
    fallback = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", text)
    return fallback.group(0) if fallback else None

def extract_total(text):
    matches = re.findall(r"(\d{1,4}[.,]\d{2})", text)
    return matches[-1] if matches else "0,00"

def extract_patients(text):
    patients = []
    match = re.search(r"Pacientes?\s*:\s*([A-ZÀ-Úa-zà-ú]+)", text, re.IGNORECASE)
    if match:
        patients.append(match.group(1).upper().strip())
    
    # Búsqueda secundaria en el texto si no encuentra la etiqueta directa
    for line in text.splitlines():
        if re.search(r"\b(LLUC|ZOE)\b", line, re.IGNORECASE):
            name = re.search(r"\b(LLUC|ZOE)\b", line, re.IGNORECASE).group(0).upper()
            if name not in patients:
                patients.append(name)
    return patients

def extract_table_rows(text):
    rows = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    
    known_patients = extract_patients(text)
    default_patient = known_patients[0] if known_patients else ""
    default_date = extract_invoice_date(text) or ""

    stop_keywords = ["PAGOS", "PENDIENTE DE PAGO", "RESPONSABLE DEL TRATAMIENTO", "ANICURA SPAIN"]
    current_articulo = ""

    for line in lines:
        if any(keyword in line.upper() for keyword in stop_keywords):
            break

        if "EXCL.IVA" in line.upper() or "ARTÍCULOS" in line.upper() or "PRECIO" in line.upper():
            continue

        decimal_matches = re.findall(r"(\d{1,4}[.,]\d{2})", line)

        if "%" in line and decimal_matches:
            fecha_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", line)
            fecha = fecha_match.group(1) if fecha_match else default_date

            paciente = default_patient
            for p in known_patients:
                if p in line.upper():
                    paciente = p
                    break

            iva_match = re.search(r"(\d{1,2})\s*%", line)
            iva = iva_match.group(1) + "%" if iva_match else "21%"

            if len(decimal_matches) >= 3:
                precio = decimal_matches[-3]
                iva_valor = decimal_matches[-2]
                importe = decimal_matches[-1]
            elif len(decimal_matches) == 2:
                precio = decimal_matches[0]
                iva_valor = ""
                importe = decimal_matches[1]
            else:
                precio = decimal_matches[0]
                iva_valor = ""
                importe = decimal_matches[0]

            clean = line
            clean = re.sub(r"\b\d{2}/\d{2}/\d{4}\b", "", clean)
            clean = re.sub(r"\d{1,4}[.,]\d{2}", "", clean)
            clean = re.sub(r"\d{1,2}\s*%", "", clean)
            clean = clean.replace("|", " ").replace("€", "")

            if paciente:
                clean = re.sub(rf"\b{paciente}\b", "", clean, flags=re.IGNORECASE)

            words = clean.strip().split()
            cantidad = "1"
            nombre_words = []

            for w in words:
                if w.isdigit() and len(w) <= 2:
                    cantidad = w
                elif "Caja" in w or "Unidad" in w:
                    cantidad = w
                else:
                    nombre_words.append(w)

            articulo_linea = " ".join(nombre_words).strip()
            articulo_final = f"{current_articulo} {articulo_linea}".strip() if current_articulo else articulo_linea
            current_articulo = ""

            if articulo_final:
                rows.append({
                    "fecha": fecha,
                    "paciente": paciente,
                    "articulo": articulo_final,
                    "precio": precio,
                    "cantidad": cantidad,
                    "iva_igic": iva,
                    "iva_valor": iva_valor,
                    "importe": importe
                })
        else:
            clean_text = line.replace("|", " ").strip()
            if clean_text and clean_text.upper() not in [default_patient, "TOTAL", "SUBTOTAL"]:
                if not re.search(r"\b\d{2}/\d{2}/\d{4}\b", clean_text):
                    current_articulo = f"{current_articulo} {clean_text}".strip()

    return rows

def extract_table_data(pdf_path):
    full_text = normalize_text(extract_all_text(pdf_path))
    return {
        "invoice_number": extract_invoice_number(full_text),
        "invoice_date": extract_invoice_date(full_text),
        "total": extract_total(full_text),
        "patients": extract_patients(full_text),
        "table_rows": extract_table_rows(full_text)
    }

# ============================================================
# UPLOAD ENDPOINT
# ============================================================

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"success": False, "error": "El PDF és massa gran (màxim 20 MB)."}), 413

@app.route("/upload", methods=["POST"])
def upload_file():
    filepath = None
    try:
        if "file" not in request.files or request.files["file"].filename == "":
            return jsonify({"success": False, "error": "Fitxer no proporcionat."}), 400

        file = request.files["file"]
        if not file.filename.lower().endswith(".pdf"):
            return jsonify({"success": False, "error": "Només s'accepten fitxers PDF."}), 400

        original_filename = Path(file.filename).name
        filepath = UPLOAD_FOLDER / f"{uuid.uuid4().hex}_{original_filename}"
        file.save(filepath)

        extracted_data = extract_table_data(filepath)

        return jsonify({"success": True, "data": extracted_data, "filename": original_filename}), 200

    except Exception as error:
        return jsonify({"success": False, "error": f"Error en processar: {str(error)}"}), 500

    finally:
        if filepath and filepath.exists():
            filepath.unlink()
        gc.collect()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)