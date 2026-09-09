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

# URL del desplegament de Google Apps Script.
# Es configura amb la variable d'entorn APPS_SCRIPT_URL a Render,
# així no cal tocar el codi cada vegada que es torna a desplegar
# l'Apps Script (que canvia la URL /exec).
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/config", methods=["GET"])
def config():
    """
    El frontend demana aquí la URL de l'Apps Script.
    """
    return jsonify({
        "apps_script_url": APPS_SCRIPT_URL
    })


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


def ocr_words(image, scale):
    """
    Fa OCR i retorna les paraules amb les seves coordenades,
    ja reescalades a punts del PDF.

    Cada paraula és:

        {"text": ..., "x0": ..., "x1": ..., "y0": ..., "y1": ...}

    Treballar amb coordenades és el que permet saber a quina
    COLUMNA pertany cada paraula. Sense això no hi ha manera
    fiable de saber si un número és el preu sense IVA o
    l'import final, ni si una fila porta data o no.
    """

    image = preprocess_image(image)

    config = "--oem 3 --psm 6"

    data = None

    for lang in ("spa", "eng"):
        try:
            data = pytesseract.image_to_data(
                image,
                lang=lang,
                config=config,
                output_type=pytesseract.Output.DICT
            )
            break
        except Exception as error:
            print(f"Error OCR {lang}:", error)

    if not data:
        return []

    words = []

    count = len(data.get("text", []))

    for i in range(count):

        text = (data["text"][i] or "").strip()

        if not text:
            continue

        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            confidence = -1.0

        if confidence < 0:
            continue

        x = data["left"][i] / scale
        y = data["top"][i] / scale
        w = data["width"][i] / scale
        h = data["height"][i] / scale

        words.append({
            "text": text,
            "x0": x,
            "x1": x + w,
            "y0": y,
            "y1": y + h
        })

    return words


# ============================================================
# EXTRAER PALABRAS DEL PDF (nativas u OCR)
# ============================================================

def extract_words(pdf_path):
    """
    Retorna (paraules, text_pla).

    Les factures d'AniCura no porten capa de text real: les
    lletres són traços vectorials. Per això a la pràctica
    sempre passem per OCR. Igualment provem primer el text
    natiu, que és instantani, per si algun dia arriba un PDF
    digital de veritat.
    """

    document = None
    all_words = []

    try:

        document = fitz.open(pdf_path)

        page_count = len(document)

        print(f"PDF obert: {page_count} pàgines")

        if page_count > MAX_PAGES:
            raise ValueError(
                f"El PDF tiene demasiadas páginas. "
                f"Máximo permitido: {MAX_PAGES}"
            )

        page_offset = 0.0

        for page_number in range(page_count):

            page = None
            pix = None
            image = None

            try:

                page = document.load_page(page_number)

                page_height = page.rect.height

                native = page.get_text("words")

                native_text_length = sum(
                    len(w[4]) for w in native
                )

                if native_text_length >= 30:

                    page_words = [
                        {
                            "text": w[4],
                            "x0": w[0],
                            "x1": w[2],
                            "y0": w[1],
                            "y1": w[3]
                        }
                        for w in native
                    ]

                else:

                    scale = 3.0

                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(scale, scale),
                        alpha=False
                    )

                    image = Image.open(
                        io.BytesIO(pix.tobytes("png"))
                    )

                    image.load()

                    page_words = ocr_words(image, scale)

                # Apilem les pàgines verticalment perquè les
                # coordenades Y siguin úniques a tot el document.
                for w in page_words:
                    w["y0"] += page_offset
                    w["y1"] += page_offset
                    w["page"] = page_number + 1

                all_words.extend(page_words)

                page_offset += page_height + 50

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

        return all_words, words_to_text(all_words)

    finally:

        try:
            if document is not None:
                document.close()
        except Exception:
            pass

        gc.collect()


def group_lines(words, tolerance_ratio=0.6):
    """
    Agrupa paraules en línies visuals segons la coordenada Y.
    """

    if not words:
        return []

    heights = sorted(
        w["y1"] - w["y0"]
        for w in words
        if w["y1"] > w["y0"]
    )

    median_height = (
        heights[len(heights) // 2]
        if heights
        else 10.0
    )

    tolerance = max(median_height * tolerance_ratio, 2.0)

    ordered = sorted(
        words,
        key=lambda w: ((w["y0"] + w["y1"]) / 2, w["x0"])
    )

    lines = []
    current = []
    current_center = None

    for word in ordered:

        center = (word["y0"] + word["y1"]) / 2

        if (
            current_center is None
            or abs(center - current_center) <= tolerance
        ):

            current.append(word)

            centers = [
                (w["y0"] + w["y1"]) / 2
                for w in current
            ]

            current_center = sum(centers) / len(centers)

        else:

            lines.append(sorted(current, key=lambda w: w["x0"]))

            current = [word]
            current_center = center

    if current:
        lines.append(sorted(current, key=lambda w: w["x0"]))

    return lines


def line_text(line):
    return " ".join(w["text"] for w in line)


def words_to_text(words):
    """
    Text pla reconstruït a partir de les paraules.
    Serveix per a les dades de capçalera (número, data, total)
    i per depurar.
    """

    return "\n".join(
        line_text(line)
        for line in group_lines(words)
    )


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
        r"Factura\s*[#¿$:]*\s*([A-Z]{2,4}\d+-\d+)",
        r"\b(FES\d+-\d+)\b",
        r"Factura\s*[#¿$:]*\s*([A-Z0-9]+-\d+)",
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
        r"Fecha\s*:\s*(\d{2}/\d{2}/\d{4})",
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

MONEY_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})\s*(?:€|EUR)?",
    re.IGNORECASE
)

DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


def extract_total(text):
    """
    Busca el TOTAL REAL de la factura.

    Exemple AniCura:

        Total 119,76 € 690,00 €

    119,76 € = IVA
    690,00 € = total final
    """

    if not text:
        return None

    clean = re.sub(r"[ \t]+", " ", text.replace("\xa0", " "))

    before_payments = re.split(
        r"\bPagos\b",
        clean,
        maxsplit=1,
        flags=re.IGNORECASE
    )[0]

    total_matches = list(
        re.finditer(r"\bTotal\b", before_payments, re.IGNORECASE)
    )

    for match in reversed(total_matches):

        chunk = before_payments[match.end():match.end() + 250]

        amounts = MONEY_RE.findall(chunk)

        if len(amounts) >= 2:
            return amounts[-1]

    payment_match = re.search(
        r"(?:Tarjeta|Pagado|Pago).*?(\d{1,6}[.,]\d{2})\s*(?:€|EUR)?",
        clean,
        re.IGNORECASE | re.DOTALL
    )

    if payment_match:
        return payment_match.group(1)

    return None


# ============================================================
# PACIENTES
# ============================================================

# Paraules que MAI són un pacient. Sense aquesta llista l'OCR
# acabava agafant la capçalera de la taula ("Artículos") com
# si fos el nom de l'animal, i llavors no es detectava cap
# línia de factura.
NOT_A_PATIENT = {
    "ARTICULOS", "ARTÍCULOS", "ARTICULO", "ARTÍCULO",
    "PACIENTE", "PACIENTES", "FECHA", "PRECIO", "CANTIDAD",
    "IMPORTE", "IVA", "IGIC", "TOTAL", "SUBTOTAL", "PAGOS",
    "PAGADOR", "BENEFICIARIO", "FACTURA", "EXCL", "MACHO",
    "HEMBRA", "UNKNOWN", "MIGRATION"
}


def extract_patients(text):
    """
    Els pacients surten sempre a la capçalera, en aquest format:

        Pacientes: CONAN (1527555), Macho, Unknown - Migration,
                   3.7 kg, 10/05/2013 (13 años, 3 meses)

    El patró fiable és NOM seguit d'un identificador entre
    parèntesis. Amb això suportem també diverses mascotes a la
    mateixa factura, encara que sigui poc habitual.
    """

    patients = []

    def add(value):
        value = (value or "").strip(" .,;:-")
        if not value:
            return
        if value.upper() in NOT_A_PATIENT:
            return
        if len(value) < 2 or len(value) > 40:
            return
        if value not in patients:
            patients.append(value)

    # 1) Línia "Pacientes:" -> NOM (123456)
    header_match = re.search(
        r"Pacientes?\s*:(.{0,400})",
        text,
        re.IGNORECASE | re.DOTALL
    )

    if header_match:

        block = header_match.group(1)

        for name in re.findall(
            r"([A-ZÁÉÍÓÚÀÈÌÒÙÇÑ][A-ZÁÉÍÓÚÀÈÌÒÙÇÑ' \-]{1,38}?)\s*\(\d{4,}\)",
            block
        ):
            add(name)

        # 2) Sense identificador: agafem el primer nom en
        #    majúscules just després dels dos punts.
        if not patients:

            first = re.match(
                r"\s*([A-ZÁÉÍÓÚÀÈÌÒÙÇÑ][A-ZÁÉÍÓÚÀÈÌÒÙÇÑ' \-]{1,38})",
                block
            )

            if first:
                add(first.group(1))

    return patients


# ============================================================
# TAULA DE CONCEPTES (per posició de columnes)
# ============================================================

# Ancoratges de la capçalera de la taula.
# La clau és el camp; el valor, els inicis de paraula que el
# identifiquen dins la capçalera.
COLUMN_ANCHORS = [
    ("date",     ("fecha", "data")),
    ("patient",  ("paciente", "pacient", "mascota")),
    ("article",  ("articulo", "artículo", "articulos", "artículos",
                  "concepto", "descripcion", "descripción")),
    ("price",    ("precio", "preu")),
    ("quantity", ("cantidad", "cant", "quantitat", "uds")),
    ("vat_pct",  ("iva/igic%", "iva%", "iva/igic", "iva")),
    ("vat",      ("ivanigic", "iva/igic")),
    ("amount",   ("importe", "import")),
]

STOP_WORDS = (
    "total", "pagos", "pendiente", "forma de pago",
    "base imponible", "subtotal", "anicura spain"
)


def normalize_header_word(word):
    return re.sub(
        r"[^a-z0-9/%]",
        "",
        word.lower()
            .replace("á", "a").replace("é", "e").replace("í", "i")
            .replace("ó", "o").replace("ú", "u")
    )


def find_header(lines):
    """
    Localitza la línia de capçalera de la taula i retorna els
    límits X de cada columna.
    """

    for index, line in enumerate(lines):

        normalized = [
            normalize_header_word(w["text"])
            for w in line
        ]

        joined = " ".join(normalized)

        if "paciente" not in joined and "pacient" not in joined:
            continue

        if "importe" not in joined and "import" not in joined:
            continue

        # Assignem cada paraula de la capçalera a un camp.
        anchors = {}

        for word, normal in zip(line, normalized):

            for field, prefixes in COLUMN_ANCHORS:

                if field in anchors:
                    continue

                if any(normal.startswith(p) for p in prefixes):
                    anchors[field] = word["x0"]
                    break

        if "patient" not in anchors or "amount" not in anchors:
            continue

        # Ordenem per X i convertim en intervals.
        ordered = sorted(anchors.items(), key=lambda kv: kv[1])

        columns = []

        for position, (field, x0) in enumerate(ordered):

            if position + 1 < len(ordered):
                x1 = ordered[position + 1][1]
            else:
                x1 = float("inf")

            columns.append({
                "field": field,
                "x0": x0,
                "x1": x1
            })

        # La primera columna arriba fins al marge esquerre.
        columns[0]["x0"] = float("-inf")

        return index, columns

    return None, None


def split_line_by_columns(line, columns):
    """
    Reparteix les paraules d'una línia entre les columnes.
    """

    cells = {column["field"]: [] for column in columns}

    for word in line:

        center = (word["x0"] + word["x1"]) / 2

        for column in columns:

            if column["x0"] <= center < column["x1"]:
                cells[column["field"]].append(word["text"])
                break

    return {
        field: " ".join(parts).strip()
        for field, parts in cells.items()
    }


def line_center(line):
    centers = [(w["y0"] + w["y1"]) / 2 for w in line]
    return sum(centers) / len(centers)


def clean_article(text):

    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.strip(" -:|.,")

    return text


def parse_money(text):
    """
    Retorna l'import europeu trobat al text, en format 1234,56.
    """

    if not text:
        return ""

    match = MONEY_RE.search(text.replace("\xa0", " "))

    return match.group(1) if match else ""


def extract_items_from_words(words, patients=None):
    """
    Llegeix la taula de conceptes fent servir les COLUMNES.

    Regles:

    - Cada fila real de la factura porta un pacient i un import.
    - La data pot faltar: llavors s'hereta de la fila anterior.
    - Un article pot ocupar dues línies. Aquesta segona línia
      no porta ni pacient ni imports, i pot quedar per SOBRE o
      per SOTA de la fila. S'assigna a la fila més propera
      verticalment, que és sempre la seva.
    """

    lines = group_lines(words)

    if not lines:
        return []

    header_index, columns = find_header(lines)

    if columns is None:
        return []

    patient_names = [
        p.upper()
        for p in (patients or [])
        if p
    ]

    rows = []
    fragments = []

    for line in lines[header_index + 1:]:

        text = line_text(line)

        lowered = text.lower().strip()

        if any(lowered.startswith(word) for word in STOP_WORDS):
            break

        # La segona línia de la capçalera ("excl.IVA/IGIC")
        if "excl" in lowered.replace(".", "") and len(line) <= 2:
            continue

        cells = split_line_by_columns(line, columns)

        patient_cell = cells.get("patient", "").strip()
        amount_cell = cells.get("amount", "").strip()

        has_patient = bool(patient_cell) and (
            not patient_names
            or any(
                name in patient_cell.upper()
                for name in patient_names
            )
        )

        has_amount = bool(parse_money(amount_cell))

        if has_patient or has_amount:

            rows.append({
                "center": line_center(line),
                "date": (
                    DATE_RE.search(cells.get("date", "")).group(1)
                    if DATE_RE.search(cells.get("date", ""))
                    else ""
                ),
                "patient": patient_cell,
                "article": clean_article(cells.get("article", "")),
                "price": parse_money(cells.get("price", "")),
                "amount": parse_money(amount_cell),
                "prefix": [],
                "suffix": []
            })

        elif cells.get("article", "").strip():

            fragments.append({
                "center": line_center(line),
                "text": clean_article(cells["article"])
            })

    if not rows:
        return []

    # ------------------------------------------------------------
    # Assignem cada tros d'article a la fila més propera.
    # ------------------------------------------------------------

    for fragment in fragments:

        best = None
        best_distance = None

        for position, row in enumerate(rows):

            distance = abs(row["center"] - fragment["center"])

            if best_distance is None or distance < best_distance:
                best = position
                best_distance = distance

        if best is None:
            continue

        if fragment["center"] < rows[best]["center"]:
            rows[best]["prefix"].append(fragment["text"])
        else:
            rows[best]["suffix"].append(fragment["text"])

    # ------------------------------------------------------------
    # Muntem el resultat final.
    # ------------------------------------------------------------

    items = []

    previous_date = ""
    previous_patient = ""

    for row in rows:

        date = row["date"] or previous_date

        if row["date"]:
            previous_date = row["date"]

        patient = row["patient"] or previous_patient

        if row["patient"]:
            previous_patient = row["patient"]

        article = clean_article(
            " ".join(
                row["prefix"] + [row["article"]] + row["suffix"]
            )
        )

        if not article:
            continue

        amount = row["amount"]
        price = row["price"] or amount

        if not amount and not price:
            continue

        items.append({
            "date": date,
            "patient": patient,
            "article": article,

            # Preu sense IVA: és la base de la comissió del 5%
            # i el que va a la columna "PREU S/IVA" del full.
            "price": price,

            # Import final de la línia, amb IVA.
            "amount": amount
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

    temp_filename = f"{uuid.uuid4().hex}.pdf"

    pdf_path = UPLOAD_FOLDER / temp_filename

    try:

        uploaded_file.save(pdf_path)

        words, text = extract_words(pdf_path)

        if not text.strip():

            return jsonify({
                "success": False,
                "error": "No se ha podido extraer texto del PDF."
            }), 400

        normalized = normalize_text(text)

        invoice_number = extract_invoice_number(normalized)
        invoice_date = extract_invoice_date(normalized)
        total = extract_total(normalized)
        patients = extract_patients(normalized)

        items = extract_items_from_words(words, patients)

        # Si l'OCR no ha llegit bé la capçalera i no hem pogut
        # detectar el pacient, la taula igualment es llegeix per
        # columnes. En aquest cas prenem els pacients de les
        # pròpies files.
        if not patients:

            for item in items:

                if item["patient"] and item["patient"] not in patients:
                    patients.append(item["patient"])

        print("Número factura:", invoice_number)
        print("Fecha factura:", invoice_date)
        print("Total:", total)
        print("Pacientes:", patients)
        print("Conceptos:", len(items))

        for item in items:
            print(item)

        return jsonify({
            "success": True,

            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "total": total,

            "patients": patients,

            "items": items,

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
            "error": "Error procesando el PDF: " + str(error)
        }), 500

    finally:

        try:
            if pdf_path.exists():
                pdf_path.unlink()

        except Exception as error:
            print("No se pudo eliminar el archivo temporal:", error)

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
