# 🔧 Qué estaba fallando y qué he arreglado

## El diagnóstico real

Tus facturas del AniCura **no tienen texto, son una imagen** metida dentro
del PDF (lo comprobé extrayendo el PDF con PyMuPDF: solo hay un bloque de
tipo "imagen", cero texto). Por eso tu cambio a OCR con Tesseract era el
camino correcto, pero tenía 2 bugs y le faltaba la pieza de despliegue:

### Bug 1 — Resolución de OCR demasiado baja
Estabas renderizando el PDF a 1.5x antes de pasarlo por Tesseract. A esa
resolución, el texto sale ilegible:
```
aa082026 CONAN — EUTANASIA GRTO 15702€ ...
```
Ninguna fecha hacía match con tu regex → la tabla y los pacientes salían
**siempre vacíos**, tanto en local como en Render. No era un problema de
despliegue, era de calidad de imagen.

**Arreglado:** ahora renderiza a **3x** y además binariza la imagen
(blanco/negro puro) en vez de solo subir el contraste. Con esto el texto
sale prácticamente perfecto:
```
05/09/2026 CONAN ECOGRAFIA ABDOMINAL INICIAL GATO 78,51 € 1 21% 16,49 € 95,00 €
```

### Bug 2 — Cantidad y precio confundidos
Aunque el OCR saliera limpio, tu regex de "cantidad" cogía el primer
número que encontraba en la línea, que normalmente era el **precio**, no
la cantidad real (casi siempre "1").

**Arreglado:** ahora primero identifico los importes con decimales
(78,51 / 16,49 / 95,00) y lo que queda aparte, si es un número suelto sin
decimales, es la cantidad.

### Bug 3 (extra) — Fila sin fecha (Radiografía)
En tus facturas, la última línea de un paciente (ej. "RADIOGRAFIA
INICIAL") a veces no lleva fecha propia en el PDF original. Antes se
perdía esa fila entera. Ahora, si una línea no tiene fecha pero sigue con
el mismo paciente, hereda la fecha de la fila anterior.

## Por qué fallaba en Render (y por qué en local también, aparte de los bugs)

**Tesseract OCR no es una librería de Python, es un programa del sistema
operativo.** `pip install pytesseract` solo instala el "conector" a
Python, pero si el programa `tesseract.exe` (Windows) o `tesseract`
(Linux) no está instalado aparte, todo falla con
`TesseractNotFoundError`.

- **En Windows (local):** tienes que descargar e instalar Tesseract-OCR
  como programa aparte, no solo con pip.
- **En Render (gratis, runtime "Python"):** no puedes hacer
  `apt-get install`, así que Tesseract nunca estaría disponible en ese
  plan. **Por eso te añado un `Dockerfile`**: con Docker sí puedes decirle
  a Render "instala tesseract-ocr" antes de arrancar la app, y el plan
  free de Render sigue siendo gratis con Docker.

---

## 📥 Qué hacer ahora

### 1. Instalar Tesseract en tu Windows (para poder probarlo en local)

1. Descarga el instalador desde:
   https://github.com/UB-Mannheim/tesseract/wiki
   (usa el .exe de 64 bits, ej. `tesseract-ocr-w64-setup-5.x.x.exe`)
2. Durante la instalación, en "Additional language data" marca
   **Spanish** y **Catalan**.
3. Instálalo en la ruta por defecto:
   `C:\Program Files\Tesseract-OCR\`
4. Abre `app.py` y descomenta/añade esta línea justo debajo de los
   imports (ya está preparada, solo hay que quitar el `#`):

```python
pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)
```

5. Reinicia tu terminal/VSCode para que coja el PATH actualizado.

### 2. Probar en local

```bash
pip install -r requirements.txt
python app.py
```
Abre `http://localhost:5000` y sube una factura.

### 3. Desplegar en Render (con Docker, plan Free)

1. Sube estos archivos a tu repo de GitHub: `app.py`, `index.html`,
   `requirements.txt`, `Dockerfile`, `render.yaml`.
2. En Render: **New +** → **Blueprint** (o "Web Service") → conecta el
   repo.
3. Render detecta el `Dockerfile` automáticamente (o usa el `render.yaml`
   si eliges Blueprint). **Importante:** en el tipo de entorno elige
   **Docker**, no "Python", si te lo pregunta.
4. Deja el plan **Free**. El build tardará un poco más la primera vez
   porque instala Tesseract dentro de la imagen Docker.
5. Cuando termine, Render te da una URL tipo
   `https://extractor-facturas.onrender.com`.

### 4. Conectar con tu Google Apps Script

Ya tienes la URL del Web App de Apps Script metida en `index.html`
(`GOOGLE_APPS_SCRIPT_URL`). Solo cambia si en algún momento vuelves a
publicar una nueva versión del Apps Script (Google te da una URL nueva
cada vez que haces "Nueva implementación").

---

## ⚠️ Limitación conocida

Con OCR nunca vas a tener el 100% de precisión garantizado en el 100% de
los casos (aunque con 3x + binarización sale muy bien en tus facturas de
ejemplo). Si algún día ves un artículo raro o un número mal leído,
revisa/corrige manualmente esa fila antes de exportar a Sheets — por eso
tu interfaz ya permite seleccionar fila por fila antes de mandar a
Google Sheets, es la red de seguridad correcta.
