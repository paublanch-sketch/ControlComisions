/*
 * EXTRACTOR DE FACTURES OCR -> GOOGLE SHEETS
 *
 * Cada veterinari té el seu propi arxiu.
 * Dins de cada arxiu es crea una pestanya per mes:
 *   Gener 2026, Febrer 2026, ... Desembre 2026
 *
 * Si la pestanya del mes NO existeix:
 *   - intenta copiar la pestanya "PLANTILLA"
 *   - si "PLANTILLA" no existeix, crea una plantilla bàsica.
 *
 * En exportar:
 *   - es conserva el format de la plantilla
 *   - es esborra el contingut de la zona de dades des de A3:E
 *   - s'escriuen NOMÉS les línies seleccionades, començant a A3
 *   - es calcula el 5% sobre PREU S/IVA (columna D)
 *
 * Nota:
 * L'usuari va demanar que la informació es col·loqui des de A1. En aquest disseny,
 * A1 és el títol/capçalera de la plantilla i les dades comencen a A3,
 * respectant el format mostrat.
 */

const SHEETS = {
  Roser:  'ID_GOOGLE_SHEET_ROSER',
  Carlos: 'ID_GOOGLE_SHEET_CARLOS',
  Laura:  'ID_GOOGLE_SHEET_LAURA',
  Maria:  'ID_GOOGLE_SHEET_MARIA',
  Sonia:  'ID_GOOGLE_SHEET_SONIA',
  Sara:   'ID_GOOGLE_SHEET_SARA'
};

const TEMPLATE_TAB_NAME = 'PLANTILLA';

// Capçaleres de la plantilla.
const HEADERS = ['DATA', 'FACTURA', 'DESCRIPCIÓ', 'PREU S/IVA', '5%'];

// Primera fila de dades de la plantilla.
const DATA_START_ROW = 3;

// Nombre de files de dades que volem conservar/preparar.
// La plantilla de l'exemple té 10 files de dades (3..12).
const TEMPLATE_DATA_ROWS = 10;

const MONTH_NAMES = [
  'Gener', 'Febrer', 'Març', 'Abril', 'Maig', 'Juny',
  'Juliol', 'Agost', 'Setembre', 'Octubre', 'Novembre', 'Desembre'
];

function doGet() {
  return ContentService
    .createTextOutput(JSON.stringify({
      ok: true,
      message: 'Extractor de Factures OCR connectat'
    }))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      throw new Error('No s’ha rebut cap contingut.');
    }

    const payload = JSON.parse(e.postData.contents);
    const person = String(payload.person || '').trim();
    const month = String(payload.month || '').trim();

    if (!Object.prototype.hasOwnProperty.call(SHEETS, person)) {
      throw new Error('Persona no vàlida: ' + person);
    }

    validateMonth(month);

    const spreadsheetId = SHEETS[person];
    if (!spreadsheetId || spreadsheetId.indexOf('ID_GOOGLE_SHEET_') === 0) {
      throw new Error('Falta configurar l’ID de Google Sheet de ' + person);
    }

    const rows = Array.isArray(payload.table_rows) ? payload.table_rows : [];
    if (!rows.length) {
      throw new Error('No hi ha línies seleccionades per exportar.');
    }

    const ss = SpreadsheetApp.openById(spreadsheetId);
    const monthSheet = getOrCreateMonthSheet(ss, month, person);

    // Neteja NOMÉS el contingut de la zona de dades i conserva el format.
    clearDataArea(monthSheet);

    // Assegura títol i capçaleres principals.
    writeTemplateHeader(monthSheet, person, month);

    // Converteix les línies OCR al format de la plantilla.
    const values = rows.map(r => [
      normalizeDateForSheet(r.fecha || ''),
      r.factura || payload.invoice?.invoice_number || '',
      r.articulo || '',
      parseMoney(r.precio),
      calculateCommission5(r.precio)
    ]);

    monthSheet.getRange(DATA_START_ROW, 1, values.length, HEADERS.length).setValues(values);

    // Format numèric a D i E.
    monthSheet.getRange(DATA_START_ROW, 4, values.length, 2).setNumberFormat('#,##0.00 €');

    // Nota de control a A1.
    const invoice = payload.invoice || {};
    monthSheet.getRange('A1').setNote(
      'Veterinari: ' + person +
      '\nMes: ' + month +
      '\nFactura(es): ' + (invoice.invoice_number || '') +
      '\nData factura: ' + (invoice.invoice_date || '') +
      '\nExportades: ' + values.length +
      '\nÚltima exportació: ' + new Date().toLocaleString()
    );

    return jsonResponse({
      ok: true,
      person: person,
      month: month,
      sheet_name: monthSheet.getName(),
      exported_rows: values.length
    });

  } catch (err) {
    return jsonResponse({
      ok: false,
      error: String(err && err.message ? err.message : err)
    });
  }
}

function validateMonth(month) {
  // Format esperat: YYYY-MM.
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
    throw new Error('Mes no vàlid. Ha de tenir format YYYY-MM.');
  }
}

function getOrCreateMonthSheet(ss, month, person) {
  const monthName = monthToSheetName(month);

  let sheet = ss.getSheetByName(monthName);
  if (sheet) return sheet;

  const template = ss.getSheetByName(TEMPLATE_TAB_NAME);

  if (template) {
    // Copia la pestanya PLANTILLA amb TOT el seu format.
    sheet = template.copyTo(ss).setName(monthName);

    // La còpia arrossega dades d'exemple; es netejaran després.
    return sheet;
  }

  // Fallback si no existeix pestanya PLANTILLA.
  sheet = ss.insertSheet(monthName);
  buildFallbackTemplate(sheet, person, month);
  return sheet;
}

function monthToSheetName(month) {
  const parts = month.split('-');
  const year = Number(parts[0]);
  const monthIndex = Number(parts[1]) - 1;
  return MONTH_NAMES[monthIndex] + ' ' + year;
}

function clearDataArea(sheet) {
  const lastRow = Math.max(sheet.getMaxRows(), DATA_START_ROW + TEMPLATE_DATA_ROWS - 1);
  const rowsToClear = Math.max(1, lastRow - DATA_START_ROW + 1);

  // A:E des de la primera fila de dades. Només clearContent per conservar format.
  sheet.getRange(DATA_START_ROW, 1, rowsToClear, 5).clearContent();
}

function writeTemplateHeader(sheet, person, month) {
  // A1:E1 es deixa com una sola capçalera visual.
  // Si la plantilla ja té combinada A1:E1, setValue a A1 funciona.
  sheet.getRange('A1').setValue(
    'COMISSIONS  ' + monthToSheetName(month).toUpperCase() +
    '  -- VETERINARI: ' + person.toUpperCase()
  );

  sheet.getRange(2, 1, 1, HEADERS.length).setValues([HEADERS]);
}

function buildFallbackTemplate(sheet, person, month) {
  sheet.getRange('A1:E1').merge();
  sheet.getRange('A1').setValue(
    'COMISSIONS  ' + monthToSheetName(month).toUpperCase() +
    '  -- VETERINARI: ' + person.toUpperCase()
  );

  sheet.getRange(2, 1, 1, HEADERS.length).setValues([HEADERS]);

  // Fila d'exemple buida/guia i 9 files més, com a la plantilla aportada.
  const blankRows = Array.from({length: TEMPLATE_DATA_ROWS}, () => ['', '', '', '', '']);
  sheet.getRange(DATA_START_ROW, 1, TEMPLATE_DATA_ROWS, 5).setValues(blankRows);

  // Format bàsic del fallback.
  sheet.getRange('A1:E1').setFontWeight('bold').setHorizontalAlignment('center');
  sheet.getRange('A2:E2').setFontWeight('bold').setHorizontalAlignment('center');
  sheet.setColumnWidth(1, 110);
  sheet.setColumnWidth(2, 170);
  sheet.setColumnWidth(3, 420);
  sheet.setColumnWidth(4, 130);
  sheet.setColumnWidth(5, 90);
  sheet.getRange(DATA_START_ROW, 4, TEMPLATE_DATA_ROWS, 2).setNumberFormat('#,##0.00 €');
}

function parseMoney(value) {
  if (value === null || value === undefined || value === '') return '';

  let s = String(value)
    .replace(/\u00a0/g, ' ')
    .replace(/€/g, '')
    .trim();

  // Suporta:
  // 107,44
  // 1.234,56
  // 107.44
  if (s.includes(',') && s.includes('.')) {
    s = s.replace(/\./g, '').replace(',', '.');
  } else if (s.includes(',')) {
    s = s.replace(',', '.');
  }

  const num = Number(s.replace(/[^0-9.-]/g, ''));
  return Number.isFinite(num) ? num : '';
}

function calculateCommission5(price) {
  const num = parseMoney(price);
  if (num === '') return '';
  return Math.round((num * 0.05 + Number.EPSILON) * 100) / 100;
}

function normalizeDateForSheet(dateValue) {
  if (!dateValue) return '';

  const s = String(dateValue).trim();

  // dd/mm/yyyy
  let m = s.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{4})$/);
  if (m) {
    const d = Number(m[1]);
    const mo = Number(m[2]);
    const y = Number(m[3]);
    return new Date(y, mo - 1, d);
  }

  // yyyy-mm-dd
  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) {
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }

  // Si no es pot interpretar, conserva el text.
  return s;
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}