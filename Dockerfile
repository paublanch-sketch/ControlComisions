# ============================================================
# Dockerfile per desplegar a Render (o qualsevol host Docker)
#
# Per què cal Docker i no el runtime "Python" normal de Render?
# Perquè Tesseract OCR és un PROGRAMA del sistema operatiu,
# no una llibreria de Python. El pla gratuït de Render amb
# runtime "Python" no permet fer apt-get install, així que
# sense Docker l'OCR mai funcionarà en producció (encara que
# en local sí, si tens Tesseract instal·lat a Windows).
# ============================================================

FROM python:3.11-slim

# ------------------------------------------------------------
# Instal·lar Tesseract OCR + idiomes espanyol i català
# ------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-cat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ------------------------------------------------------------
# Instal·lar dependències de Python
# ------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------
# Copiar el codi de l'aplicació
# ------------------------------------------------------------
COPY . .

# Carpeta d'uploads (per si de cas)
RUN mkdir -p uploads

# Render assigna el port dinàmicament amb $PORT
ENV PORT=5000
EXPOSE 5000

# gunicorn amb 1 sol worker I 1 sol thread: amb 512 MB de RAM
# al pla gratuït, i sent l'OCR una tasca 100% de CPU (no
# d'espera), tenir més threads no accelera res en una sola CPU
# compartida i només afegeix risc de quedar-se sense memòria
# si arriben 2 peticions a la vegada.
#
# --timeout 240: a la CPU compartida (molt limitada) del pla
# gratuït de Render, l'OCR pot trigar bastant més que en un
# ordinador normal. Amb 120s gunicorn matava el worker abans
# que acabés (WORKER TIMEOUT / SIGKILL als logs). Amb "spa"
# en lloc de "spa+cat" l'OCR ja és ~2x més ràpid, però deixem
# marge extra igualment.
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 1 --timeout 240 app:app
