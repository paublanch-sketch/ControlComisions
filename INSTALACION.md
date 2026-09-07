# 🔧 Guía Paso a Paso - Instalación

## 🖥️ OPCIÓN 1: Windows (MÁS FÁCIL)

### Paso 1: Descargar Python
1. Ve a https://www.python.org/downloads/
2. Descarga Python 3.10 o superior
3. **IMPORTANTE**: Al instalar, marca la opción **"Add Python to PATH"**

### Paso 2: Descargar el Proyecto
1. Descarga todos los archivos del proyecto en una carpeta
2. Abre la carpeta en el explorador

### Paso 3: Ejecutar
1. Haz **doble clic** en `run.bat`
2. Se abrirá una ventana de comandos y se instalará todo automáticamente
3. Verás un mensaje como:
   ```
   ✓ Backend ejecutándose en:
   http://localhost:5000
   ```

### Paso 4: Abrir en el navegador
1. Ve a: `http://localhost:5000`
2. ¡Listo! Ya puedes usar la aplicación

---

## 🍎 OPCIÓN 2: Mac

### Paso 1: Instalar Homebrew (si no lo tienes)
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### Paso 2: Instalar Python
```bash
brew install python3
```

### Paso 3: Descargar el Proyecto
1. Descarga los archivos en una carpeta
2. Abre Terminal

### Paso 4: Navegar a la carpeta
```bash
cd /ruta/de/tu/carpeta
```

### Paso 5: Ejecutar el script
```bash
chmod +x run.sh
./run.sh
```

### Paso 6: Abrir en el navegador
- Ve a: `http://localhost:5000`

---

## 🐧 OPCIÓN 3: Linux (Ubuntu/Debian)

### Paso 1: Instalar dependencias
```bash
sudo apt-get update
sudo apt-get install python3 python3-pip python3-venv
```

### Paso 2: Descargar el Proyecto
```bash
cd tu-carpeta-proyecto
```

### Paso 3: Ejecutar
```bash
chmod +x run.sh
./run.sh
```

### Paso 4: Abrir en el navegador
- Ve a: `http://localhost:5000`

---

## ✅ Verificar que Funciona

Una vez ejecutado, deberías ver en la terminal:
```
 * Running on http://localhost:5000
 * Debug mode: on
```

Si ves esto, está funcionando perfectamente.

---

## 🆘 Problemas Comunes

### "Python no se encuentra"
**Solución**: Desinstala Python e instálalo de nuevo, marcando "Add Python to PATH"

### "El puerto 5000 ya está en uso"
**Solución**: Edita `app.py` y cambia el puerto:
```python
app.run(debug=True, host='localhost', port=5001)  # Cambia 5000 por 5001
```

### "No puedo acceder a localhost:5000"
**Solución**: 
1. Verifica que la terminal muestre "Running on..."
2. Intenta con `http://127.0.0.1:5000`
3. Revisa el firewall de tu PC

### Error al procesar PDF
- El PDF debe tener una tabla estructurada
- Prueba con los PDFs de ejemplo

---

## 📚 Uso

1. **Abre** `http://localhost:5000` en tu navegador
2. **Sube** un archivo PDF (o arrastra)
3. **Espera** a que se procese
4. **Visualiza** los datos en la tabla
5. **Exporta** a JSON o CSV si lo necesitas

---

## 🛑 Para Detener

En la terminal/cmd, presiona: **Ctrl + C**

---

**¡Si todo está configurado correctamente, ¡deberías ver la aplicación funcionando en segundos!** 🚀
