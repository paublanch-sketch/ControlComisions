from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import fitz  # PyMuPDF
import pytesseract

from PIL import Image, ImageEnhance, ImageFilter

import re
from pathlib import Path
import io
import gc
import uuid


# ============================================================
# CONFIGURACIÓ FLASK
# ============================================================

app = Flask(__name__)
CORS(app)

# Carpeta per als PDFs pujats
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)

# Límit màxim del PDF
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

# Màxim de pàgines per PDF
#
# Les factures d'AniCura sempre són d'1 pàgina amb aquest format
# estàndard. Ho limitem a 5 (marge per si algun dia n'hi ha amb
# més d'un pacient/pàgina) per protegir la RAM del pla gratuït
# de Render (512 MB): un PDF de 30 pàgines a 3x podria fer petar
# la memòria si algú el puja per error.
MAX_PAGES = 5


# ============================================================
# CONFIGURACIÓ TESSERACT
# ============================================================

# Si Tesseract no està al PATH de Windows,
# descomenta aquesta línia i posa la ruta correcta.
#
# IMPORTANT: cal haver instal·lat Tesseract-OCR com a PROGRAMA
# a Windows (no només "pip install pytesseract"). Descarrega'l
# de: https://github.com/UB-Mannheim/tesseract/wiki
#
# pytesseract.pytesseract.tesseract_cmd = (
#     r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# )

import os

_TESSERACT_CMD = os.environ.get("TESSERACT_CMD")

if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD


# ============================================================
# CONFIGURACIÓ UPLOAD
# ============================================================

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE


# ============================================================
# PÀGINA PRINCIPAL
# ============================================================

@app.route("/")
def index():
    """
    Carrega el fitxer index.html.
    """

    return send_from_directory(
        ".",
        "index.html"
    )


# ============================================================
# OCR
# ============================================================

def preprocess_image(image):
    """
    Millora la imatge abans de passar-la per OCR.

    IMPORTANT:
    Aquestes factures són una IMATGE dins del PDF (no tenen
    text natiu), per això calen dues coses per llegir-les bé:

    1. Renderitzar a prou resolució (veure MATRIU OCR més avall).
    2. Binaritzar (blanc/negre pur) en lloc de només augmentar
       contrast, que és el que feia que l'OCR anterior confongués
       lletres i números.
    """

    # Escala de grisos
    image = image.convert("L")

    # Augmentar contrast abans de binaritzar
    image = ImageEnhance.Contrast(
        image
    ).enhance(2.0)

    # Binarització (blanc/negre pur).
    # Llindar 180: per sota és negre, per sobre és blanc.
    threshold = 180

    image = image.point(
        lambda x: 0 if x < threshold else 255,
        "1"
    )

    return image


def perform_ocr(image):
    """
    Executa Tesseract OCR.
    """

    image = preprocess_image(
        image
    )

    # PSM 6:
    # Assumeix un bloc de text uniforme.
    config = "--oem 3 --psm 6"

    # --------------------------------------------------------
    # Primer intent: català + espanyol
    # --------------------------------------------------------

    try:

        text = pytesseract.image_to_string(
            image,
            lang="spa+cat",
            config=config
        )

        return text

    except Exception as error:

        print(
            "No s'ha pogut utilitzar spa+cat:",
            error
        )

    # --------------------------------------------------------
    # Segon intent: espanyol
    # --------------------------------------------------------

    try:

        text = pytesseract.image_to_string(
            image,
            lang="spa",
            config=config
        )

        return text

    except Exception as error:

        print(
            "No s'ha pogut utilitzar spa:",
            error
        )

    # --------------------------------------------------------
    # Últim intent: anglès
    # --------------------------------------------------------

    try:

        text = pytesseract.image_to_string(
            image,
            lang="eng",
            config=config
        )

        return text

    except Exception as error:

        print(
            "No s'ha pogut utilitzar eng:",
            error
        )

        return ""


# ============================================================
# OCR DEL PDF
# ============================================================

def extract_all_text(pdf_path):
    """
    Processa el PDF pàgina per pàgina.

    IMPORTANT:

    1. Intentem primer extreure text natiu del PDF.
    2. Si la pàgina ja té text, NO fem OCR.
    3. Si la pàgina és escanejada, fem OCR a 1.5x.
    4. Mai guardem totes les imatges en RAM.
    """

    document = None

    all_text = []

    try:

        # ----------------------------------------------------
        # Obrir PDF
        # ----------------------------------------------------

        document = fitz.open(
            pdf_path
        )

        page_count = len(
            document
        )

        print()
        print(
            f"PDF obert: {page_count} pàgines"
        )

        # ----------------------------------------------------
        # Comprovar número de pàgines
        # ----------------------------------------------------

        if page_count > MAX_PAGES:

            raise ValueError(
                f"El PDF té {page_count} pàgines. "
                f"El màxim permès és {MAX_PAGES}."
            )

        # ----------------------------------------------------
        # Processar una pàgina cada vegada
        # ----------------------------------------------------

        for page_number in range(
            page_count
        ):

            print()
            print(
                f"Processant pàgina "
                f"{page_number + 1}/{page_count}..."
            )

            page = None
            pix = None
            image = None

            try:

                # ------------------------------------------------
                # Carregar pàgina
                # ------------------------------------------------

                page = document.load_page(
                    page_number
                )

                # =================================================
                # PRIMER: INTENTAR TEXT NATIU
                # =================================================

                native_text = page.get_text(
                    "text"
                )

                # Si el PDF ja té text suficient,
                # no cal fer OCR.
                if native_text and len(
                    native_text.strip()
                ) >= 30:

                    print(
                        f"Pàgina {page_number + 1}: "
                        "text natiu detectat → sense OCR"
                    )

                    all_text.append(
                        f"\n--- PÀGINA {page_number + 1} ---\n"
                        f"{native_text}"
                    )

                    continue

                # =================================================
                # SI NO HI HA TEXT → OCR
                # =================================================

                print(
                    f"Pàgina {page_number + 1}: "
                    "escanejada → executant OCR"
                )

                # ------------------------------------------------
                # RESOLUCIÓ OCR
                #
                # 3x: amb 1.5x el text sortia il·legible
                # (dates com "aa082026" en lloc de "05/09/2026").
                # A 3x, per a una factura d'una pàgina, la imatge
                # pesa ~13 MB en RAM, cosa assumible fins i tot
                # amb 512 MB de límit (es allibera de seguida
                # amb gc.collect()).
                # ------------------------------------------------

                matrix = fitz.Matrix(
                    3.0,
                    3.0
                )

                # ------------------------------------------------
                # Renderitzar pàgina
                # ------------------------------------------------

                pix = page.get_pixmap(
                    matrix=matrix,
                    alpha=False
                )

                # ------------------------------------------------
                # Convertir a PNG
                # ------------------------------------------------

                image_bytes = pix.tobytes(
                    "png"
                )

                image = Image.open(
                    io.BytesIO(
                        image_bytes
                    )
                )

                image.load()

                # ------------------------------------------------
                # OCR
                # ------------------------------------------------

                text = perform_ocr(
                    image
                )

                all_text.append(
                    f"\n--- PÀGINA {page_number + 1} ---\n"
                    f"{text}"
                )

            finally:

                # ------------------------------------------------
                # Alliberar PIL
                # ------------------------------------------------

                if image is not None:

                    try:
                        image.close()
                    except Exception:
                        pass

                # ------------------------------------------------
                # Alliberar pixmap
                # ------------------------------------------------

                pix = None

                # ------------------------------------------------
                # Alliberar pàgina
                # ------------------------------------------------

                page = None

                # ------------------------------------------------
                # Forçar neteja RAM
                # ------------------------------------------------

                gc.collect()

        return "\n".join(
            all_text
        )

    finally:

        # --------------------------------------------------------
        # Tancar PDF
        # --------------------------------------------------------

        if document is not None:

            try:
                document.close()
            except Exception:
                pass

        gc.collect()


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

        " Iva": " IVA",
        " lva": " IVA",

        "Iva": "IVA",
        "lva": "IVA",

        "IGIC": "IGIC",
    }

    for old, new in replacements.items():

        text = text.replace(
            old,
            new
        )

    # Eliminar espais i tabuladors duplicats
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

        r"Factura\s*#?\s*([A-Z0-9]+[-/][A-Z0-9-]+)",

        r"Factura\s*#?\s*([A-Z]{2,5}\d+[-/]\d+)",

        r"Factura\s*#?\s*([FES]\w+)",

        r"\bFES\d+[-]\d+\b",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            if match.lastindex:

                return match.group(
                    1
                ).strip()

            return match.group(
                0
            ).strip()

    return None


# ============================================================
# DATA DE FACTURA
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

            return match.group(
                1
            )

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

            return match.group(
                1
            )

    return None


# ============================================================
# PACIENTES
# ============================================================

def extract_patients(text):
    """
    Intenta detectar pacients a partir de les files OCR.
    """

    patients = []

    lines = text.splitlines()

    for line in lines:

        line = line.strip()

        if not line:
            continue

        # Buscar una data al principi
        date_match = re.match(
            r"^(\d{2}/\d{2}/\d{4})\s+(.+)",
            line
        )

        if not date_match:
            continue

        remainder = date_match.group(
            2
        ).strip()

        parts = remainder.split()

        if len(parts) < 2:
            continue

        patient = parts[0].strip()

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

        # Els noms dels pacients solen estar en majúscules.
        if (
            patient.upper() == patient
            and len(patient) >= 2
            and len(patient) <= 40
        ):

            if patient not in patients:

                patients.append(
                    patient
                )

    return patients


# ============================================================
# TAULA
# ============================================================

def extract_table_rows(text):
    """
    Reconstrueix les files de la factura a partir de l'OCR.
    """

    rows = []

    lines = text.splitlines()

    # Guardem l'última data vista: hi ha factures on l'última
    # línia d'un pacient (ex: RADIOGRAFIA) surt sense data
    # perquè a l'original ja comparteix data amb la fila anterior.
    last_date = None
    last_patient = None

    for line in lines:

        line = line.strip()

        if not line:
            continue

        # ----------------------------------------------------
        # Buscar data al principi
        # ----------------------------------------------------

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

            # Línia sense data explícita però que continua
            # amb el mateix pacient (ex: RADIOGRAFIA INICIAL).
            fecha = last_date
            content = line

        else:
            continue

        # ----------------------------------------------------
        # Imports amb decimals (preu, IVA valor, importe)
        #
        # IMPORTANT: només comptem com "import" els números
        # AMB DECIMALS (ex: "78,51"). Un número solt com "1"
        # (la quantitat) NO porta decimals, així que amb això
        # ja no es confon la quantitat amb el preu.
        # ----------------------------------------------------

        decimal_pattern = r"(\d{1,4}[.,]\d{2})\s*€?"

        decimal_values = re.findall(
            decimal_pattern,
            content
        )

        if len(decimal_values) < 2:
            continue

        # La factura sempre porta, per aquest ordre:
        # preu, (iva%), iva_valor, importe
        # Els 3 últims decimals són sempre
        # preu / iva_valor / importe.

        precio = decimal_values[-3] + " €" if len(decimal_values) >= 3 else (decimal_values[0] + " €")
        iva_valor = decimal_values[-2] + " €" if len(decimal_values) >= 2 else ""
        importe = decimal_values[-1] + " €"

        # ----------------------------------------------------
        # IVA %
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
        # Eliminar de "content" tot el que ja hem identificat
        # (decimals, iva%) per quedar-nos només amb:
        # PACIENT ARTICLE CANTITAT
        # ----------------------------------------------------

        remainder = re.sub(
            decimal_pattern,
            " ",
            content
        )

        remainder = re.sub(
            r"\d{1,2}\s*%",
            " ",
            remainder
        )

        remainder = re.sub(
            r"\s+",
            " ",
            remainder
        ).strip()

        words = remainder.split()

        if len(words) < 2:
            continue

        # Primer element = pacient
        paciente = words[0]

        # ----------------------------------------------------
        # Cantitat: sol ser l'últim número solt que queda
        # (sense decimals) just abans dels imports.
        # Si no en trobem cap, per defecte és "1".
        # ----------------------------------------------------

        cantidad = "1"

        rest_words = words[1:]

        for i in range(len(rest_words) - 1, -1, -1):

            if re.fullmatch(r"\d{1,3}", rest_words[i]):

                cantidad = rest_words[i]

                rest_words = (
                    rest_words[:i]
                    + rest_words[i + 1:]
                )

                break

        # La resta = article
        articulo = " ".join(
            rest_words
        ).strip()

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

        rows.append(
            row
        )

        last_date = fecha
        last_patient = paciente

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

    # --------------------------------------------------------
    # OCR / TEXT
    # --------------------------------------------------------

    full_text = extract_all_text(
        pdf_path
    )

    full_text = normalize_text(
        full_text
    )

    # --------------------------------------------------------
    # Mostrar OCR en terminal
    # --------------------------------------------------------

    print()
    print("========== TEXT EXTRET ==========")
    print()
    print(full_text)
    print()
    print("========== FI TEXT ===============")
    print()

    # --------------------------------------------------------
    # Dades factura
    # --------------------------------------------------------

    invoice_number = extract_invoice_number(
        full_text
    )

    invoice_date = extract_invoice_date(
        full_text
    )

    total = extract_total(
        full_text
    )

    # --------------------------------------------------------
    # Taula
    # --------------------------------------------------------

    table_rows = extract_table_rows(
        full_text
    )

    # --------------------------------------------------------
    # Pacients
    # --------------------------------------------------------

    patients = extract_patients(
        full_text
    )

    # Si hem trobat pacients a la taula,
    # els afegim si encara no hi són.

    for row in table_rows:

        patient = row.get(
            "paciente",
            ""
        ).strip()

        if (
            patient
            and patient not in patients
        ):

            patients.append(
                patient
            )

    # --------------------------------------------------------
    # Resultat
    # --------------------------------------------------------

    data = {

        "invoice_number":
            invoice_number,

        "invoice_date":
            invoice_date,

        "total":
            total,

        "patients":
            patients,

        "table_rows":
            table_rows
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
        print("             NOU PDF")
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

        file = request.files[
            "file"
        ]

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

        if not file.filename.lower().endswith(
            ".pdf"
        ):

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

        file.save(
            filepath
        )

        file_size = filepath.stat().st_size

        print(
            f"PDF rebut: "
            f"{original_filename}"
        )

        print(
            f"Mida: "
            f"{file_size / (1024 * 1024):.2f} MB"
        )

        # ----------------------------------------------------
        # OCR / PROCESSAMENT
        # ----------------------------------------------------

        extracted_data = extract_table_data(
            filepath
        )

        # ----------------------------------------------------
        # Resposta
        # ----------------------------------------------------

        print()
        print("PROCESSAMENT FINALITZAT")
        print()

        return jsonify({

            "success": True,

            "data":
                extracted_data,

            "filename":
                original_filename

        }), 200

    except Exception as error:

        print()
        print("========================================")
        print("              ERROR")
        print("========================================")
        print(
            str(error)
        )
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

        # ----------------------------------------------------
        # ELIMINAR PDF TEMPORAL
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Alliberar RAM
        # ----------------------------------------------------

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

        "status":
            "ok",

        "ocr":
            "enabled"
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
    print(
        "http://localhost:5000/"
    )

    print()

    print("Health:")
    print(
        "http://localhost:5000/health"
    )

    print()

    print("Configuració:")
    print(
        "  OCR: 1.5x"
    )
    print(
        "  MAX PDF: 20 MB"
    )
    print(
        f"  MAX PÀGINES: {MAX_PAGES}"
    )
    print(
        "  OCR només quan cal"
    )

    print()
    print("========================================")
    print()

    import os

    app.run(
        debug=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )

