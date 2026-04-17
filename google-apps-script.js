/**
 * Email Bot — Google Apps Script
 * ==============================
 * Paste this into your Google Sheet's Apps Script editor
 * (Extensions → Apps Script) and deploy as a Web App.
 *
 * Handles two endpoints:
 *  - POST: append a new row from the Flask bot
 *  - GET:  return all rows as JSON for the dashboard
 *
 * Deploy → New deployment → Web app
 *   Execute as: Me
 *   Who has access: Anyone
 *
 * EXPECTED SHEET COLUMNS (row 1):
 *   Timestamp | ContactId | From | Subject | Body | Reply | Status | Reason
 */

// ─── Append a new row from the bot (POST) ───────────────────
function doPost(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    var cleaned = e.postData.contents.replace(/[\n\r\t]/g, ' ');
    data = JSON.parse(cleaned);
  }
  sheet.appendRow([
    data.timestamp || '',
    data.contactId || '',
    data.from || '',
    data.subject || '',
    (data.body || '').substring(0, 5000),
    (data.reply || '').substring(0, 5000),
    data.status || '',
    data.reason || ''
  ]);
  return ContentService
    .createTextOutput(JSON.stringify({ok: true}))
    .setMimeType(ContentService.MimeType.JSON);
}


// ─── Return all rows as JSON for the dashboard (GET) ────────
function doGet(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var values = sheet.getDataRange().getValues();

  // Row 0 = headers, skip it
  var logs = [];
  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    // Skip blank rows
    if (!row[0] && !row[1] && !row[2]) continue;

    logs.push({
      timestamp: formatCell(row[0]),
      contactId: String(row[1] || ''),
      from:      String(row[2] || ''),
      subject:   String(row[3] || ''),
      body:      String(row[4] || ''),
      reply:     String(row[5] || ''),
      status:    String(row[6] || ''),
      reason:    String(row[7] || '')
    });
  }

  return ContentService
    .createTextOutput(JSON.stringify({logs: logs, count: logs.length}))
    .setMimeType(ContentService.MimeType.JSON);
}


// Convert Date objects to ISO strings, pass through other values
function formatCell(v) {
  if (v instanceof Date) {
    // Format as YYYY-MM-DD HH:mm:ss (matches what the bot sends)
    var pad = function(n) { return n < 10 ? '0' + n : n; };
    return v.getFullYear() + '-' +
      pad(v.getMonth() + 1) + '-' +
      pad(v.getDate()) + ' ' +
      pad(v.getHours()) + ':' +
      pad(v.getMinutes()) + ':' +
      pad(v.getSeconds());
  }
  return String(v || '');
}
