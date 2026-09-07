#!/bin/bash

echo "=============================================="
echo "  Extractor de Facturas - Iniciando..."
echo "=============================================="
echo ""

# Verificar si existe venv
if [ ! -d "venv" ]; then
    echo "Creando entorno virtual..."
    python3 -m venv venv
    source venv/bin/activate
    echo "Instalando dependencias..."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo ""
echo "=============================================="
echo "  ✓ Backend ejecutándose en:"
echo "  http://localhost:5000"
echo "=============================================="
echo ""
echo "  Abre tu navegador y ve a:"
echo "  http://localhost:5000"
echo ""
echo "  Presiona Ctrl+C para detener"
echo "=============================================="
echo ""

python3 app.py
