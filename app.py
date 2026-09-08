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
    Extreu el total REAL de la factura AniCura.

    Exemple del PDF:
        Total 27,04 € 155,81 €

    27,04 € = IVA
    155,81 € = TOTAL de la factura

    La funció retorna NOMÉS el número, sense el símbol €, perquè
    el frontend ja afegeix € a la pantalla.
    """

    # Primer busquem una línia que contingui "Total".
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()

        if not re.search(r"\bTotal\b", line, re.IGNORECASE):
            continue

        # Tots els imports monetaris de la mateixa línia.
        amounts = re.findall(
            r"(\d{1,6}[.,]\d{2})\s*€",
            line
        )

        # A AniCura el segon import és el total final.
        if len(amounts) >= 2:
            return amounts[1]

    # Fallback: per si el PDF separa la línia en diversos blocs.
    match = re.search(
        r"\bTotal\b.*?"
        r"(\d{1,6}[.,]\d{2})\s*€.*?"
        r"(\d{1,6}[.,]\d{2})\s*€",
        text,
        re.IGNORECASE | re.DOTALL
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
    Parser robust per al format de factura AniCura.

    No assumeix que una línia visual de la factura sigui una línia
    de text del PDF. Una fila pot venir repartida en diverses línies.

    Format detectat:
        DATA
        PACIENT
        ARTICLE (1 o diverses línies)
        PREU €
        QUANTITAT
        IVA %
        IVA €
        IMPORTE €

    També suporta el cas en què tota la fila està en una sola línia.
    """

    rows = []

    # ------------------------------------------------------------
    # Normalitzar només els salts/espais, però conservar les línies.
    # ------------------------------------------------------------
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if line:
            lines.append(line)

    # ------------------------------------------------------------
    # Regex estrictes per a les columnes numèriques.
    # ------------------------------------------------------------
    date_re = re.compile(r"^\d{2}/\d{2}/\d{4}$")
    money_re = re.compile(r"^\d{1,6}[.,]\d{2}\s*€?$")
    quantity_re = re.compile(r"^\d{1,3}$")
    iva_re = re.compile(r"^\d{1,2}\s*%$")

    # ------------------------------------------------------------
    # Localitzar el començament de la taula.
    #
    # És important perquè la factura també té dates en altres
    # zones, por ejemplo en Pagos.
    # ------------------------------------------------------------
    table_start = None

    for idx, line in enumerate(lines):
        if line.lower() == "fecha":
            before = " ".join(lines[:idx]).lower()
            if "pacientes:" in before:
                table_start = idx + 1
                break

    if table_start is None:
        # Fallback: buscar una línea que contenga el encabezado.
        for idx, line in enumerate(lines):
            low = line.lower()
            if "fecha" in low and "paciente" in low and "art" in low:
                table_start = idx + 1
                break

    if table_start is None:
        return rows

    i = table_start

    # ------------------------------------------------------------
    # Recorrer bloques de filas.
    # ------------------------------------------------------------
    while i < len(lines):

        if not date_re.fullmatch(lines[i]):
            i += 1
            continue

        fecha = lines[i]

        # La siguiente línea debe ser el paciente.
        if i + 1 >= len(lines):
            break

        paciente = lines[i + 1].strip()

        # Si parece una sección de resumen, no es una fila.
        if paciente.lower() in {
            "total",
            "pagos",
            "pendiente",
            "pendiente de pago",
            "tarjeta",
            "fecha",
            "paciente",
            "artículos",
            "precio",
            "cantidad",
            "importe",
        }:
            i += 1
            continue

        # --------------------------------------------------------
        # Buscar el bloque:
        #
        # precio
        # cantidad
        # iva
        # iva €
        # importe €
        #
        # Todo lo anterior al precio es el artículo.
        # --------------------------------------------------------
        article_parts = []
        j = i + 2
        found = False

        while j < len(lines):

            current = lines[j]

            # Nueva fecha => la fila anterior no estaba completa.
            if date_re.fullmatch(current):
                break

            # Fin de tabla.
            if current.lower() in {
                "total",
                "pagos",
                "pendiente",
                "pendiente de pago",
            }:
                break

            # ----------------------------------------------------
            # Caso 1: columnas finales cada una en su propia línea.
            # ----------------------------------------------------
            if (
                j + 4 < len(lines)
                and money_re.fullmatch(lines[j])
                and quantity_re.fullmatch(lines[j + 1])
                and iva_re.fullmatch(lines[j + 2])
                and money_re.fullmatch(lines[j + 3])
                and money_re.fullmatch(lines[j + 4])
            ):
                precio = lines[j]
                cantidad = lines[j + 1]
                iva = lines[j + 2]
                iva_valor = lines[j + 3]
                importe = lines[j + 4]

                articulo = " ".join(article_parts).strip()

                if articulo:
                    rows.append({
                        "fecha": fecha,
                        "paciente": paciente,
                        "articulo": articulo,
                        "precio": precio if "€" in precio else precio + " €",
                        "cantidad": cantidad,
                        "iva_igic": iva if "%" in iva else iva + " %",
                        "iva_valor": iva_valor if "€" in iva_valor else iva_valor + " €",
                        "importe": importe if "€" in importe else importe + " €",
                    })

                i = j + 5
                found = True
                break

            # ----------------------------------------------------
            # Caso 2: toda la parte numérica está en la misma línea.
            #
            # Ejemplo:
            # PERFIL HIPOTIROIDISMO 80,99 € 1 21 % 17,01 € 98,00 €
            # ----------------------------------------------------
            one_line = re.search(
                r"^(.*?)\s+"
                r"(\d{1,6}[.,]\d{2})\s*€\s+"
                r"(\d{1,3})\s+"
                r"(\d{1,2})\s*%\s+"
                r"(\d{1,6}[.,]\d{2})\s*€\s+"
                r"(\d{1,6}[.,]\d{2})\s*€$",
                current
            )

            if one_line:
                article_parts.append(one_line.group(1).strip())
                articulo = " ".join(article_parts).strip()

                rows.append({
                    "fecha": fecha,
                    "paciente": paciente,
                    "articulo": articulo,
                    "precio": one_line.group(2) + " €",
                    "cantidad": one_line.group(3),
                    "iva_igic": one_line.group(4) + " %",
                    "iva_valor": one_line.group(5) + " €",
                    "importe": one_line.group(6) + " €",
                })

                i = j + 1
                found = True
                break

            # ----------------------------------------------------
            # Todavía estamos dentro del nombre del artículo.
            # ----------------------------------------------------
            article_parts.append(current)
            j += 1

        if not found:
            i += 1

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
