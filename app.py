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

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_PAGES = 5

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

_TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ============================================================
# OCR
# ============================================================

def preprocess_image(image):
    image = image.convert("L")
    image = ImageEnhance.Contrast(image).enhance(2.0)

    threshold = 180

    image = image.point(
        lambda x: 0 if x < threshold else 255,
        "1"
    )

    return image


def perform_ocr(image):
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
# EXTRAER TEXTO DEL PDF
# ============================================================

def extract_all_text(pdf_path):

    document = None
    all_text = []

    try:

        document = fitz.open(pdf_path)

        page_count = len(document)

        print(f"PDF obert: {page_count} pàgines")

        if page_count > MAX_PAGES:
            raise ValueError(
                f"El PDF tiene demasiadas páginas. "
                f"Máximo permitido: {MAX_PAGES}"
            )

        for page_number in range(page_count):

            page = None
            pix = None
            image = None

            try:

                page = document.load_page(page_number)

                # ------------------------------------------------
                # Primero intentamos texto nativo
                # ------------------------------------------------

                native_text = page.get_text("text")

                if native_text and len(native_text.strip()) >= 30:

                    all_text.append(
                        f"\n--- PÀGINA {page_number + 1} ---\n"
                        f"{native_text}"
                    )

                    continue

                # ------------------------------------------------
                # Si no hay texto suficiente -> OCR
                # ------------------------------------------------

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

                try:
                    if image is not None:
                        image.close()
                except Exception:
                    pass

                pix = None
                page = None
                image = None

                gc.collect()

        return "\n".join(all_text)

    finally:

        try:
            if document is not None:
                document.close()
        except Exception:
            pass

        gc.collect()


# ============================================================
# NORMALIZAR TEXTO
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    replacements = {
        " Iva": " IVA",
        " lva": " IVA",
        "Iva": "IVA",
        "lva": "IVA",
        "Igic": "IGIC",
        "IGlC": "IGIC",

        # OCR puede leer Total como Todo
        "Todo": "Total",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Espacios múltiples
    text = re.sub(r"[ \t]+", " ", text)

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
            return match.group(1)

    return None


# ============================================================
# FECHA DE FACTURA
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
# TOTAL REAL DE LA FACTURA
# ============================================================

def extract_total(text):

    """
    Busca el TOTAL REAL de la factura.

    Ejemplo AniCura:

        Total 27,04 € 155,81 €

    27,04 € = IVA
    155,81 € = total final

    Devuelve:

        155,81

    sin el símbolo €.
    """

    if not text:
        return None

    clean = text.replace("\xa0", " ")

    clean = re.sub(
        r"[ \t]+",
        " ",
        clean
    )

    # --------------------------------------------------------
    # Nos quedamos con la parte anterior a "Pagos".
    # El total resumen aparece justo antes.
    # --------------------------------------------------------

    parts = re.split(
        r"\bPagos\b",
        clean,
        maxsplit=1,
        flags=re.IGNORECASE
    )

    before_payments = parts[0]

    # --------------------------------------------------------
    # Buscamos TODOS los "Total"
    # y empezamos por el último.
    # --------------------------------------------------------

    total_matches = list(
        re.finditer(
            r"\bTotal\b",
            before_payments,
            re.IGNORECASE
        )
    )

    money_pattern = re.compile(
        r"(\d{1,6}[.,]\d{2})\s*(?:€|EUR)?",
        re.IGNORECASE
    )

    for match in reversed(total_matches):

        chunk = before_payments[
            match.end():
            match.end() + 250
        ]

        amounts = money_pattern.findall(chunk)

        if len(amounts) >= 2:
            return amounts[-1]

    # --------------------------------------------------------
    # Fallback: línea de pagos
    # --------------------------------------------------------

    payment_match = re.search(
        r"(?:Tarjeta|Pagado|Pago).*?"
        r"(\d{1,6}[.,]\d{2})\s*(?:€|EUR)?",
        clean,
        re.IGNORECASE | re.DOTALL
    )

    if payment_match:
        return payment_match.group(1)

    return None


# ============================================================
# PACIENTES
# ============================================================

def extract_patients(text):

    patients = []

    # Primero intentamos encontrar:
    #
    # Paciente: LLUC

    patterns = [
        r"Paciente\s*:?\s*([A-ZÁÉÍÓÚÀÈÌÒÙÇÑ][A-ZÁÉÍÓÚÀÈÌÒÙÇÑ0-9 .'\-]{1,60})",
        r"Paciente\s+([A-ZÁÉÍÓÚÀÈÌÒÙÇÑ][A-ZÁÉÍÓÚÀÈÌÒÙÇÑ0-9 .'\-]{1,60})",
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE
        )

        for value in matches:

            value = value.strip()

            # Limpiar posibles textos posteriores
            value = re.split(
                r"\b(?:Pagador|Beneficiario|Fecha|Factura|Total)\b",
                value,
                flags=re.IGNORECASE
            )[0].strip()

            if value and value not in patients:
                patients.append(value)

    # --------------------------------------------------------
    # Fallback:
    # buscamos pacientes a partir de las filas.
    # --------------------------------------------------------

    if not patients:

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        date_pattern = re.compile(
            r"^\d{2}/\d{2}/\d{4}\b"
        )

        for line in lines:

            if date_pattern.match(line):

                parts = line.split()

                if len(parts) >= 2:

                    possible_patient = parts[1]

                    if (
                        possible_patient
                        and re.match(
                            r"^[A-ZÁÉÍÓÚÀÈÌÒÙÇÑ0-9]+$",
                            possible_patient,
                            re.IGNORECASE
                        )
                    ):

                        if possible_patient not in patients:
                            patients.append(
                                possible_patient
                            )

    return patients


# ============================================================
# UTILIDADES PARA CONCEPTOS
# ============================================================

DATE_RE = re.compile(
    r"^\s*(\d{2}/\d{2}/\d{4})\b"
)

MONEY_RE = re.compile(
    r"(\d{1,6}[.,]\d{2})\s*(?:€|EUR)?",
    re.IGNORECASE
)


def clean_line(line):

    line = line.replace("\xa0", " ")

    line = re.sub(
        r"[ \t]+",
        " ",
        line
    )

    return line.strip()


def looks_like_section_header(line):

    return bool(
        re.match(
            r"^(?:"
            r"Total|"
            r"Pagos|"
            r"Pendiente de pago|"
            r"Forma de pago|"
            r"Observaciones|"
            r"Gracias"
            r")\b",
            line,
            re.IGNORECASE
        )
    )


# ============================================================
# EXTRAER CONCEPTOS
# ============================================================

def extract_items(text, patients=None):
    """
    Extreu les línies de la factura.

    Regles:
    - Una línia nova comença quan apareix el nom del pacient.
    - Si la línia porta data, s'utilitza aquesta data.
    - Si no porta data, es conserva la data anterior.
    - L'article pot estar dividit en diverses línies.
    - L'últim import de la línia és l'import final.
    """

    if not text:
        return []

    if patients is None:
        patients = extract_patients(text)

    patients = [
        str(p).strip()
        for p in (patients or [])
        if str(p).strip()
    ]

    if not patients:
        return []

    # Pacients més llargs primer
    patients = sorted(
        patients,
        key=len,
        reverse=True
    )

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    items = []

    current = None
    previous_date = ""

    # Dates DD/MM/YYYY
    date_re = re.compile(
        r'\b(\d{2}/\d{2}/\d{4})\b'
    )

    # Imports europeus:
    # 9,09 €
    # 11,00 €
    # 155,81 €
    money_re = re.compile(
        r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€'
    )

    stop_words = (
        "Total",
        "Pagos",
        "Pendiente de pago",
        "Forma de pago",
        "Base imponible",
        "IVA",
        "IGIC"
    )

    def find_patient(line):
        """
        Busca el paciente en la línea.
        Devuelve el nombre encontrado o None.
        """
        upper_line = line.upper()

        for patient in patients:
            if patient.upper() in upper_line:
                return patient

        return None

    def clean_article(article):
        """
        Limpia la parte del artículo.
        """

        article = re.sub(
            r'\s+',
            ' ',
            article
        ).strip()

        # Quitar fecha si quedó dentro
        article = date_re.sub(
            '',
            article
        )

        # Quitar importes
        article = money_re.sub(
            '',
            article
        )

        # Quitar cantidades aisladas tipo "1"
        article = re.sub(
            r'\s+\d+\s*$',
            '',
            article
        )

        # Quitar IVA tipo 21 %
        article = re.sub(
            r'\s+\d{1,2}\s*%\s*',
            ' ',
            article
        )

        # Quitar espacios repetidos
        article = re.sub(
            r'\s+',
            ' ',
            article
        ).strip()

        return article

    def finish_current():
        """
        Finaliza la fila actual.
        """

        nonlocal current

        if not current:
            return

        article = clean_article(
            current.get("article", "")
        )

        amounts = current.get(
            "amounts",
            []
        )

        # L'últim import és l'import final
        final_amount = ""

        if amounts:
            final_amount = amounts[-1]

        if (
            current.get("patient")
            and article
            and final_amount
        ):
            items.append({
                "date": current.get(
                    "date",
                    previous_date
                ) or previous_date,

                "patient": current.get(
                    "patient",
                    ""
                ),

                "article": article,

                "amount": final_amount
            })

        current = None

    for raw_line in lines:

        line = raw_line.strip()

        # Quan arribem a Total/Pagos, acabem
        # les línies de factura.
        if any(
            line.lower().startswith(
                word.lower()
            )
            for word in stop_words
        ):
            finish_current()
            break

        patient = find_patient(line)

        date_match = date_re.search(line)

        line_date = (
            date_match.group(1)
            if date_match
            else ""
        )

        if line_date:
            previous_date = line_date

        # ==================================================
        # NOVA LÍNIA:
        # el pacient marca sempre el començament.
        # ==================================================

        if patient:

            # Acabem la línia anterior
            finish_current()

            current = {
                "date": line_date or previous_date,
                "patient": patient,
                "article": "",
                "amounts": []
            }

            # Treure el pacient de la línia
            article_part = re.sub(
                re.escape(patient),
                '',
                line,
                flags=re.IGNORECASE
            )

            # Treure la data
            article_part = date_re.sub(
                '',
                article_part,
                count=1
            )

            # Buscar imports
            amounts = money_re.findall(
                article_part
            )

            if amounts:
                current["amounts"].extend(
                    amounts
                )

                # L'article és tot el que hi ha
                # abans del primer import
                first_money = money_re.search(
                    article_part
                )

                if first_money:
                    article_part = (
                        article_part[
                            :first_money.start()
                        ]
                    )

            current["article"] = (
                article_part.strip()
            )

            continue

        # ==================================================
        # CONTINUACIÓ DE LA LÍNIA ACTUAL
        # ==================================================

        if current is None:
            continue

        # Si aquesta línia té una data,
        # actualitzem la data.
        if line_date:
            current["date"] = line_date
            previous_date = line_date

        # Imports de la línia
        amounts = money_re.findall(line)

        if amounts:
            current["amounts"].extend(
                amounts
            )

            # Només afegim a l'article
            # el text anterior al primer import.
            first_money = money_re.search(line)

            if first_money:
                continuation = line[
                    :first_money.start()
                ].strip()

                if continuation:
                    current["article"] += (
                        " " + continuation
                    )

        else:
            # És una continuació de l'article
            # sense import.
            current["article"] += (
                " " + line
            )

    # Última línia
    finish_current()

    return items
    """
    IMPORTANTE:

    Las filas NO se detectan por fecha.

    Se detectan por el nombre del paciente.

    Esto permite trabajar con PDFs donde:

        08/09/2026 LLUC CONCEPTO...
        LLUC OTRO CONCEPTO...
        LLUC OTRO CONCEPTO...

    En las filas que no tengan fecha se reutiliza
    la fecha de la fila anterior.

    Solo se devuelve:

        fecha
        paciente
        artículo
        importe

    Se ignora:

        precio sin IVA
        cantidad
        IVA %
        importe IVA
    """

    if not text:
        return []

    if patients is None:
        patients = extract_patients(text)

    # --------------------------------------------------------
    # Limpiar pacientes
    # --------------------------------------------------------

    patients = [
        clean_line(p)
        for p in patients
        if p and clean_line(p)
    ]

    if not patients:
        return []

    # Ordenar pacientes de más largo a más corto.
    # Evita problemas si algún paciente tiene nombres compuestos.
    patients = sorted(
        patients,
        key=len,
        reverse=True
    )

    lines = [
        clean_line(line)
        for line in text.splitlines()
        if clean_line(line)
    ]

    # --------------------------------------------------------
    # Construimos las filas.
    #
    # Una fila empieza cuando aparece un paciente.
    #
    # Ejemplo:
    #
    # 08/09/2026 LLUC CALCIO...
    #
    # o:
    #
    # LLUC FOSFORO...
    # --------------------------------------------------------

    raw_rows = []

    current_row = []

    for line in lines:

        if looks_like_section_header(line):

            if current_row:
                raw_rows.append(
                    " ".join(current_row)
                )
                current_row = []

            # Ya estamos entrando en la zona de totales/pagos.
            break

        # ----------------------------------------------------
        # ¿La línea contiene un paciente?
        # ----------------------------------------------------

        patient_found = None

        for patient in patients:

            pattern = re.compile(
                r"(?<!\S)"
                + re.escape(patient)
                + r"(?!\S)",
                re.IGNORECASE
            )

            if pattern.search(line):

                patient_found = patient
                break

        # ----------------------------------------------------
        # Si encontramos paciente:
        # nueva fila.
        # ----------------------------------------------------

        if patient_found:

            if current_row:

                raw_rows.append(
                    " ".join(current_row)
                )

            current_row = [line]

        else:

            # ------------------------------------------------
            # Línea de continuación de la fila anterior.
            #
            # Esto es importante para artículos que ocupan
            # varias líneas.
            # ------------------------------------------------

            if current_row:
                current_row.append(line)

    # Guardar última fila
    if current_row:
        raw_rows.append(
            " ".join(current_row)
        )

    # --------------------------------------------------------
    # Convertir filas a objetos.
    # --------------------------------------------------------

    items = []

    previous_date = None

    for row in raw_rows:

        row = clean_line(row)

        if not row:
            continue

        # ----------------------------------------------------
        # Fecha
        # ----------------------------------------------------

        date_match = DATE_RE.match(row)

        if date_match:

            current_date = date_match.group(1)

            # Eliminar fecha del principio
            body = row[
                date_match.end():
            ].strip()

            previous_date = current_date

        else:

            # No hay fecha:
            # usamos SIEMPRE la anterior.
            current_date = previous_date

            body = row

        if not body:
            continue

        # ----------------------------------------------------
        # Identificar paciente
        # ----------------------------------------------------

        patient_found = None
        patient_start = None
        patient_end = None

        for patient in patients:

            pattern = re.compile(
                re.escape(patient),
                re.IGNORECASE
            )

            match = pattern.search(body)

            if match:

                patient_found = patient
                patient_start = match.start()
                patient_end = match.end()

                break

        if not patient_found:
            continue

        # ----------------------------------------------------
        # Todo lo que viene después del paciente.
        # ----------------------------------------------------

        after_patient = body[
            patient_end:
        ].strip()

        # ----------------------------------------------------
        # Encontrar importes.
        #
        # El ÚLTIMO importe de la fila es el importe final
        # del concepto.
        # ----------------------------------------------------

        amounts = list(
            MONEY_RE.finditer(after_patient)
        )

        if not amounts:
            continue

        final_amount = amounts[-1].group(1)

        # ----------------------------------------------------
        # El artículo es todo lo que aparece ANTES del primer
        # importe.
        #
        # Por tanto ignoramos automáticamente:
        #
        # precio sin IVA
        # cantidad
        # IVA %
        # importe IVA
        # etc.
        # ----------------------------------------------------

        first_amount_start = amounts[0].start()

        article = after_patient[
            :first_amount_start
        ].strip()

        # ----------------------------------------------------
        # Limpieza adicional del artículo
        # ----------------------------------------------------

        article = re.sub(
            r"\s+",
            " ",
            article
        ).strip()

        # Quitar posibles caracteres sueltos al final
        article = article.strip(" -:|")

        if not article:
            continue

        items.append({
            "date": current_date or "",
            "patient": patient_found,
            "article": article,
            "amount": final_amount
        })

    return items


# ============================================================
# API DE SUBIDA
# ============================================================

@app.route("/upload", methods=["POST"])
def upload():

    uploaded_file = request.files.get("file")

    if not uploaded_file:
        return jsonify({
            "success": False,
            "error": "No se ha recibido ningún archivo."
        }), 400

    if not uploaded_file.filename:
        return jsonify({
            "success": False,
            "error": "El archivo no tiene nombre."
        }), 400

    filename = uploaded_file.filename

    if not filename.lower().endswith(".pdf"):

        return jsonify({
            "success": False,
            "error": "Solo se permiten archivos PDF."
        }), 400

    # --------------------------------------------------------
    # Nombre temporal seguro
    # --------------------------------------------------------

    temp_filename = (
        f"{uuid.uuid4().hex}.pdf"
    )

    pdf_path = UPLOAD_FOLDER / temp_filename

    try:

        uploaded_file.save(pdf_path)

        # ----------------------------------------------------
        # Extraer texto
        # ----------------------------------------------------

        text = extract_all_text(
            pdf_path
        )

        if not text.strip():

            return jsonify({
                "success": False,
                "error": "No se ha podido extraer texto del PDF."
            }), 400

        print("\n================ TEXTO EXTRAÍDO ================\n")
        print(text)
        print("\n=================================================\n")

        # ----------------------------------------------------
        # Normalizar
        # ----------------------------------------------------

        normalized = normalize_text(text)

        # ----------------------------------------------------
        # Datos de factura
        # ----------------------------------------------------

        invoice_number = extract_invoice_number(
            normalized
        )

        invoice_date = extract_invoice_date(
            normalized
        )

        total = extract_total(
            normalized
        )

        patients = extract_patients(
            normalized
        )

        # ----------------------------------------------------
        # Conceptos
        # ----------------------------------------------------

        items = extract_items(
            normalized,
            patients
        )

        print("Número factura:", invoice_number)
        print("Fecha factura:", invoice_date)
        print("Total:", total)
        print("Pacientes:", patients)
        print("Conceptos:", len(items))

        for item in items:
            print(item)

        # ----------------------------------------------------
        # Respuesta
        # ----------------------------------------------------

        return jsonify({
            "success": True,

            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "total": total,

            "patients": patients,

            "items": items,

            # También enviamos el texto por si el frontend
            # lo utiliza para depuración.
            "text": normalized
        })

    except ValueError as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 400

    except Exception as error:

        print("ERROR:", error)

        return jsonify({
            "success": False,
            "error": (
                "Error procesando el PDF: "
                + str(error)
            )
        }), 500

    finally:

        # ----------------------------------------------------
        # Eliminar PDF temporal
        # ----------------------------------------------------

        try:

            if pdf_path.exists():
                pdf_path.unlink()

        except Exception as error:

            print(
                "No se pudo eliminar el archivo temporal:",
                error
            )

        gc.collect()


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "success": True,
        "status": "ok"
    })


# ============================================================
# ARRANCAR SERVIDOR
# ============================================================

if __name__ == "__main__":

    print()
    print("======================================")
    print("📄 Extractor de Factures")
    print("======================================")
    print("Servidor iniciado")
    print("http://127.0.0.1:5000")
    print()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )