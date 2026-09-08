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

# Carpeta per als PDFs pujats
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)

# Límit màxim del PDF (20 MB)
MAX_FILE_SIZE = 20 * 1024 * 1024

# Màxim de pàgines per PDF
MAX_PAGES = 5

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE


# ============================================================
# CONFIGURACIÓ TESSERACT
# ============================================================

_TESSERACT_CMD = os.environ.get("TESSERACT_CMD")

if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD


# ============================================================
# PÀGINA PRINCIPAL I HEALTH
# ============================================================

@app.route("/")
def index():
    """
    Carrega el fitxer index.html.
    """
    return send_from_directory(".", "index.html")


@app.route("/health", methods=["GET"])
def health():
    """
    Comprova l'estat del servei.
    """
    return jsonify({
        "status": "ok",
        "ocr": "enabled"
    }), 200


# ============================================================
# OCR
# ============================================================

def preprocess_image(image):
    """
    Millora la imatge abans de passar-la per OCR (Grisos + Contrast + Binarització).
    """
    image = image.convert("L")

    image = ImageEnhance.Contrast(
        image
    ).enhance(2.0)

    threshold = 180

    image = image.point(
        lambda x: 0 if x < threshold else 255,
        "1"
    )

    return image


def perform_ocr(image):
    """
    Executa Tesseract OCR provant diferents idiomes segons rendiment.
    """
    image = preprocess_image(image)

    config = "--oem 3 --psm 6"

    # Idiomes a provar en ordre de velocitat
    languages = ["spa", "spa+cat", "eng"]

    for lang in languages:
        try:
            text = pytesseract.image_to_string(
                image,
                lang=lang,
                config=config
            )
            return text
        except Exception as error:
            print(f"No s'ha pogut utilitzar {lang}:", error)

    return ""


# ============================================================
# OCR DEL PDF
# ============================================================

def extract_all_text(pdf_path):
    """
    Processa el PDF pàgina per pàgina gestionant la memòria RAM.
    """
    document = None
    all_text = []

    try:
        document = fitz.open(pdf_path)
        page_count = len(document)

        print()
        print(f"PDF obert: {page_count} pàgines")

        if page_count > MAX_PAGES:
            raise ValueError(
                f"El PDF té {page_count} pàgines. "
                f"El màxim permès és {MAX_PAGES}."
            )

        for page_number in range(page_count):

            print()
            print(f"Processant pàgina {page_number + 1}/{page_count}...")

            page = None
            pix = None
            image = None

            try:
                page = document.load_page(page_number)

                # 1. Intentar text natiu
                native_text = page.get_text("text")

                if native_text and len(native_text.strip()) >= 30:
                    print(
                        f"Pàgina {page_number + 1}: "
                        "text natiu detectat → sense OCR"
                    )
                    all_text.append(
                        f"\n--- PÀGINA {page_number + 1} ---\n"
                        f"{native_text}"
                    )
                    continue

                # 2. Si no hi ha text → OCR
                print(
                    f"Pàgina {page_number + 1}: "
                    "escanejada → executant OCR"
                )

                matrix = fitz.Matrix(3.0, 3.0)

                pix = page.get_pixmap(
                    matrix=matrix,
                    alpha=False
                )

                image_bytes = pix.tobytes("png")

                image = Image.open(
                    io.BytesIO(image_bytes)
                )

                image.load()

                text = perform_ocr(image)

                all_text.append(
                    f"\n--- PÀGINA {page_number + 1} ---\n"
                    f"{text}"
                )

            finally:
                if image is not None:
                    try:
                        image.close()
                    except Exception:
                        pass

                pix = None
                page = None
                gc.collect()

        return "\n".join(all_text)

    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass

        gc.collect()


# ============================================================
# NETEJA I EXTRACCIÓ
# ============================================================

def normalize_text(text):
    """
    Neteja errors típics de l'OCR.
    """
    if not text:
        return ""

    replacements = {
        " Iva": " IVA",
        " lva": " IVA",
        "Iva": "IVA",
        "lva": "IVA",
        "IGIC": "IGIC",
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
            if match.lastindex:
                return match.group(1).strip()
            return match.group(0).strip()

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
        "TOTAL", "IVA", "IGIC", "BASE",
        "IMPORTE", "CANTIDAD", "PRECIO",
        "ARTICULO", "ARTÍCULO"
    }

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        date_match = re.match(
            r"^(\d{2}/\d{2}/\d{4})\s+(.+)",
            line
        )

        if not date_match:
            continue

        remainder = date_match.group(2).strip()
        parts = remainder.split()

        if len(parts) < 2:
            continue

        patient = parts[0].strip()

        if patient.upper() in excluded:
            continue

        if (
            patient.upper() == patient
            and 2 <= len(patient) <= 40
            and patient not in patients
        ):
            patients.append(patient)

    return patients


def extract_table_rows(text):
    rows = []
    lines = text.splitlines()

    last_date = None
    last_patient = None

    decimal_pattern = r"(\d{1,4}[.,]\d{2})\s*€?"

    for line in lines:
        line = line.strip()

        if not line:
            continue

        date_match = re.match(
            r"^(\d{2}/\d{2}/\d{4})\s+(.+)$",
            line
        )

        if date_match:
            fecha = date_match.group(1)
            content = date_match.group(2).strip()

        elif (
            last_date
            and last_patient
            and line.upper().startswith(last_patient.upper())
            and re.search(r"\d[.,]\d{2}", line)
        ):
            fecha = last_date
            content = line

        else:
            continue

        decimal_values = re.findall(decimal_pattern, content)

        if len(decimal_values) < 2:
            continue

        precio = decimal_values[-3] + " €" if len(decimal_values) >= 3 else (decimal_values[0] + " €")
        iva_valor = decimal_values[-2] + " €" if len(decimal_values) >= 2 else ""
        importe = decimal_values[-1] + " €"

        iva_match = re.search(r"(\d{1,2})\s*%", content)
        iva = iva_match.group(1) + " %" if iva_match else ""

        remainder = re.sub(decimal_pattern, " ", content)
        remainder = re.sub(r"\d{1,2}\s*%", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip()

        words = remainder.split()

        if len(words) < 2:
            continue

        paciente = words[0]
        cantidad = "1"
        rest_words = words[1:]

        for i in range(len(rest_words) - 1, -1, -1):
            if re.fullmatch(r"\d{1,3}", rest_words[i]):
                cantidad = rest_words[i]
                rest_words = rest_words[:i] + rest_words[i + 1:]
                break

        articulo = " ".join(rest_words).strip()

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

        last_date = fecha
        last_patient = paciente

    return rows


def extract_table_data(pdf_path):
    print()
    print("========================================")
    print("        INICIANT PROCESSAMENT")
    print("========================================")
    print()

    full_text = extract_all_text(pdf_path)
    full_text = normalize_text(full_text)

    print()
    print("========== TEXT EXTRET ==========")
    print()
    print(full_text)
    print()
    print("========== FI TEXT ===============")
    print()

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
    return jsonify({
        "success": False,
        "error": "El PDF és massa gran. La mida màxima és de 20 MB."
    }), 413


@app.route("/upload", methods=["POST"])
def upload_file():
    filepath = None

    try:
        print()
        print("========================================")
        print("                 NOU PDF")
        print("========================================")
        print()

        if "file" not in request.files or request.files["file"].filename == "":
            return jsonify({
                "success": False,
                "error": "No s'ha proporcionat cap fitxer."
            }), 400

        file = request.files["file"]

        if not file.filename.lower().endswith(".pdf"):
            return jsonify({
                "success": False,
                "error": "Només s'accepten fitxers PDF."
            }), 400

        original_filename = Path(file.filename).name
        unique_filename = f"{uuid.uuid4().hex}_{original_filename}"
        filepath = UPLOAD_FOLDER / unique_filename

        file.save(filepath)

        extracted_data = extract_table_data(filepath)

        return jsonify({
            "success": True,
            "data": extracted_data,
            "filename": original_filename
        }), 200

    except Exception as error:
        return jsonify({
            "success": False,
            "error": f"S'ha produït un error processant el fitxer: {str(error)}"
        }), 500

    finally:
        if filepath is not None and filepath.exists():
            try:
                filepath.unlink()
                print("PDF temporal eliminat.")
            except Exception as cleanup_error:
                print(f"No s'ha pogut eliminar el PDF temporal: {cleanup_error}")

        gc.collect()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    print()
    print("========================================")
    print("     EXTRACTOR DE FACTURES OCR")
    print("========================================")
    print()

    app.run(
        debug=True,
        host="0.0.0.0",
        port=port
    )