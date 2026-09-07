import io
import os
from pathlib import Path
from flask import Flask, request, jsonify
from PIL import Image
import fitz  # PyMuPDF
# importa aquí la resta de llibreries o funcions que utilitzis (p. ex. pytesseract, extract_table_data, etc.)

app = Flask(__name__)

# Configuració de la carpeta temporal
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


# ============================================================
# FUNCIONS D'UTILITAT I OCR
# ============================================================

def pdf_to_images(pdf_path):
    """
    Converteix totes les pàgines del PDF en imatges.
    Fem servir zoom 1.5x en comptes de 3x per evitar excedir els 512MB de RAM de Render.
    """
    document = fitz.open(pdf_path)
    images = []

    for page in document:
        # Matriu d'1.5x (suficient per a OCR i molt més lleugera en memòria)
        matrix = fitz.Matrix(1.5, 1.5)

        pix = page.get_pixmap(
            matrix=matrix,
            alpha=False
        )

        image_bytes = pix.tobytes("png")

        image = Image.open(
            io.BytesIO(image_bytes)
        )

        images.append(image)

    document.close()
    return images


def extract_table_data(pdf_path):
    """
    Aquesta funció processa el PDF convertint-lo a imatges
    i aplicant-hi l'OCR.
    """
    images = pdf_to_images(pdf_path)
    extracted_data = []

    for idx, image in enumerate(images):
        # Aquí executes la teva lògica d'OCR (p. ex. pytesseract)
        # data = pytesseract.image_to_string(image)
        # extracted_data.append(data)
        pass

    return extracted_data


# ============================================================
# RUTES FLASK
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "message": "API d'OCR i processament de PDF activa."
    }), 200


@app.route("/upload", methods=["POST"])
def upload_file():
    filepath = None

    try:
        # ----------------------------------------------------
        # Comprovar fitxer
        # ----------------------------------------------------
        if "file" not in request.files:
            return jsonify({
                "success": False,
                "error": "No s'ha proporcionat cap fitxer."
            }), 400

        file = request.files["file"]

        if file.filename == "":
            return jsonify({
                "success": False,
                "error": "No s'ha seleccionat cap fitxer."
            }), 400

        # ----------------------------------------------------
        # Comprovar extensió
        # ----------------------------------------------------
        if not file.filename.lower().endswith(".pdf"):
            return jsonify({
                "success": False,
                "error": "Només s'accepten fitxers PDF."
            }), 400

        # ----------------------------------------------------
        # Nom segur i desat
        # ----------------------------------------------------
        filename = Path(file.filename).name
        filepath = UPLOAD_FOLDER / filename

        file.save(filepath)

        print(f"\nPDF rebut: {filepath}")

        # ----------------------------------------------------
        # OCR / Extracció de dades
        # ----------------------------------------------------
        extracted_data = extract_table_data(filepath)

        # ----------------------------------------------------
        # Resposta
        # ----------------------------------------------------
        return jsonify({
            "success": True,
            "data": extracted_data,
            "filename": filename
        }), 200

    except Exception as error:
        print("\n========================================")
        print("              ERROR")
        print("========================================")
        print(str(error))
        print()

        return jsonify({
            "success": False,
            "error": f"S'ha produït un error processant el fitxer: {str(error)}"
        }), 500

    finally:
        # ----------------------------------------------------
        # Neteja de fitxers temporals del disc
        # ----------------------------------------------------
        if filepath and filepath.exists():
            try:
                filepath.unlink()
            except Exception as e:
                print(f"No s'ha pogut esborrar el fitxer temporal: {e}")


# ============================================================
# PUNT D'ENTRADA
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)