from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import fitz  # PyMuPDF
import pytesseract

from PIL import Image, ImageEnhance
import re
from pathlib import Path
import io
import gc
import uuid
import os


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
# PÀGINA PRINCIPAL
# ============================================================

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ============================================================
# OCR
# ============================================================

def preprocess_image(image):
    """
    Millora la imatge abans de passar-la per OCR.

    Les factures AniCura són escanejades i mantenen sempre
    el mateix format visual.
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
    OCR amb castellà.
    """

    image = preprocess_image(image)

    config = "--oem 3 --psm 6"

    try:
        return pytesseract.image_to_string(
            image,
            lang="spa",
            config=config
        )
    except Exception as error:
        print("Error OCR spa:", error)

    try:
        return pytesseract.image_to_string(
            image,
            lang="eng",
            config=config
        )
    except Exception as error:
        print("Error OCR eng:", error)
        return ""


# ============================================================
# OCR DEL PDF
# ============================================================

def extract_all_text(pdf_path):
    """
    Processa el PDF pàgina per pàgina.

    Com que el format és fix, utilitzem OCR només si
    la pàgina no conté text natiu suficient.
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

            print(
                f"Processant pàgina "
                f"{page_number + 1}/{page_count}..."
            )

            page = None
            pix = None
            image = None

            try:

                page = document.load_page(
                    page_number
                )

                # ------------------------------------------------
                # TEXT NATIU
                # ------------------------------------------------

                native_text = page.get_text("text")

                if native_text and len(
                    native_text.strip()
                ) >= 30:

                    print(
                        f"Pàgina {page_number + 1}: "
                        "text natiu detectat"
                    )

                    all_text.append(
                        f"\n--- PÀGINA {page_number + 1} ---\n"
                        f"{native_text}"
                    )

                    continue

                # ------------------------------------------------
                # OCR
                # ------------------------------------------------

                print(
                    f"Pàgina {page_number + 1}: "
                    "executant OCR"
                )

                matrix = fitz.Matrix(3.0, 3.0)

                pix = page.get_pixmap(
                    matrix=matrix,
                    alpha=False
                )

                image_bytes = pix.tobytes(
                    "png"
                )

                image = Image.open(
                    io.BytesIO(image_bytes)
                )

                image.load()

                text = perform_ocr(
                    image
                )

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
# NORMALITZACIÓ
# ============================================================

def normalize_text(text):
    """
    Neteja errors comuns de OCR sense destruir les línies.
    """

    if not text:
        return ""

    replacements = {
        " Iva": " IVA",
        " lva": " IVA",
        "Iva": "IVA",
        "lva": "IVA",
        "Igic": "IGIC",
        "IGlC": "IGIC",
        "Todo": "Total",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Només traiem espais/tabs repetits.
    # IMPORTANT: NO eliminem salts de línia perquè
    # els necessitem per detectar les files.
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    return text


# ============================================================
# NÚMERO DE FACTURA
# ============================================================

def extract_invoice_number(text):

    patterns = [
        r"Factura\s*#?\s*([A-Z0-9]+-\d+)",
        r"\b(FES\d+-\d+)\b",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(1).strip()

    return None


# ============================================================
# DATA
# ============================================================

def extract_invoice_date(text):

    patterns = [
        r"Fecha\s*:?\s*(\d{2}/\d{2}/\d{4})",
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
# TOTAL FACTURA
# ============================================================

def extract_total(text):
    """
    A les factures AniCura:

        Total 27,04 € 155,81 €

    27,04 = IVA
    155,81 = total factura

    Per tant, agafem SEMPRE el segon import.
    """

    patterns = [

        r"\bTotal\s+"
        r"(\d{1,4}[.,]\d{2})\s*€\s+"
        r"(\d{1,4}[.,]\d{2})\s*€",

        r"\bTotal\s+"
        r"(\d{1,4}[.,]\d{2})\s+"
        r"(\d{1,4}[.,]\d{2})",

    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(2)

    return None


# ============================================================
# PACIENTES
# ============================================================

def extract_patients_from_rows(rows):

    patients = []

    for row in rows:

        patient = row.get(
            "paciente",
            ""
        ).strip()

        if (
            patient
            and patient not in patients
        ):
            patients.append(patient)

    return patients


# ============================================================
# TAULA
# ============================================================

def extract_table_rows(text):
    """
    Parser específic per al format estàndard de factura AniCura.

    Estructura EXACTA de les files:

        DATA PACIENTE ARTÍCULO PREU € CANTITAT IVA % IVA € IMPORT €

    Exemple:

        08/09/2026 LLUC PERFIL HIPOTIROIDISMO
        80,99 € 1 21 % 17,01 € 98,00 €

    També suportem que TOTA la fila vingui en una sola línia.
    """

    rows = []

    lines = text.splitlines()

    i = 0

    while i < len(lines):

        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # ----------------------------------------------------
        # Una fila comença SEMPRE per data.
        # ----------------------------------------------------

        date_match = re.match(
            r"^(\d{2}/\d{2}/\d{4})\s+(.+)$",
            line
        )

        if not date_match:
            i += 1
            continue

        fecha = date_match.group(1)
        current = date_match.group(2).strip()

        # ----------------------------------------------------
        # Intentem que la fila sigui completa.
        #
        # Si l'escàner ha partit l'article en una segona línia,
        # anem incorporant línies fins trobar:
        #
        # PREU € CANTITAT IVA% IVA€ IMPORTE €
        # ----------------------------------------------------

        combined = current

        max_extra_lines = 4

        for extra in range(max_extra_lines + 1):

            match = re.search(
                r"""
                ^(.+?)
                \s+
                (\d{1,5}[.,]\d{2})\s*€
                \s+
                (\d{1,3})
                \s+
                (\d{1,2})\s*%
                \s+
                (\d{1,5}[.,]\d{2})\s*€
                \s+
                (\d{1,5}[.,]\d{2})\s*€
                $
                """,
                combined,
                re.VERBOSE
            )

            if match:
                break

            if extra >= max_extra_lines:
                match = None
                break

            next_index = i + extra + 1

            if next_index >= len(lines):
                break

            next_line = lines[next_index].strip()

            # Si la següent línia comença amb una nova data,
            # no pertany a aquesta fila.
            if re.match(
                r"^\d{2}/\d{2}/\d{4}\b",
                next_line
            ):
                break

            # Evitar incorporar capçaleres/resums.
            if re.match(
                r"^(Total|Pagos|Pendiente|Tarjeta|Fecha|Paciente|"
                r"Artículos|Precio|Pagado)\b",
                next_line,
                re.IGNORECASE
            ):
                break

            combined += " " + next_line

        # ----------------------------------------------------
        # Si no trobem les 6 columnes finals, no és una fila.
        # ----------------------------------------------------

        if not match:
            i += 1
            continue

        beginning = match.group(1).strip()

        precio = match.group(2)
        cantidad = match.group(3)
        iva = match.group(4)
        iva_valor = match.group(5)
        importe = match.group(6)

        # ----------------------------------------------------
        # DATA ja està separada.
        #
        # Ara beginning:
        #
        # LLUC BIOQUIMICA LAB INT RAL METROLAB CALCIO
        #
        # Primer token = pacient
        # Resta = article
        # ----------------------------------------------------

        parts = beginning.split()

        if len(parts) < 2:
            i += 1
            continue

        paciente = parts[0].strip()

        articulo = " ".join(
            parts[1:]
        ).strip()

        if not paciente or not articulo:
            i += 1
            continue

        # ----------------------------------------------------
        # Evitem agafar línies del resum de factura.
        # ----------------------------------------------------

        if paciente.upper() in {
            "TOTAL",
            "TODO",
            "PAGOS",
            "PENDIENTE",
            "TARJETA",
        }:
            i += 1
            continue

        row = {
            "fecha": fecha,
            "paciente": paciente,
            "articulo": articulo,
            "precio": f"{precio} €",
            "cantidad": cantidad,
            "iva_igic": f"{iva} %",
            "iva_valor": f"{iva_valor} €",
            "importe": f"{importe} €"
        }

        rows.append(row)

        # ----------------------------------------------------
        # Saltar les línies que hem incorporat.
        # ----------------------------------------------------

        consumed = 1

        if match:

            # Comptem quantes línies formen la fila.
            while (
                consumed <= max_extra_lines
                and i + consumed < len(lines)
            ):

                candidate = lines[
                    i + consumed
                ].strip()

                if not candidate:
                    consumed += 1
                    continue

                test_combined = " ".join(
                    lines[i:i + consumed + 1]
                ).strip()

                date_removed = re.sub(
                    r"^\d{2}/\d{2}/\d{4}\s+",
                    "",
                    test_combined
                )

                if re.search(
                    r"\d{1,5}[.,]\d{2}\s*€\s+"
                    r"\d{1,3}\s+"
                    r"\d{1,2}\s*%\s+"
                    r"\d{1,5}[.,]\d{2}\s*€\s+"
                    r"\d{1,5}[.,]\d{2}\s*€$",
                    date_removed
                ):
                    consumed += 1
                else:
                    break

        i += consumed

    return rows


# ============================================================
# FUNCIÓ PRINCIPAL
# ============================================================

def extract_table_data(pdf_path):

    print()
    print("========================================")
    print("        INICIANT PROCESSAMENT")
    print("========================================")
    print()

    full_text = extract_all_text(
        pdf_path
    )

    full_text = normalize_text(
        full_text
    )

    print()
    print("========== TEXT EXTRET ==========")
    print()
    print(full_text)
    print()
    print("========== FI TEXT ===============")
    print()

    invoice_number = extract_invoice_number(
        full_text
    )

    invoice_date = extract_invoice_date(
        full_text
    )

    total = extract_total(
        full_text
    )

    table_rows = extract_table_rows(
        full_text
    )

    patients = extract_patients_from_rows(
        table_rows
    )

    data = {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "total": total,
        "patients": patients,
        "table_rows": table_rows
    }

    return data


# ============================================================
# ERROR PDF MASSA GRAN
# ============================================================

@app.errorhandler(413)
def request_entity_too_large(error):

    return jsonify({
        "success": False,
        "error":
            "El PDF és massa gran. "
            "La mida màxima és de 20 MB."
    }), 413


# ============================================================
# UPLOAD PDF
# ============================================================

@app.route(
    "/upload",
    methods=["POST"]
)
def upload_file():

    filepath = None

    try:

        print()
        print("========================================")
        print("              NOU PDF")
        print("========================================")
        print()

        # ----------------------------------------------------
        # Comprovar fitxer
        # ----------------------------------------------------

        if "file" not in request.files:

            return jsonify({
                "success": False,
                "error":
                    "No s'ha proporcionat "
                    "cap fitxer."
            }), 400

        file = request.files["file"]

        if file.filename == "":

            return jsonify({
                "success": False,
                "error":
                    "No s'ha seleccionat "
                    "cap fitxer."
            }), 400

        # ----------------------------------------------------
        # Comprovar extensió
        # ----------------------------------------------------

        if not file.filename.lower().endswith(".pdf"):

            return jsonify({
                "success": False,
                "error":
                    "Només s'accepten "
                    "fitxers PDF."
            }), 400

        # ----------------------------------------------------
        # Nom segur i únic
        # ----------------------------------------------------

        original_filename = Path(
            file.filename
        ).name

        unique_filename = (
            uuid.uuid4().hex
            + "_"
            + original_filename
        )

        filepath = (
            UPLOAD_FOLDER
            / unique_filename
        )

        # ----------------------------------------------------
        # Guardar PDF
        # ----------------------------------------------------

        file.save(filepath)

        file_size = filepath.stat().st_size

        print(
            f"PDF rebut: {original_filename}"
        )

        print(
            f"Mida: "
            f"{file_size / (1024 * 1024):.2f} MB"
        )

        # ----------------------------------------------------
        # PROCESSAMENT
        # ----------------------------------------------------

        extracted_data = extract_table_data(
            filepath
        )

        print()
        print("PROCESSAMENT FINALITZAT")
        print()

        return jsonify({
            "success": True,
            "data": extracted_data,
            "filename": original_filename
        }), 200

    except Exception as error:

        print()
        print("========================================")
        print("              ERROR")
        print("========================================")
        print(str(error))
        print()

        return jsonify({
            "success": False,
            "error":
                (
                    "S'ha produït un error "
                    "processant el fitxer: "
                    f"{str(error)}"
                )
        }), 500

    finally:

        if (
            filepath is not None
            and filepath.exists()
        ):

            try:
                filepath.unlink()

                print(
                    "PDF temporal eliminat."
                )

            except Exception as cleanup_error:

                print(
                    "No s'ha pogut eliminar "
                    "el PDF temporal: "
                    f"{cleanup_error}"
                )

        gc.collect()


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "ok",
        "ocr": "enabled"
    }), 200


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print()
    print("========================================")
    print("     EXTRACTOR DE FACTURES OCR")
    print("========================================")
    print()

    print("Web:")
    print("http://localhost:5000/")

    print()
    print("Health:")
    print("http://localhost:5000/health")

    print()
    print("Configuració:")
    print("  OCR: 3x")
    print("  MAX PDF: 20 MB")
    print(f"  MAX PÀGINES: {MAX_PAGES}")
    print("  OCR només quan cal")
    print("  1 pacient per full")
    print("  Format AniCura fix")

    print()
    print("========================================")
    print()

    app.run(
        debug=True,
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        )
    )
