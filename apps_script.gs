/*
 * EXTRACTOR DE FACTURES OCR -> GOOGLE SHEETS
 */

const SHEETS = {
  Roser:  '1AdPwLZvC_HDiwsJpybV4OBJHlmv_fyfcB-rWN8RAJBI',
  Carlos: '1MC3ZMPWs951o4lg06XWgNU6NFn8kq7F4l4AjJEu5NmY',
  X:      '1_pBgQGg2v6L7vGmTQWu0IXLjmbpBLEr5O5gLR72lwu4',
  Ti:     '1x4FnlAgunX-6q6Aqckd1lEX9BJiQ2KQfuqpHqSlSPFI',
  Laura:  '1eneWqD7QcsTvsTzlpPK0KAJycgWqcBkFe7I0GB3h0gw',
  Maria:  '1nOmeWCtwUYqwtgaeDs0WtUHJHLpt3gbGXnOaOxyJrd4',
  Sonia:  '1W30yQ52ABBgR1JOxMYARRZslzd7wC248MzpgO48ElCU',
  Sara:   '1Ny9vwbGJwn_FIF6aLbnZKXbYQWjgqO9iQZbhvy_shrA'
};

const TEMPLATE_TAB_NAME = 'PLANTILLA';
const HEADERS = ['DATA', 'FACTURA', 'DESCRIPCIÓ', 'PREU S/IVA', '5%'];
const DATA_START_ROW = 3;

// Format de data del full.
//
// El valor que s'escriu a la columna A és una data de veritat, no
// text. Sense forçar el format, el full la dibuixa segons la seva
// configuració regional: als fulls creats amb la d'Estats Units,
// el 8 de setembre sortia com a 9/8/2026.
const DATE_FORMAT = 'dd/mm/yyyy';

const MONEY_FORMAT = '#,##0.00 €';

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
    let rawContent = '';

    if (e && e.postData && e.postData.contents) {
      rawContent = e.postData.contents;
    } else if (e && e.parameter && e.parameter.payload) {
      rawContent = e.parameter.payload;
    } else {
      throw new Error('No s’ha rebut cap contingut.');
    }

    const payload = JSON.parse(rawContent);
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

    writeTemplateHeader(monthSheet, person, month);

    const invoiceNum = (payload.invoice && payload.invoice.invoice_number) ? payload.invoice.invoice_number : '';

    let halvedRows = 0;

    const values = rows.map(r => {
      const descripcio = String(r.articulo || '').trim();
      let preu = parseMoney(r.precio);

      // La web envia "meitat" amb la decisió que es veu a la
      // pantalla (inclosos els canvis fets a mà). Si no hi és,
      // s'aplica la regla automàtica.
      const meitat = typeof r.meitat === 'boolean'
        ? r.meitat
        : isHalfPriceArticle(descripcio);

      if (meitat && typeof preu === 'number') {
        preu = Math.round((preu / 2 + Number.EPSILON) * 100) / 100;
        halvedRows++;
      }

      // El 5% es calcula sobre el preu final de la columna D,
      // ja dividit si toca.
      return [
        normalizeDateForSheet(r.fecha || ''),
        r.factura || invoiceNum,
        descripcio,
        preu,
        calculateCommission5(preu)
      ];
    });

    // Troba la primera fila realment buida a la columna A
    const nextRow = getNextAvailableRow(monthSheet);

    monthSheet.getRange(nextRow, 1, values.length, HEADERS.length).setValues(values);

    // Data en format dia/mes/any, independentment de la
    // configuració regional del full.
    monthSheet.getRange(nextRow, 1, values.length, 1).setNumberFormat(DATE_FORMAT);

    monthSheet.getRange(nextRow, 4, values.length, 2).setNumberFormat(MONEY_FORMAT);

    const invoice = payload.invoice || {};
    monthSheet.getRange('A1').setNote(
      'Veterinari: ' + person +
      '\nMes: ' + month +
      '\nFactura(es): ' + (invoice.invoice_number || '') +
      '\nData factura: ' + (invoice.invoice_date || '') +
      '\nExportades en aquesta tanda: ' + values.length +
      '\nÚltima exportació: ' + new Date().toLocaleString()
    );

    return jsonResponse({
      ok: true,
      person: person,
      month: month,
      sheet_name: monthSheet.getName(),
      exported_rows: values.length,
      halved_rows: halvedRows
    });

  } catch (err) {
    return jsonResponse({
      ok: false,
      error: String(err && err.message ? err.message : err)
    });
  }
}

/*
 * Línies que van a la columna D dividides entre 2.
 *
 * Les factures d'AniCura arriben en castellà
 * ("HOSPITALIZACION ESTANDAR HASTA 24 HORAS GATO",
 * "HOSPITALIZACION ALIMENTACION ESTANDAR",
 * "ADMINISTRACION MEDICACIÓN"), per això es mira el castellà
 * i el català, sense accents ni majúscules.
 *
 * Ha de ser la mateixa regla que isHalfPriceArticle() de
 * index.html.
 */
function isHalfPriceArticle(text) {
  const t = String(text || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/\s+/g, ' ');

  const hospital24h =
    /hospitali(z|tz)acio/.test(t) &&
    /24\s*(h\b|hrs?\b|hores|horas)/.test(t);

  return hospital24h ||
    t.includes('medicacio') ||
    t.includes('alimentacio');
}

function validateMonth(month) {
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
    sheet = template.copyTo(ss).setName(monthName);
    return sheet;
  }

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

// Cerca la primera fila de la Columna A (Data) que estigui buida a partir de DATA_START_ROW
function getNextAvailableRow(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < DATA_START_ROW) return DATA_START_ROW;

  const dates = sheet.getRange(DATA_START_ROW, 1, lastRow - DATA_START_ROW + 1, 1).getValues();
  for (let i = 0; i < dates.length; i++) {
    if (dates[i][0] === '' || dates[i][0] === null || dates[i][0] === undefined) {
      return DATA_START_ROW + i;
    }
  }

  return lastRow + 1;
}

function writeTemplateHeader(sheet, person, month) {
  if (!sheet.getRange('A1').getValue()) {
    sheet.getRange('A1').setValue(
      'COMISSIONS  ' + monthToSheetName(month).toUpperCase() +
      '  -- VETERINARI: ' + person.toUpperCase()
    );
  }
  if (!sheet.getRange('A2').getValue()) {
    sheet.getRange(2, 1, 1, HEADERS.length).setValues([HEADERS]);
  }
}

function buildFallbackTemplate(sheet, person, month) {
  sheet.getRange('A1:E1').merge();
  sheet.getRange('A1').setValue(
    'COMISSIONS  ' + monthToSheetName(month).toUpperCase() +
    '  -- VETERINARI: ' + person.toUpperCase()
  );

  sheet.getRange(2, 1, 1, HEADERS.length).setValues([HEADERS]);

  sheet.getRange('A1:E1').setFontWeight('bold').setHorizontalAlignment('center');
  sheet.getRange('A2:E2').setFontWeight('bold').setHorizontalAlignment('center');
  sheet.setColumnWidth(1, 110);
  sheet.setColumnWidth(2, 170);
  sheet.setColumnWidth(3, 420);
  sheet.setColumnWidth(4, 130);
  sheet.setColumnWidth(5, 90);
}

function parseMoney(value) {
  if (value === null || value === undefined || value === '') return '';

  let s = String(value)
    .replace(/\u00a0/g, ' ')
    .replace(/€/g, '')
    .trim();

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

  // Dia/mes/any, que és com arriben les dates de la factura.
  let m = s.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{4})$/);
  if (m) {
    const d = Number(m[1]);
    const mo = Number(m[2]);
    const y = Number(m[3]);
    return new Date(y, mo - 1, d);
  }

  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) {
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }

  return s;
}

/*
 * Arregla les dates ja exportades d'una pestanya.
 *
 * Executa-la un cop des de l'editor si vols que les files que ja
 * havies enviat també es vegin en dia/mes/any. Canvia el nom del
 * veterinari i el de la pestanya.
 */
function repararFormatDates() {

  const person = 'Roser';
  const sheetName = 'Setembre 2026';

  const sheet = SpreadsheetApp
    .openById(SHEETS[person])
    .getSheetByName(sheetName);

  if (!sheet) {
    throw new Error('No existeix la pestanya ' + sheetName);
  }

  const lastRow = sheet.getLastRow();

  if (lastRow < DATA_START_ROW) return;

  sheet
    .getRange(DATA_START_ROW, 1, lastRow - DATA_START_ROW + 1, 1)
    .setNumberFormat(DATE_FORMAT);
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
