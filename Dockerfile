FROM python:3.10-slim

# Evita demanar opcions interactives durant la instal·lació
ENV DEBIAN_FRONTEND=noninteractive

# Instal·la Tesseract OCR, els idiomes (Espanyol i Català) i llibreries del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-cat \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia i instal·la les dependències de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia tot el codi de la teva aplicació
COPY . .

# Executa l'aplicació amb Gunicorn en el port de Render
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "app:app"]