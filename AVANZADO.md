# 🔬 Guía Avanzada - Personalización y Extensión

## 📊 Cómo Funciona la Extracción

### 1. Lectura del PDF
```python
with pdfplumber.open(pdf_path) as pdf:
    page = pdf.pages[0]  # Primera página
    tables = page.extract_tables()  # Extrae TODAS las tablas
```

### 2. Búsqueda de Número de Factura
```python
match = re.search(r'Factura\s*#([FES0-9]+)', text)
# Busca patrón: "Factura #FES024-511260009408"
```

### 3. Procesamiento de Filas
- Lee cada fila de la tabla principal
- Detecta pacientes automáticamente
- Mantiene estructura flexible (N filas)

---

## 🛠️ Personalización

### A. Cambiar los Campos Extraídos

En `app.py`, función `extract_table_data()`:

**Actual:**
```python
row_data = {
    'fecha': clean_row[0],
    'paciente': clean_row[1],
    'articulo': clean_row[2],
    ...
}
```

**Para agregar campos:**
```python
row_data = {
    'fecha': clean_row[0],
    'paciente': clean_row[1],
    'articulo': clean_row[2],
    'descripcion_extra': clean_row[8],  # Nuevo campo
    'codigo_articulo': clean_row[9],     # Otro nuevo
}
```

**Luego en `index.html` agregar columna a la tabla:**
```html
<th>Descripción Extra</th>
<th>Código</th>
```

Y en el JavaScript:
```javascript
<td>${row.descripcion_extra || '-'}</td>
<td>${row.codigo_articulo || '-'}</td>
```

---

### B. Cambiar el Patrón de Número de Factura

Si tus facturas tienen otro formato:

```python
# Actual:
match = re.search(r'Factura\s*#([FES0-9]+)', text)

# Ejemplo: si es "INV-2024-001"
match = re.search(r'INV-(\d{4}-\d{3})', text)

# Ejemplo: si está en formato "Invoice: ABC123XYZ"
match = re.search(r'Invoice:\s*([A-Z0-9]+)', text)
```

---

### C. Extraer Información del Pagador

Actualmente extrae beneficiario. Para agregar pagador:

```python
def extract_payer_info(text):
    """Extrae info del pagador"""
    payer_section = text.split('Pagador')
    if len(payer_section) > 1:
        lines = payer_section[1].split('\n')[:4]
        return '\n'.join(lines)
    return None

# Luego en extract_table_data():
data['payer'] = extract_payer_info(full_text)
```

---

### D. Filtrar Filas Específicas

Si quieres ignorar ciertas filas (como encabezados internos):

```python
# Agregar en el bucle de procesamiento:
if row[1] == 'SKIP_TEXT':  # Ignora esta fila
    continue

if 'TOTAL' in str(row).upper():  # Ignora filas con TOTAL
    continue
```

---

### E. Cambiar Puertos y Configuración

**Puerto del servidor:**
```python
# En app.py, línea final:
app.run(debug=True, host='0.0.0.0', port=8080)  # Usa 8080 en lugar de 5000
```

**Modo producción:**
```python
# Cambiar debug y agregar threading
app.run(debug=False, host='0.0.0.0', port=80, threaded=True)
```

---

### F. Agregar Autenticación

```python
from functools import wraps

VALID_TOKENS = ['tu-token-secreto']

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token or token.replace('Bearer ', '') not in VALID_TOKENS:
            return {'error': 'Unauthorized'}, 401
        return f(*args, **kwargs)
    return decorated

@app.route('/upload', methods=['POST'])
@token_required
def upload_file():
    # ... código anterior
```

---

### G. Validar PDFs Antes de Procesar

```python
def validate_pdf(pdf_path):
    """Valida que el PDF tenga estructura esperada"""
    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf.pages) == 0:
            raise ValueError('PDF vacío')
        
        page = pdf.pages[0]
        tables = page.extract_tables()
        
        if not tables or len(tables) == 0:
            raise ValueError('No se encontraron tablas')
        
        if len(tables[0]) < 5:
            raise ValueError('Tabla demasiado pequeña')
    
    return True

# Usar en upload_file():
try:
    validate_pdf(filepath)
except ValueError as e:
    return {'error': str(e)}, 400
```

---

### H. Guardar en Base de Datos

**Instalar SQLAlchemy:**
```bash
pip install SQLAlchemy
```

**Crear modelo:**
```python
from flask_sqlalchemy import SQLAlchemy

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///facturas.db'
db = SQLAlchemy(app)

class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(50), unique=True)
    date = db.Column(db.String(10))
    total = db.Column(db.String(20))
    json_data = db.Column(db.JSON)

# Guardar:
invoice = Invoice(
    number=data['invoice_number'],
    date=data['invoice_date'],
    total=data['total'],
    json_data=data
)
db.session.add(invoice)
db.session.commit()
```

---

### I. Agregar Historial de Subidas

```python
import os
from datetime import datetime

UPLOAD_LOG = 'uploads.log'

def log_upload(filename, invoice_number, status):
    with open(UPLOAD_LOG, 'a') as f:
        timestamp = datetime.now().isoformat()
        f.write(f"{timestamp} | {filename} | {invoice_number} | {status}\n")

# En upload_file():
log_upload(file.filename, extracted_data['invoice_number'], 'success')
```

---

### J. Procesamiento Batch (Múltiples PDFs)

```python
@app.route('/batch-upload', methods=['POST'])
def batch_upload():
    """Procesa múltiples archivos a la vez"""
    files = request.files.getlist('files')
    results = []
    
    for file in files:
        if file and file.filename.endswith('.pdf'):
            filepath = f"uploads/{file.filename}"
            file.save(filepath)
            
            try:
                data = extract_table_data(filepath)
                results.append({
                    'file': file.filename,
                    'success': True,
                    'data': data
                })
            except Exception as e:
                results.append({
                    'file': file.filename,
                    'success': False,
                    'error': str(e)
                })
    
    return jsonify(results), 200
```

---

### K. Cache de Resultados

```python
from functools import lru_cache
import hashlib

cache = {}

def get_file_hash(filepath):
    with open(filepath, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()

def extract_table_data(pdf_path):
    file_hash = get_file_hash(pdf_path)
    
    if file_hash in cache:
        return cache[file_hash]
    
    data = extract_table_data_impl(pdf_path)  # Tu función original
    cache[file_hash] = data
    
    return data
```

---

## 📈 Optimizaciones

### 1. Comprimir Responses
```python
from gzip import compress

@app.after_request
def compress_response(response):
    response.data = compress(response.data)
    response.headers['Content-Encoding'] = 'gzip'
    return response
```

### 2. Limpiar Uploads Automáticamente
```python
import os
from datetime import datetime, timedelta

def cleanup_old_uploads(days=7):
    now = datetime.now()
    for file in os.listdir('uploads'):
        filepath = os.path.join('uploads', file)
        if os.path.getmtime(filepath) < (now - timedelta(days=days)).timestamp():
            os.remove(filepath)

# Ejecutar al iniciar
cleanup_old_uploads()
```

### 3. Logging Detallado
```python
import logging

logging.basicConfig(
    filename='app.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Usar:
logger.info(f"Procesando: {file.filename}")
logger.error(f"Error: {str(e)}")
```

---

## 🔌 Integración con Servicios Externos

### Google Drive (Guardar en la nube)
```bash
pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

### Email (Enviar resultados)
```python
import smtplib
from email.mime.text import MIMEText

def send_results(email, data):
    msg = MIMEText(json.dumps(data, indent=2))
    msg['Subject'] = f"Factura {data['invoice_number']}"
    
    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login('tu-email@gmail.com', 'tu-contraseña')
    server.sendmail('tu-email@gmail.com', email, msg.as_string())
    server.quit()
```

---

## 🧪 Testing

```python
# test_app.py
import pytest
from app import extract_invoice_number

def test_extract_invoice_number():
    text = "Factura #FES024-511260009408"
    assert extract_invoice_number(text) == "FES024-511260009408"

def test_extract_invoice_number_invalid():
    text = "No invoice here"
    assert extract_invoice_number(text) is None

# Ejecutar:
# pytest test_app.py
```

---

**¡Ahora tienes todo para personalizar y extender la aplicación!** 🚀
