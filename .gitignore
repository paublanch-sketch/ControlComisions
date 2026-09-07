FROM python:3.10-slim

# Instal·la Tesseract OCR i els paquets d'idioma (Espanyol i Català)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-cat \
    libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia i instal·la les dependències de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia tot el codi de la teva aplicació (app.py, HTML, etc.)
COPY . .

# Executa l'aplicació amb Gunicorn en el port que utilitza Render
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "app:app"]