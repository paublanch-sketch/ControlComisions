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

# La data d'una línia, tolerant amb l'OCR de les factures escanejades:
# "8/09/2026", "08-09-2026", "08/09/ 2026", "O8/O9/2O26", o la data
# enganxada al pacient ("08/09/2026SINDY").
LOOSE_DATE_RE = re.compile(
    r"(?<![\dA-Za-z])([0-3OoIl]?[\dOoIl])\s*[/\-.]\s*([01OoIl]?[\dOoIl])\s*[/\-.]\s*(2[0OoIl][\dOoIl]{2}|[\dOoIl]{2})(?![\dOoIl])"
)


def find_date(text):
    """
    Busca una data dd/mm/aaaa en un tros de text i la torna ben
    escrita, o "" si no n'hi ha cap de vàlida.
    """

    for match in LOOSE_DATE_RE.finditer(text or ""):

        parts = [
            re.sub(r"[OoIl]", lambda m: "0" if m.group(0) in "Oo" else "1", p)
            for p in match.groups()
        ]

        day, month, year = (int(p) for p in parts)

        if year < 100:
            year += 2000

        if 1 <= day <= 31 and 1 <= month <= 12 and 2000 <= year <= 2100:
            return f"{day:02d}/{month:02d}/{year}"

    return ""


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

        # La capçalera ocupa dues línies: sota "Precio" hi ha
        # "excl.IVA/IGIC". Aquesta segona línia forma part de la
        # capçalera i cal tenir-la en compte per saber fins on
        # arriba la columna.
        header_words = list(line)
        skipped = 0

        if index + 1 < len(lines):

            following = lines[index + 1]

            following_text = line_text(following)

            is_continuation = (
                len(following) <= 3
                and not MONEY_RE.search(following_text)
                and not DATE_RE.search(following_text)
                and "excl" in normalize_header_word(following_text)
            )

            if is_continuation:
                header_words += list(following)
                skipped = 1

        # Assignem cada paraula de la capçalera a un camp.
        anchors = {}
        unmatched = []

        for word in header_words:

            normal = normalize_header_word(word["text"])

            matched = None

            for field, prefixes in COLUMN_ANCHORS:

                if field in anchors:
                    continue

                if any(normal.startswith(p) for p in prefixes):
                    matched = field
                    break

            if matched:
                anchors[matched] = {
                    "x0": word["x0"],
                    "x1": word["x1"]
                }
            else:
                unmatched.append(word)

        if "patient" not in anchors or "amount" not in anchors:
            continue

        # Les paraules de capçalera que no són cap camp (per
        # exemple "excl.IVA/IGIC") eixamplen la columna a la
        # qual pertanyen.
        for word in unmatched:

            center = (word["x0"] + word["x1"]) / 2

            closest = min(
                anchors.items(),
                key=lambda kv: abs(
                    (kv[1]["x0"] + kv[1]["x1"]) / 2 - center
                )
            )[0]

            anchors[closest]["x0"] = min(anchors[closest]["x0"], word["x0"])
            anchors[closest]["x1"] = max(anchors[closest]["x1"], word["x1"])

        ordered = sorted(
            anchors.items(),
            key=lambda kv: kv[1]["x0"]
        )

        # El límit entre dues columnes és el punt mig entre el
        # final d'una capçalera i el començament de la següent.
        #
        # Fer servir només la x inicial no serveix: els títols
        # numèrics ("Precio", "Importe") van alineats a la dreta
        # i les xifres cauen a la seva esquerra.
        columns = []

        for position, (field, extent) in enumerate(ordered):

            if position == 0:
                x0 = float("-inf")
            else:
                previous = ordered[position - 1][1]
                x0 = (previous["x1"] + extent["x0"]) / 2

            if position + 1 < len(ordered):
                following_extent = ordered[position + 1][1]
                x1 = (extent["x1"] + following_extent["x0"]) / 2
            else:
                x1 = float("inf")

            columns.append({
                "field": field,
                "x0": x0,
                "x1": x1
            })

        return index + skipped, columns

    return None, None


def refine_article_boundary(data_lines, columns):
    """
    Ajusta on acaba la columna de la descripció.

    Els títols de la capçalera són molt més curts que el text
    que hi ha a sota: "Artículos" ocupa quatre dits i la
    descripció s'allarga fins a tocar el preu. Si ens quedem
    amb la mesura del títol, les últimes paraules de l'article
    ("GATO", "HASTA 24", "LAB INT RAL") cauen dins la columna
    del preu i es perden.

    Per això mirem les files de dades: la columna del preu
    comença on comença l'import més a l'esquerra de tota la
    taula.
    """

    fields = [column["field"] for column in columns]

    if "article" not in fields:
        return columns

    article = columns[fields.index("article")]

    following = None

    for column in columns:
        if column["x0"] >= article["x1"]:
            if following is None or column["x0"] < following["x0"]:
                following = column

    if following is None:
        return columns

    leftmost = None

    for line in data_lines:

        text = line_text(line)

        if text.lower().startswith(STOP_WORDS):
            break

        money_words = [
            word
            for word in line
            if MONEY_RE.fullmatch(word["text"].strip())
        ]

        # Només ens fiem de les files completes, que porten com
        # a mínim el preu, l'IVA i l'import.
        if len(money_words) < 2:
            continue

        first = min(money_words, key=lambda w: w["x0"])

        if first["x0"] <= article["x0"]:
            continue

        if leftmost is None or first["x0"] < leftmost:
            leftmost = first["x0"]

    if leftmost is None or leftmost <= article["x0"]:
        return columns

    boundary = leftmost - 2

    article["x1"] = boundary
    following["x0"] = boundary

    return columns


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

    # Xarxa de seguretat: si l'OCR ha desplaçat un import cap a
    # la columna de la descripció, el traiem. Cap article conté
    # una xifra amb coma i dos decimals.
    text = MONEY_RE.sub(" ", text)

    text = re.sub(r"\s*€\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" -:|.,")

    return text


def money_to_float(text):

    if not text:
        return None

    try:
        return float(
            text.replace(".", "").replace(",", ".")
        )
    except ValueError:
        return None


def row_money_values(line):
    """
    Tots els imports de la fila, en ordre d'esquerra a dreta.
    """

    values = []

    for word in sorted(line, key=lambda w: w["x0"]):

        match = MONEY_RE.search(word["text"])

        if not match:
            continue

        number = money_to_float(match.group(1))

        if number is not None:
            values.append((match.group(1), number))

    return values


VAT_RATES = (0.21, 0.10, 0.04, 0.07, 0.03, 0.0)


def parse_quantity(text):
    """
    "2", "1 pc", "1,00" -> 2.0, 1.0, 1.0. Si no hi ha número, 1.
    """

    match = re.search(r"\d+(?:[.,]\d+)?", text or "")

    if not match:
        return 1.0

    value = float(match.group(0).replace(",", "."))

    return value if value > 0 else 1.0


def parse_vat_rate(text):
    """
    "21%", "21 %", "10%" -> 0.21, 0.10. Si no n'hi ha, None.
    """

    match = re.search(r"(\d{1,2})(?:[.,]\d+)?\s*%", text or "")

    return int(match.group(1)) / 100 if match else None


def price_matches(price, amount, quantity, vat=None, vat_rate=None,
                  quantity_known=True):
    """
    Comprova que el preu sense IVA quadra amb la resta de la línia:

      - preu x quantitat x (1 + IVA%) = import, o
      - preu x quantitat x IVA% = IVA de la línia, o
      - preu x quantitat + IVA de la línia = import.

    Amb una de les tres n'hi ha prou: si l'OCR llegeix malament
    l'import, l'IVA encara confirma el preu (i al revés).
    """

    if price is None:
        return False

    rates = (vat_rate,) if vat_rate is not None else VAT_RATES

    for quantity_option in {quantity, 1.0}:

        base = price * quantity_option

        if amount is not None:
            for rate in VAT_RATES:
                if abs(base * (1 + rate) - amount) <= 0.03 + amount * 0.0005:
                    return True

        if vat is not None:

            for rate in rates:
                if rate and abs(base * rate - vat) <= 0.02 + vat * 0.001:
                    return True

            if amount is not None and abs(base + vat - amount) <= 0.03:
                return True

    # La quantitat no es pot llegir ("Z" per 2): l'import és el preu
    # per un nombre sencer d'unitats, amb un dels IVA. Si la quantitat
    # es llegeix, mana ella.
    if not quantity_known and amount is not None and price > 0:
        for rate in rates:
            units = amount / (price * (1 + rate))
            if 1.5 <= units <= 50 and abs(units - round(units)) <= 0.002 * units:
                return True

    return False


def resolve_price_and_amount(monies, column_price, column_amount,
                             column_quantity="", column_vat="",
                             column_vat_pct=""):
    """
    Decideix quin import és el PREU SENSE IVA i quin és l'import
    final de la línia.

    El preu és la columna "Precio excl.IVA/IGIC": la primera xifra
    de la fila, després de la data i l'article. L'import (la última
    xifra, amb IVA) NO és mai el preu: abans, quan el preu no es
    trobava, s'agafava l'import i la comissió del 5% sortia inflada.

    Ordre:
      1. La columna del preu, si no és el mateix número que l'import.
      2. Si l'OCR ha mogut el preu de columna: la primera xifra de la
         fila que no sigui l'import.
      3. Res: el preu es queda buit perquè s'escrigui a mà.

    Retorna (preu, import, falta_preu). Un preu que no quadra amb
    l'import ni amb l'IVA no s'avisa: l'usuari va comprovar que
    aquests avisos eren falsos (4/11/2026). price_matches només
    serveix per triar entre la columna i la primera xifra.
    """

    amount_text = column_amount or (monies[-1][0] if monies else "")
    amount = money_to_float(amount_text)

    # Totes les xifres de la fila menys l'import final (i qualsevol
    # altra còpia del mateix número, que seria l'import repetit).
    others = [
        (text, value)
        for text, value in monies
        if text != amount_text and value != amount
    ]

    quantity = parse_quantity(column_quantity)
    quantity_known = bool(re.search(r"\d", column_quantity or ""))
    vat = money_to_float(column_vat)
    vat_rate = parse_vat_rate(column_vat_pct)

    def matches(value):
        return price_matches(value, amount, quantity, vat, vat_rate,
                             quantity_known)

    price_text = ""

    if column_price and column_price != amount_text:
        price_text = column_price

    elif others:
        price_text = others[0][0]

    if not price_text:
        return "", amount_text, True

    # Si la columna no quadra però la primera xifra de la fila sí, és
    # que l'OCR ha mogut el preu de lloc.
    if (others and others[0][0] != price_text
            and not matches(money_to_float(price_text))
            and matches(others[0][1])):
        return others[0][0], amount_text, False

    return price_text, amount_text, False


def parse_money(text):
    """
    Retorna l'import europeu trobat al text, en format 1234,56.
    """

    if not text:
        return ""

    match = MONEY_RE.search(text.replace("\xa0", " "))

    return match.group(1) if match else ""


def read_table_rows(lines, columns, patient_names):
    """
    Les files d'una pàgina de la taula, de dalt a baix fins al peu
    ("Total", "Pagos", "ANICURA SPAIN"...).

    Un article pot ocupar dues línies. Aquesta segona línia no porta
    ni pacient ni imports, i pot quedar per SOBRE o per SOTA de la
    fila. S'assigna a la fila més propera verticalment, que és
    sempre la seva.
    """

    rows = []
    fragments = []

    # Dates que l'OCR ha deixat en una línia a part (una mica més
    # amunt o avall que la resta de la fila).
    date_fragments = []

    heights = sorted(
        w["y1"] - w["y0"]
        for line in lines
        for w in line
        if w["y1"] > w["y0"]
    )

    line_height = heights[len(heights) // 2] if heights else 10.0

    for line in lines:

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
                # A la columna de la data; si l'OCR l'ha partit o
                # l'ha enganxat al pacient, entre totes dues.
                "date": (
                    find_date(cells.get("date", ""))
                    or find_date(cells.get("date", "") + " " + patient_cell)
                ),
                "patient": patient_cell,
                "article": clean_article(cells.get("article", "")),
                "price": parse_money(cells.get("price", "")),
                "quantity": cells.get("quantity", ""),
                "vat": parse_money(cells.get("vat", "")),
                "vat_pct": cells.get("vat_pct", ""),
                "raw": text,
                "amount": parse_money(amount_cell),
                "monies": row_money_values(line),
                "prefix": [],
                "suffix": []
            })

        else:

            if find_date(cells.get("date", "")):
                date_fragments.append({
                    "center": line_center(line),
                    "date": find_date(cells["date"])
                })

            if cells.get("article", "").strip():
                fragments.append({
                    "center": line_center(line),
                    "text": clean_article(cells["article"])
                })

    if not rows:
        return []

    for fragment in fragments:

        best = min(
            rows,
            key=lambda row: abs(row["center"] - fragment["center"])
        )

        if fragment["center"] < best["center"]:
            best["prefix"].append(fragment["text"])
        else:
            best["suffix"].append(fragment["text"])

    # Cada data solta va a la fila sense data que tingui just al
    # costat. Més lluny no és d'aquella fila (pot ser la data de la
    # capçalera d'una pàgina).
    for fragment in date_fragments:

        free = [
            row for row in rows
            if not row["date"]
            and abs(row["center"] - fragment["center"]) <= line_height * 2
        ]

        if free:
            nearest = min(free, key=lambda row: abs(row["center"] - fragment["center"]))
            nearest["date"] = fragment["date"]

    return rows


def extract_items_from_words(words, patients=None, invoice_date=None):
    """
    Llegeix la taula de conceptes fent servir les COLUMNES.

    Regles:

    - Cada fila real de la factura porta un pacient i un import.
    - La data pot faltar: llavors s'hereta de la fila anterior.
    - La taula es llegeix pàgina per pàgina. Cada pàgina acaba al
      seu peu, i la següent torna a començar a la seva capçalera
      (o, si no en té, amb les columnes de l'anterior).
    """

    patient_names = [
        p.upper()
        for p in (patients or [])
        if p
    ]

    pages = {}

    for word in words:
        pages.setdefault(word.get("page", 1), []).append(word)

    rows = []
    columns = None

    for page_number in sorted(pages):

        lines = group_lines(pages[page_number])

        if not lines:
            continue

        header_index, page_columns = find_header(lines)

        if page_columns is not None:

            data_lines = lines[header_index + 1:]

            columns = refine_article_boundary(data_lines, page_columns)

        elif columns is not None:

            # Pàgina de continuació sense capçalera: la taula té
            # les mateixes columnes que a la pàgina anterior.
            data_lines = lines

        else:
            continue

        rows.extend(read_table_rows(data_lines, columns, patient_names))

    if not rows:
        return []

    # ------------------------------------------------------------
    # Muntem el resultat final.
    # ------------------------------------------------------------

    items = []

    # Una fila sense data és del mateix dia que la d'abans (la
    # factura només escriu la data a la primera línia de cada dia).
    # Si és la primera, la de la fila següent que en tingui.
    following_date = next((row["date"] for row in rows if row["date"]), "")
    previous_date = ""
    previous_patient = ""

    for row in rows:

        date = row["date"] or previous_date or following_date

        # Cap línia porta una data que es pugui llegir: la de la
        # factura, i es diu a la pantalla.
        date_from_invoice = not date and bool(invoice_date)

        if date_from_invoice:
            date = invoice_date

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

        price, amount, missing_price = resolve_price_and_amount(
            row["monies"],
            row["price"],
            row["amount"],
            row["quantity"],
            row["vat"],
            row["vat_pct"]
        )

        # Si no hi ha preu, la línia surt igualment amb el preu buit
        # perquè s'escrigui a mà. Mai es posa l'import en el seu lloc.
        if not amount and not price:
            continue

        items.append({
            "date": date,
            "date_from_invoice": date_from_invoice,
            "patient": patient,
            "article": article,

            # Preu sense IVA: és la base de la comissió del 5%
            # i el que va a la columna "PREU S/IVA" del full.
            "price": price,

            # Import final de la línia, amb IVA.
            "amount": amount,

            # La línia tal com es llegeix al PDF, per poder buscar-hi
            # el preu a mà quan no s'ha trobat.
            "raw": row["raw"] if missing_price else ""
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

        items = extract_items_from_words(words, patients, invoice_date)

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
