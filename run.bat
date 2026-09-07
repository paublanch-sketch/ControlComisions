@echo off
echo =============================================
echo   Extractor de Facturas - Iniciando...
echo =============================================
echo.

REM Verificar si existe venv
if not exist venv (
    echo Creando entorno virtual...
    python -m venv venv
    call venv\Scripts\activate
    echo Instalando dependencias...
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)

echo.
echo =============================================
echo   ✓ Backend ejecutándose en:
echo   http://localhost:5000
echo =============================================
echo.
echo   Abre tu navegador y ve a:
echo   http://localhost:5000
echo.
echo   Presiona Ctrl+C para detener
echo =============================================
echo.

python app.py

pause
