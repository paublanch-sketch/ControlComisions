from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
import re
from pathlib import Path
import io
import os

app = Flask(__name__)
CORS(app)

# Carpeta per als PDFs pujats
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)


# ============================================================
# CONFIGURACIÓ TESSERACT
# ============================================================

# Windows:
# Si Tesseract no està al PATH, descomenta aquesta línia
# i posa la ruta correcta.
#
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


# ============================================================
# OCR
# ============================================================

def pdf_to_images(pdf_path):
    """
    Converteix totes les pàgines del PDF en imatges.
    300 DPI aproximadament per millorar l'OCR.
    """

    document = fitz.open(pdf_path)
    images = []

    for page in document:
        # Zoom 3x = bona resolució per OCR
        matrix = fitz.Matrix(3, 3)

        pix = page.get_pixmap(
            matrix=matrix,
            alpha=False
        )

        image_bytes = pix.tobytes("png")
        image = Image.open(io.BytesIO(image_bytes))

        images.append(image)

    document.close()

    return images


def preprocess_image(image):
    """
    Millora la imatge abans de passar-la per OCR.
    """

    # Escala de grisos
    image = image.convert("L")

    # Augmentar contrast
    image = ImageEnhance.Contrast(image).enhance(2.0)

    # Afilar
    image = image.filter(ImageFilter.SHARPEN)

    return image


def perform_ocr(image):
    """
    Executa Tesseract OCR.
    """

    image = preprocess_image(image)

    # psm 6:
    # assumeix un bloc de text uniforme.
    #
    # Si tens factures molt diferents podem provar
    # altres modes posteriorment.
    config = "--oem 3 --psm 6"

    try:
        # Primer intent amb espanyol + català
        text = pytesseract.image_to_string(
            image,
            lang="spa+cat",
            config=config
        )

    except Exception:
        # Si no estan instal·lats els idiomes,
        # intentem amb anglès.
        text = pytesseract.image_to_string(
            image,
            lang="eng",
            config=config
        )

    return text


def extract_all_text(pdf_path):
    """
    OCR de totes les pàgines.
    """

    images = pdf_to_images(pdf_path)

    all_text = []

    for page_number, image in enumerate(images, start=1):

        text = perform_ocr(image)

        all_text.append(
            f"\n--- PÀGINA {page_number} ---\n{text}"
        )

    return "\n".join(all_text)


# ============================================================
# NETEJA OCR
# ============================================================

def normalize_text(text):
    """
    Neteja errors típics de l'OCR.
    """

    if not text:
        return ""

    replacements = {
        "€": "€",
        " Iva": " IVA",
        " lva": " IVA",
        "IGIC": "IGIC",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Eliminar espais duplicats
    text = re.sub(r"[ \t]+", " ", text)

    return text


# ============================================================
# NÚMERO FACTURA
# ============================================================

def extract_invoice_number(text):

    patterns = [
        r"Factura\s*#?\s*([A-Z0-9]+[-/][A-Z0-9-]+)",
        r"Factura\s*#?\s*([A-Z]{2,5}\d+[-/]\d+)",
        r"Factura\s*#?\s*([FES]\w+)",
        r"FES\d+[-]\d+",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            if match.lastindex:
                return match.group(1).strip()

            return match.group(0).strip()

    return None


# ============================================================
# DATA
# ============================================================

def extract_invoice_date(text):

    patterns = [
        r"Fecha\s*:?\s*(\d{2}/\d{2}/\d{4})",
        r"Fecha\s*:?\s*(\d{2}-\d{2}-\d{4})",
        r"Fecha\s*:?\s*(\d{2}\.\d{2}\.\d{4})",
        r"\b(\d{2}/\d{2}/\d{4})\b",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(1)

    return None


# ============================================================
# TOTAL
# ============================================================

def extract_total(text):

    patterns = [
        r"Total\s*:?\s*([0-9\.,]+)\s*€",
        r"TOTAL\s*:?\s*([0-9\.,]+)",
        r"Total factura\s*:?\s*([0-9\.,]+)",
        r"Importe total\s*:?\s*([0-9\.,]+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(1)

    return None


# ============================================================
# PACIENTES
# ============================================================

def extract_patients(text):
    """
    Intenta detectar pacientes a partir de las filas OCR.

    En factures veterinàries normalment el nom del pacient
    apareix entre la data i la descripció del servei.
    """

    patients = []

    lines = text.splitlines()

    for line in lines:

        line = line.strip()

        if not line:
            continue

        # Buscar una fecha al principio
        date_match = re.match(
            r"^(\d{2}/\d{2}/\d{4})\s+(.+)",
            line
        )

        if not date_match:
            continue

        remainder = date_match.group(2).strip()

        # Dividir por espacios
        parts = remainder.split()

        if len(parts) < 2:
            continue

        # Normalmente el paciente aparece después de la fecha
        patient = parts[0].strip()

        # Evitar falsos positivos
        excluded = {
            "TOTAL",
            "IVA",
            "IGIC",
            "BASE",
            "IMPORTE",
            "CANTIDAD",
            "PRECIO",
            "ARTICULO",
            "ARTÍCULO",
        }

        if patient.upper() in excluded:
            continue

        # Los nombres de pacientes suelen ser cortos
        # y estar en mayúsculas.
        if (
            patient.upper() == patient
            and len(patient) >= 2
            and len(patient) <= 40
        ):
            if patient not in patients:
                patients.append(patient)

    return patients


# ============================================================
# TABLA
# ============================================================

def extract_table_rows(text):
    """
    Reconstruye las filas de la factura a partir del OCR.

    Formato esperado:

    FECHA PACIENTE ARTICULO PRECIO CANTIDAD IVA IVA€ IMPORTE
    """

    rows = []

    lines = text.splitlines()

    for line in lines:

        line = line.strip()

        if not line:
            continue

        # ----------------------------------------------------
        # Buscar fecha al principio
        # ----------------------------------------------------

        date_match = re.match(
            r"^(\d{2}/\d{2}/\d{4})\s+(.+)$",
            line
        )

        if not date_match:
            continue

        fecha = date_match.group(1)
        content = date_match.group(2).strip()

        # ----------------------------------------------------
        # Buscar importes al final
        # ----------------------------------------------------

        money_pattern = r"([0-9]{1,3}(?:[.,][0-9]{2})?)\s*€?"

        money_values = re.findall(
            money_pattern,
            content
        )

        # Necesitamos al menos algunos valores numéricos
        if len(money_values) < 2:
            continue

        # ----------------------------------------------------
        # Cantidad
        # ----------------------------------------------------

        quantity_match = re.search(
            r"\b(\d+(?:[.,]\d+)?)\b",
            content
        )

        cantidad = (
            quantity_match.group(1)
            if quantity_match
            else ""
        )

        # ----------------------------------------------------
        # IVA
        # ----------------------------------------------------

        iva_match = re.search(
            r"(\d{1,2})\s*%",
            content
        )

        iva = (
            iva_match.group(1) + " %"
            if iva_match
            else ""
        )

        # ----------------------------------------------------
        # Separar texto de números
        # ----------------------------------------------------

        text_part = re.sub(
            r"\d{1,3}(?:[.,]\d{2})?\s*€?",
            " ",
            content
        )

        text_part = re.sub(
            r"\d{1,2}\s*%",
            " ",
            text_part
        )

        text_part = re.sub(
            r"\s+",
            " ",
            text_part
        ).strip()

        words = text_part.split()

        if len(words) < 2:
            continue

        # Primer elemento = paciente
        paciente = words[0]

        # Resto = artículo
        articulo = " ".join(words[1:])

        # ----------------------------------------------------
        # Valores monetarios
        # ----------------------------------------------------

        precio = ""
        iva_valor = ""
        importe = ""

        if len(money_values) >= 1:
            precio = money_values[0] + " €"

        if len(money_values) >= 2:
            importe = money_values[-1] + " €"

        if len(money_values) >= 3:
            iva_valor = money_values[-2] + " €"

        row = {
            "fecha": fecha,
            "paciente": paciente,
            "articulo": articulo,
            "precio": precio,
            "cantidad": cantidad,
            "iva_igic": iva,
            "iva_valor": iva_valor,
            "importe": importe
        }

        rows.append(row)

    return rows


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def extract_table_data(pdf_path):

    print("\n========================================")
    print("INICIANDO OCR")
    print("========================================\n")

    # OCR completo
    full_text = extract_all_text(pdf_path)

    full_text = normalize_text(full_text)

    # Mostrar OCR en terminal para poder depurar
    print("\n========== TEXTO OCR ==========\n")
    print(full_text)
    print("\n========== FIN OCR ============\n")

    # Datos
    invoice_number = extract_invoice_number(full_text)
    invoice_date = extract_invoice_date(full_text)
    total = extract_total(full_text)

    # Tabla
    table_rows = extract_table_rows(full_text)

    # Pacientes
    patients = extract_patients(full_text)

    # Si hemos encontrado pacientes en la tabla,
    # damos prioridad a esos.
    for row in table_rows:

        patient = row.get("paciente", "").strip()

        if patient and patient not in patients:
            patients.append(patient)

    data = {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "total": total,
        "patients": patients,
        "table_rows": table_rows
    }

    return data


# ============================================================
# UPLOAD
# ============================================================

@app.route("/upload", methods=["POST"])
def upload_file():

    try:

        if "file" not in request.files:
            return jsonify({
                "error": "No file provided"
            }), 400

        file = request.files["file"]

        if file.filename == "":
            return jsonify({
                "error": "No file selected"
            }), 400

        if not file.filename.lower().endswith(".pdf"):
            return jsonify({
                "error": "Only PDF files are allowed"
            }), 400

        # Nombre seguro
        filename = Path(file.filename).name

        filepath = UPLOAD_FOLDER / filename

        file.save(filepath)

        print(f"\nPDF recibido: {filepath}")

        # OCR
        extracted_data = extract_table_data(filepath)

        return jsonify({
            "success": True,
            "data": extracted_data,
            "filename": filename
        }), 200

    except Exception as e:

        print("\nERROR:")
        print(str(e))

        return jsonify({
            "error": f"Error processing file: {str(e)}"
        }), 500


# ============================================================
# HEALTH
# ============================================================

@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "ok",
        "ocr": "enabled"
    }), 200


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print("\n========================================")
    print(" EXTRACTOR DE FACTURAS OCR")
    print(" http://localhost:5000")
    print("========================================\n")

    app.run(
        debug=True,
        host="localhost",
        port=5000
    )

