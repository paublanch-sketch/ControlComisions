FROM python:3.10-slim

# Instal·lar Tesseract OCR i idiomes (Espanyol i Català)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-cat \
    libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Executar amb Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "app:app"]