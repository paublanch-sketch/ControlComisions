EXTRACTOR DE FACTURAS OCR → GOOGLE SHEETS
===============================================

AHORA INCLUYE
-------------
- Selector de persona: Roser, Carlos, Laura, Maria, Sonia, Sara.
- Selector de MES.
- Selección de líneas haciendo clic: quedan VERDES.
- Solo las líneas verdes se exportan.
- Cada persona tiene su propio Google Sheet.
- Dentro de cada Google Sheet hay una pestaña por mes.
- Si el mes no existe, se crea automáticamente.
- Si existe la pestaña PLANTILLA, el mes nuevo se crea copiándola, conservando su formato.
- Si no existe PLANTILLA, el sistema genera una plantilla básica.
- Los datos se escriben en la estructura:
  A DATA
  B FACTURA
  C DESCRIPCIÓ
  D PREU S/IVA
  E 5%
- El 5% se calcula automáticamente sobre PREU S/IVA.

ESTRUCTURA RECOMENDADA EN CADA GOOGLE SHEET
--------------------------------------------
En cada archivo de Roser, Carlos, Laura, Maria, Sonia y Sara:

  PLANTILLA
  Enero 2026
  Febrero 2026
  Marzo 2026
  ...

No hace falta crear manualmente todos los meses.

IMPORTANTE: PLANTILLA
---------------------
Crea una pestaña llamada exactamente:

PLANTILLA

Pon ahí el diseño que quieres utilizar para todos los meses.

Cuando el usuario elige "Mayo 2026":
- si "Mayo 2026" ya existe, usa esa pestaña;
- si NO existe, copia PLANTILLA y la llama "Mayo 2026".

Así todos los meses nuevos conservan el mismo diseño.

FORMATO DE LA PLANTILLA
------------------------
El diseño que has mostrado es:

Fila 1:
COMISIONES MESX 2026 -- VETERINARIO: XXXXXXXXXXXXXXXXX

Fila 2:
DATA | FACTURA | DESCRIPCIÓ | PREU S/IVA | 5%

Filas 3 en adelante:
cada línea seleccionada.

El sistema respeta esto y empieza a poner las líneas en A3.
A1 sigue siendo el título y A2:E2 las cabeceras.

CONFIGURAR LOS 6 SHEETS
-----------------------
En Code_GOOGLE_SHEETS_6_PERSONAS.gs sustituye:

ID_GOOGLE_SHEET_ROSER
ID_GOOGLE_SHEET_CARLOS
ID_GOOGLE_SHEET_LAURA
ID_GOOGLE_SHEET_MARIA
ID_GOOGLE_SHEET_SONIA
ID_GOOGLE_SHEET_SARA

por los IDs reales.

El ID está en la URL del archivo:
https://docs.google.com/spreadsheets/d/EL_ID_DEL_ARCHIVO/edit

PUBLICAR APPS SCRIPT
--------------------
1. Ve a script.google.com
2. Nuevo proyecto.
3. Pega Code_GOOGLE_SHEETS_6_PERSONAS.gs
4. Guarda.
5. Implementar → Nueva implementación.
6. Tipo: Aplicación web.
7. Ejecutar como: tu cuenta.
8. Quién tiene acceso: Cualquiera.
9. Copia la URL.

CONFIGURAR EL HTML
------------------
En extractor_facturas_google.html busca:

const GOOGLE_APPS_SCRIPT_URL = 'PEGA_AQUI_LA_URL_DE_TU_WEB_APP';

Sustitúyela por la URL de la Web App.

CÓMO SE USA
-----------
1. Cargas el PDF.
2. El OCR muestra las líneas.
3. Haces clic en las líneas que quieres exportar.
4. Las seleccionadas se ponen verdes.
5. Seleccionas la persona.
6. Seleccionas el mes.
7. Pulsas "Exportar ... a Google Sheets".
8. Solo esas líneas se escriben en la pestaña del mes elegido.

EJEMPLO
-------
Seleccionas:
- CONAN — ECOGRAFÍA ABDOMINAL — 78,51 €
- ZOE — CONSULTA — 50,00 €

Eliges:
Persona = Laura
Mes = Mayo 2026

Resultado en el archivo de Laura, pestaña:
Mayo 2026

Solo se escriben esas dos líneas.

COMISIÓN DEL 5%
----------------
Si PREU S/IVA = 107,44 €:
5% = 5,37 €

El script lo calcula automáticamente.

NOTA SOBRE "DESDE A1"
---------------------
En tu diseño, A1 contiene el título, A2 contiene las cabeceras y las
líneas empiezan en A3. Por eso el programa no borra el título ni desplaza
la plantilla: limpia A3:E y escribe ahí las líneas seleccionadas.

Si después me proporcionas el archivo real de la plantilla Excel/Sheets,
el mapeo puede ajustarse a sus celdas exactas y a su formato real.
