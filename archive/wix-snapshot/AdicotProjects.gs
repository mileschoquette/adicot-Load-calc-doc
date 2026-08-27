// =============================================================================
// ADICOT PROJECTS — Google Apps Script
// =============================================================================
// Intake-to-proposal pipeline:
//   Gmail label -> Claude extraction -> Google Sheet ("Projects" tab) + Drive folder
//   -> static admin review notification email (button to /job/<id>/star on Render)
//   -> review/edit/approve, client answers, and client sign-off all happen
//      entirely in the Flask app (job_star_save(), then /portal/<token> in
//      job_lifecycle.py), writing straight to the Sheet via _cms.update_project().
//      Nothing posts back to this script for any of that anymore.
//
// NOTE: Review no longer happens on a Wix-hosted page. That page (and the
// Velo backend it POSTed to) has been retired; nothing in this file links to
// it anymore.
// =============================================================================


// ── CONFIGURATION ─────────────────────────────────────────────────────────────

const SHEET_ID      = "1ZSWc4CL5UVtPqB74hIiTSgug2ld7dqeViWuXvad668g"; // "Website Database" — must match Render's GOOGLE_SHEETS_SPREADSHEET_ID
const TAB_NAME      = "Adicot Projects";
const ADMIN_EMAIL   = "admin@adicot.com";
const REVIEW_EMAIL  = "agc@adicot.com";
const SLACK_WEBHOOK = "REDACTED_SLACK_WEBHOOK"; // rotate the real value in Slack, then set it directly in the Apps Script editor — not committed here

// Base URL for the native Flask client portal (replaces the Wix magic-link portal).
const PORTAL_BASE_URL = "https://adicot-load-calc-doc.onrender.com";
// Shared HMAC secret for portal magic-link tokens — same value must be set as the
// Flask app's PORTAL_TOKEN_SECRET env var. See portal_tokens.py for the algorithm
// (this file's _makePortalToken() below is the JS-side equivalent).
const PORTAL_TOKEN_SECRET = PropertiesService.getScriptProperties().getProperty('PORTAL_TOKEN_SECRET');

// ── ASHRAE CLIMATIC DESIGN CONDITIONS (2025) ────────────────────────────────
// Address -> Maps geocode (built-in, no key) -> nearest WMO station -> 2025 IP
// design conditions. Returns a small object or null. Used by notifyProjectsSheet().
const ASHRAE_BASE    = 'https://ashrae-meteo.info/v3.0';
const ASHRAE_VERSION = '2025';      // 2009/2013/2017/2021/2025 (no 2024)
const ASHRAE_UNITS   = 'IP';        // 'IP' (°F) or 'SI' (°C)

function getWeatherStationData(address) {
  if (!address) return null;
  try {
    var geo = _ashraeGeocode(address);
    if (!geo) { _logToSheet('weather: could not geocode ' + address); return null; }
    var station = _ashraeNearestStation(geo.lat, geo.lng);
    if (!station) { _logToSheet('weather: no station near ' + address); return null; }
    var s = _ashraeConditions(station.wmo);
    if (!s) { _logToSheet('weather: no conditions for WMO ' + station.wmo); return null; }
    return {
      station:             s.place || station.place,
      wmo:                 station.wmo,
      lat:                 s.lat  || String(geo.lat),
      elev:                s.elev || '',
      edition:             ASHRAE_VERSION,
      units:               ASHRAE_UNITS,
      heatingDB99:         s['heating_DB_99']          || '',
      coolingDB1:          s['cooling_DB_MCWB_1_DB']   || '',
      coolingMCWB1:        s['cooling_DB_MCWB_1_MCWB'] || '',
      hottestMonth:        s['hottest_month']          || '',
      hottestMonthDBRange: s['hottest_month_DB_range'] || '',
    };
  } catch (err) {
    _logToSheet('getWeatherStationData ERROR: ' + err.message);
    return null;
  }
}

function _ashraeGeocode(address) {
  var res = Maps.newGeocoder().geocode(address);
  if (!res || res.status !== 'OK' || !res.results.length) return null;
  var loc = res.results[0].geometry.location;
  return { lat: loc.lat, lng: loc.lng };
}

function _ashraeNearestStation(lat, lng) {
  var json = _ashraePost(ASHRAE_BASE + '/request_places.php', {
    lat: String(lat), long: String(lng), number: '10', ashrae_version: ASHRAE_VERSION
  });
  var list = (json && json.meteo_stations) || [];
  return list.length ? { wmo: list[0].wmo, place: list[0].place } : null;  // nearest first
}

function _ashraeConditions(wmo) {
  var json = _ashraePost(ASHRAE_BASE + '/request_meteo_parametres.php', {
    wmo: wmo, ashrae_version: ASHRAE_VERSION, si_ip: ASHRAE_UNITS
  });
  return (json && json.meteo_stations) ? json.meteo_stations[0] : null;
}

function _ashraePost(url, payload) {
  var resp = UrlFetchApp.fetch(url, {
    method: 'post', payload: payload, muteHttpExceptions: true,
    headers: { 'X-Requested-With': 'XMLHttpRequest', 'Referer': ASHRAE_BASE + '/',
               'User-Agent': 'Mozilla/5.0' }
  });
  if (resp.getResponseCode() !== 200) return null;
  return JSON.parse(resp.getContentText().replace(/^﻿/, ''));   // strip UTF-8 BOM
}

// Manual test — run from the editor, read Logs.
function testWeatherStation() {
  Logger.log(JSON.stringify(getWeatherStationData('15825 Green Acres Ave, Wildwood, FL'), null, 2));
}

const INTAKE_LABEL    = "Projects/x-Estimate/Intake";
const PROCESSED_LABEL = "Projects/x-Estimate/PSR Ready";
const PROJECT_LABEL_PREFIX  = 'Projects/x-Estimate/';   // before client signs

function _projectLabelName(data) {
  var raw = String(data && (data.projectFolder || data.projectName || data.jobNo) || '').trim();
  return raw ? PROJECT_LABEL_PREFIX + raw : '';
}

function _getOrCreateProjectLabel(data) {
  var name = _projectLabelName(data);
  if (!name) return null;
  var label = GmailApp.getUserLabelByName(name);
  if (!label) { label = GmailApp.createLabel(name); _logToSheet('Project label created: ' + name); }
  return label;
}

function _applyProjectLabel(thread, data, where) {
  try {
    var label = _getOrCreateProjectLabel(data);
    if (label && thread) { thread.addLabel(label); _logToSheet('Project label "' + label.getName() + '" applied to ' + (where || 'thread')); }
  } catch (err) { _logToSheet('_applyProjectLabel error (' + (where || '') + '): ' + err.message); }
}
const MODEL_HAIKU  = "claude-haiku-4-5-20251001";
const MODEL_SONNET = "claude-sonnet-4-6";

const MAX_PDF_BYTES = 20 * 1024 * 1024;

// ── Adicot shared drive root ──────────────────────────────
const ADICOT_DRIVE_ID = '0ACMGIQqrx5HoUk9PVA';
const JOB_FOLDER_NAME = '1-job';

const COL = {
  DATE:               1,
  QUOTE_TO:           2,
  PROJECT_NAME:       3,
  PROPERTY_OWNER:     4,
  WARNING:            5,
  PROJECT_ADDRESS:    6,
  TOTAL_COST:         7,
  SF:                 8,
  SF_PER_DOLLAR:      9,
  PRODUCT_SERVICE:   10,
  STATUS_HEADER:     11,
  STATUS:            12,
  DATE_OPTIONAL:     13,
  NOTE:              14,
  PROPOSAL_LINK:     15,
  OCCUPANCY:         16,
  INSURANCE:         17,
  JOB_NO:            18,
  DESCRIPTION:       19,
  EDIT_LINK:         20,
  GENERATED_LINK:    21,
  TOTAL_PAY:         23,
  STATE:             24,
  COUNTY:            25,
  DATE_RECEIVED:     26,
  FORM_VERSION:      27,
  BUILDING_STATUS:   28,
  ORIENTATION:       29,
  OCCUPANTS:         30,
  ROOF_DECK_TYPE:    31,
  ROOF_INSUL_POS:    32,
  ROOF_SUSP_CEIL:    33,
  ROOF_R_VALUE:      34,
  ROOF_COLOR:        35,
  CEIL_HEIGHT:       36,
  WALL_FINISH:       37,
  WALL_CONSTRUCTION: 38,
  WALL_COLOR:        39,
  WALL_R_VALUE:      40,
  WALL_HEIGHT:       41,
  GLASS_FIXED_U:     42,
  GLASS_FIXED_SHGC:  43,
  GLASS_OPER_U:      44,
  GLASS_OPER_SHGC:   45,
  DOOR_TYPE:         46,
  LIGHTING_OCC:      47,
  LIGHTING_WPF:      48,
  HEAT_GEN_EQUIP:    49,
  AC_NEW_EXISTING:   50,
  AC_MOUNTING:       51,
  PROJECT_NOTES:     52,
  DRIVE_FOLDER:      53,
};
// NOTE: The Sheet has no columns for roofCover, atticCond, or engagementDays —
// a holdover from when those fields lived in the Wix CMS only. Never overwrite
// ROOF_COLOR with a roof-covering value (the old collision bug).
//
// The COL map and TAB_NAME ("Adicot Projects") above are the LEGACY partial
// mirror tab. The only thing left that still uses them is appendProjectRow(),
// itself already dead (see its own header comment — "legacy, no longer
// called"). handleClientSigned/handleClientAnswers, which used to write into
// this legacy tab (a real bug — they never touched the tab sheets_client.py
// reads), were confirmed dead and removed: client signing/answering now
// happens entirely in Flask's /portal/<token> route (job_lifecycle.py),
// which writes straight to PROJECTS_TAB_NAME via _cms.update_project().
// notifyProjectsSheet() below also writes to that same new tab (see
// PROJECTS_TAB_NAME / SHEET_COLUMNS just below), using the exact same column
// order as sheets_client.py's SHEET_COLUMNS in the Python app.

// Dedicated tab for the new, clean schema — matches sheets_client.py's
// GOOGLE_SHEETS_WORKSHEET_NAME default ("Projects"). Created automatically
// (with a header row) the first time notifyProjectsSheet() runs if it doesn't
// exist yet.
const PROJECTS_TAB_NAME = "Projects";

// Exact same order as sheets_client.py's SHEET_COLUMNS — keep the two in sync
// by hand; there is no shared source file between this repo's Python and this
// standalone Apps Script project.
const SHEET_COLUMNS = [
  "_id", "legacy_wix_id", "createdDate", "status", "workOrderComplete",
  "proposalSigned", "reviewComplete", "signedDate", "signedBy", "signedTitle",
  "gcAccepted", "totalCost", "jobNo", "title", "projectAddress",
  "propertyOwner", "owner", "clientName", "clientCompany", "clientEmail",
  "clientPhone", "productService", "clientCode", "subClient", "community",
  "subdivision", "locationDisambig", "lennarJobNo", "engagementDays",
  "buildingStatus", "sf", "occupants", "orientation", "indoorTemp",
  "indoorRH", "weatherData", "deckType", "roofCover", "roofColor",
  "roofRValue", "insulPosition", "suspCeiling", "atticCond", "ceilingHeight",
  "wallFinish", "wallConstruction", "wallColor", "wallRValue", "wallHeight",
  "partConstruction", "partRValue", "floorType", "floorRValue", "glassU",
  "glassSHGC", "glassOperU", "glassOperSHGC", "glassSGDU", "glassSGDSHGC",
  "glassFrame", "glazingType", "glazingTint", "skylights", "doorType",
  "occupancyType", "lpdSpaceType", "lightingWattsPerSF", "equipWattsPerSF",
  "heatGenEquipment", "infiltration", "changeRate", "acNewExisting",
  "acMounting", "systemType", "hvacType", "heatType", "coolingEff",
  "heatingEff", "efficiencyTier", "manufacturer", "hasOutsideAir",
  "hasExhaust", "hasStrip", "heatStripCOP", "hwType", "hwEfficiency",
  "hwCapacityGal", "description", "projectFolder", "driveFolderUrl",
  "driveFolderId", "snippetRoofRValue", "snippetWallConstruction",
  "snippetGlassValues", "snippetCeilingHeight", "snippetLightingWsf",
  "snippetProjectAddress",
  "projectCity", "projectState", "projectZip", "projectCounty",
  "latitude", "elevation", "numStories",
  "extLightDescription", "extLightCategory", "extLightNumLuminaires",
  "extLightWattsPerLuminaire", "extLightAreaLengthUnits", "extLightControlType",
  "osaLowDry", "osaDailyRange",
];

function _getProjectsSheet() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var sheet = ss.getSheetByName(PROJECTS_TAB_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(PROJECTS_TAB_NAME);
    sheet.getRange(1, 1, 1, SHEET_COLUMNS.length).setValues([SHEET_COLUMNS]);
  }
  return sheet;
}

// ── PORTAL MAGIC-LINK TOKENS ───────────────────────────────────────────────────
// JS-side mirror of portal_tokens.py's algorithm — see that file's docstring for
// why this is a hand-rolled HMAC scheme rather than itsdangerous (a Python-only
// format Apps Script can't mint). Token = base64url(payload) + "." +
// base64url(HMAC-SHA256(secret, base64url(payload))), payload = "id.expiryUnixTs".

function _b64urlEncodeBytes(bytes) {
  return Utilities.base64EncodeWebSafe(bytes).replace(/=+$/, '');
}

function _makePortalToken(id, daysValid) {
  var expiryTs = Math.floor(Date.now() / 1000) + (daysValid || 180) * 86400;
  var payload = id + '.' + expiryTs;
  var payloadB64 = _b64urlEncodeBytes(Utilities.newBlob(payload).getBytes());
  var sigBytes = Utilities.computeHmacSha256Signature(payloadB64, PORTAL_TOKEN_SECRET);
  var sigB64 = _b64urlEncodeBytes(sigBytes);
  return payloadB64 + '.' + sigB64;
}

function _generateRowId() {
  // Short, reasonably-unique id — Apps Script equivalent of Python's secrets.token_hex(6).
  return Utilities.getUuid().replace(/-/g, '').slice(0, 16);
}


// ── LPD LOOKUP — 2024 IECC C405.3.2(1) ───────────────────────────────────────

const LPD_2024_GS = {
  'Automotive facility':        0.73,
  'Convention center':          0.64,
  'Courthouse':                 0.75,
  'Dining: bar lounge/leisure': 0.74,
  'Dining: cafeteria/fast food':0.70,
  'Dining: family':             0.65,
  'Dormitory':                  0.52,
  'Exercise center':            0.72,
  'Fire station':               0.56,
  'Gymnasium':                  0.75,
  'Health care clinic':         0.77,
  'Hospital':                   0.92,
  'Hotel/Motel':                0.53,
  'Library':                    0.83,
  'Manufacturing facility':     0.82,
  'Motion picture theater':     0.43,
  'Multiple-family':            0.46,
  'Museum':                     0.56,
  'Office':                     0.62,
  'Parking garage':             0.17,
  'Penitentiary':               0.65,
  'Performing arts theater':    0.82,
  'Police station':             0.62,
  'Post office':                0.64,
  'Religious building':         0.66,
  'Retail':                     0.78,
  'School/university':          0.70,
  'Sports arena':               0.73,
  'Town hall':                  0.67,
  'Transportation':             0.56,
  'Warehouse':                  0.45,
  'Workshop':                   0.86,
};

function _getLpdSpaceType(occupancyType) {
  var occ = (occupancyType || '').toLowerCase();
  if (occ.includes('medical') || occ.includes('outpatient') || occ.includes('clinic') || occ.includes('dental')) return 'Health care clinic';
  if (occ.includes('hospital')) return 'Hospital';
  if (occ.includes('bar') || occ.includes('lounge')) return 'Dining: bar lounge/leisure';
  if (occ.includes('cafeteria') || occ.includes('fast food')) return 'Dining: cafeteria/fast food';
  if (occ.includes('restaurant') || occ.includes('dining') || occ.includes('food service')) return 'Dining: family';
  if (occ.includes('office')) return 'Office';
  if (occ.includes('retail')) return 'Retail';
  if (occ.includes('multifamily') || occ.includes('apartment') || occ.includes('condo') || occ.includes('residential')) return 'Multiple-family';
  if (occ.includes('church') || occ.includes('worship') || occ.includes('religious') || occ.includes('assembly')) return 'Religious building';
  if (occ.includes('school') || occ.includes('university')) return 'School/university';
  if (occ.includes('gymnasium') || occ.includes('gym')) return 'Gymnasium';
  if (occ.includes('exercise') || occ.includes('fitness')) return 'Exercise center';
  if (occ.includes('warehouse')) return 'Warehouse';
  if (occ.includes('manufactur')) return 'Manufacturing facility';
  if (occ.includes('library')) return 'Library';
  if (occ.includes('hotel') || occ.includes('motel')) return 'Hotel/Motel';
  return null;
}


// ── PROJECT NAMING CONVENTION ─────────────────────────────────────────────────

function buildProjectFolderName(clientCode, subClient, locationDisambig) {
  if (!clientCode) return '';
  var name = clientCode.trim();

  if (name === 'Crown') {
    if (locationDisambig && locationDisambig.trim()) name += '-' + locationDisambig.trim();
    if (subClient && subClient.trim()) name += '-' + subClient.trim();
    return name;
  }
  if (subClient && subClient.trim()) name += '-' + subClient.trim();
  if (locationDisambig && locationDisambig.trim()) {
    var loc = locationDisambig.trim();
    name += (loc.charAt(0) === '(') ? ' ' + loc : '-' + loc;
  }
  return name;
}

function _deriveSubClient(projectName) {
  if (!projectName) return '';
  var pn = String(projectName).trim();
  var paren = pn.match(/\(([^)]+)\)/);
  if (paren && paren[1].trim()) return paren[1].trim();
  return pn;
}


// ── DRIVE FOLDER MANAGEMENT ───────────────────────────────────────────────────

function getOrCreateJobFolder() {
  const q = "name = '" + JOB_FOLDER_NAME + "'"
          + " and mimeType = 'application/vnd.google-apps.folder'"
          + " and '" + ADICOT_DRIVE_ID + "' in parents"
          + " and trashed = false";

  const found = Drive.Files.list({
    q: q,
    corpora: 'drive',
    driveId: ADICOT_DRIVE_ID,
    includeItemsFromAllDrives: true,
    supportsAllDrives: true,
    fields: 'files(id,name,webViewLink)'
  });

  if (found.files && found.files.length) {
    const f = found.files[0];
    Logger.log('1-job exists: %s', f.id);
    return { id: f.id, url: f.webViewLink };
  }

  const created = Drive.Files.create({
    name: JOB_FOLDER_NAME,
    mimeType: 'application/vnd.google-apps.folder',
    parents: [ADICOT_DRIVE_ID]
  }, null, {
    supportsAllDrives: true,
    fields: 'id,name,webViewLink'
  });

  Logger.log('1-job created: %s', created.id);
  return { id: created.id, url: created.webViewLink };
}

function setupJobFolder() {
  const job = getOrCreateJobFolder();
  const props = PropertiesService.getScriptProperties();
  props.setProperty('JOB_FOLDER_ID', job.id);
  props.setProperty('JOB_FOLDER_URL', job.url);
  Logger.log('Stored JOB_FOLDER_ID=%s  JOB_FOLDER_URL=%s', job.id, job.url);
  return job;
}

const PROJECT_SUBFOLDERS = ['1-From Client', '2-Equipment', '3-Load', '4-Design', '5-Energy', '6-Submit'];

function _findOrCreateFolder(name, parentId) {
  const q = "name = '" + name.replace(/'/g, "\\'") + "'"
          + " and mimeType = 'application/vnd.google-apps.folder'"
          + " and '" + parentId + "' in parents"
          + " and trashed = false";
  const found = Drive.Files.list({
    q: q, corpora: 'drive', driveId: ADICOT_DRIVE_ID,
    includeItemsFromAllDrives: true, supportsAllDrives: true,
    fields: 'files(id,name,webViewLink)'
  });
  if (found.files && found.files.length) {
    return { id: found.files[0].id, url: found.files[0].webViewLink };
  }
  const created = Drive.Files.create({
    name: name, mimeType: 'application/vnd.google-apps.folder', parents: [parentId]
  }, null, { supportsAllDrives: true, fields: 'id,name,webViewLink' });
  return { id: created.id, url: created.webViewLink };
}

function createProjectFolder(clientCode, subClient, locationDisambig) {
  const folderName = buildProjectFolderName(clientCode, subClient, locationDisambig);
  if (!folderName) throw new Error('No clientCode — cannot build folder name');

  const jobFolderId = PropertiesService.getScriptProperties().getProperty('JOB_FOLDER_ID');
  if (!jobFolderId) throw new Error('JOB_FOLDER_ID not set — run setupJobFolder() first');

  const clientFolder = _findOrCreateFolder(clientCode.trim(), jobFolderId);
  const projectFolder = _findOrCreateFolder(folderName, clientFolder.id);

  for (var i = 0; i < PROJECT_SUBFOLDERS.length; i++) {
    _findOrCreateFolder(PROJECT_SUBFOLDERS[i], projectFolder.id);
  }

  return { id: projectFolder.id, url: projectFolder.url };
}


// ── ADMIN REVIEW NOTIFICATION EMAIL ──────────────────────────────────────────
// Static HTML notification. The actual review/editing happens on the hosted
// page; this email just notifies and links to it (CMS = single source of truth).

function _sendAdminReviewEmail(data, projectId) {
  var jobNo   = data.jobNo || '';
  var subject = '✉️ Review: ' + jobNo + ' · ' + (data.projectFolder || data.projectName || 'New Project');
  // Points at the Flask app's own work-order tab (Render), not the retired Wix
  // admin-review page — projectId here is the Sheets row _id notifyProjectsSheet()
  // minted, which /job/<id>/star already knows how to load via sheets_client.
  var reviewUrl = PORTAL_BASE_URL + '/job/' + encodeURIComponent(projectId || '') + '/star';

  var html  = _buildAdminNotificationHtml(data, reviewUrl);
  var plain = _adminNotifyPlain(data, reviewUrl);

  GmailApp.sendEmail(REVIEW_EMAIL, subject, plain, {
    htmlBody: html,
    name:     'Adicot Intake Pipeline',
  });
  _logToSheet('Admin review notification email sent for ' + jobNo + ' to ' + REVIEW_EMAIL);
  // Group the review notification we just sent under the project label.
  try {
    Utilities.sleep(1000);  // let the sent copy index before we search
    var _rev = GmailApp.search('in:sent newer_than:1d subject:"' + jobNo + '"', 0, 1);
    if (_rev.length) _applyProjectLabel(_rev[0], data, 'admin review notification');
  } catch (e) { _logToSheet('label admin notification error: ' + e.message); }
}

function _adminNotifyPlain(data, reviewUrl) {
  return [
    'ADICOT — INTAKE READY FOR REVIEW',
    (data.jobNo || '') + ' · ' + (data.projectFolder || data.projectName || ''),
    (data.clientName || '') + (data.clientCompany ? ' · ' + data.clientCompany : ''),
    '',
    'Area: ' + (data.sf ? data.sf + ' SF' : '—'),
    'Service: ' + (data.productService || '—'),
    '',
    'Open the review page to edit fields, set pricing, and approve:',
    reviewUrl,
  ].join('\n');
}

function _buildAdminNotificationHtml(data, reviewUrl) {
  var e = function(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); };
  var jobNo   = data.jobNo || '';
  var title   = data.projectFolder || data.projectName || 'New Project';
  var newFlag = data.newClientFlag
    ? '<tr><td style="padding:0 24px 4px"><span style="font-size:11px;background:#FEF9C3;color:#854D0E;padding:4px 10px;border-radius:20px;font-weight:600">✨ New client added: ' + e(data.newClientFlag) + ' — verify code &amp; aliases in CMS</span></td></tr>'
    : '';

  var confirmedKeys = ['projectAddress','sf','occupants','occupancyType','buildingStatus','roofRValue','wallConstruction','glassU','glassSHGC','ceilingHeight','heatGenEquipment'];
  var missingKeys   = ['deckType','roofCover','insulPosition','suspCeiling','atticCond','doorType'];
  var confirmed = confirmedKeys.filter(function(k){ return data[k] && data[k] !== 0; }).length;
  var missing   = missingKeys.filter(function(k){ return !data[k]; }).length;

  return '<!DOCTYPE html><html><head><meta charset="UTF-8"></head>' +
  '<body style="margin:0;padding:0;background:#FAFAF7;font-family:Arial,Helvetica,sans-serif">' +
  '<table width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF7;padding:28px 12px"><tr><td align="center">' +
  '<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;max-width:560px;border:1px solid #D4D0C2">' +

  '<tr><td style="background:#2C2C2A;padding:18px 24px">' +
  '<p style="margin:0;font-size:10px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:#E8740A">Adicot — intake review</p>' +
  '<p style="margin:6px 0 0;font-size:16px;color:#fff;font-weight:400">' + e(jobNo) + ' &nbsp;&middot;&nbsp; ' + e(title) + '</p>' +
  '<p style="margin:3px 0 0;font-size:12px;color:#D4D0C2">' + e(data.clientName||'') + (data.clientCompany?' &middot; '+e(data.clientCompany):'') + '</p>' +
  '</td></tr>' +

  newFlag +

  '<tr><td style="padding:22px 24px 6px">' +
  '<p style="margin:0 0 14px;font-size:14px;color:#444441;line-height:1.6">A new intake has been extracted and saved. Open the review page to edit any field, set services &amp; pricing, and approve.</p>' +

  '<table width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF7;border:1px solid #EDE8E1;border-radius:8px;margin-bottom:18px">' +
  '<tr><td style="padding:11px 16px;border-bottom:1px solid #EDE8E1"><span style="font-size:11px;color:#9A9A9A;text-transform:uppercase;letter-spacing:.06em">Area</span><br><span style="font-size:14px;color:#2C2C2A;font-weight:500">' + (data.sf ? e(data.sf)+' SF' : '—') + '</span></td></tr>' +
  '<tr><td style="padding:11px 16px"><span style="font-size:11px;color:#9A9A9A;text-transform:uppercase;letter-spacing:.06em">Service</span><br><span style="font-size:14px;color:#2C2C2A;font-weight:500">' + e(data.productService||'—') + '</span></td></tr>' +
  '</table>' +

  '<p style="margin:0 0 18px;font-size:12px;color:#9A9A9A">' +
  '<span style="display:inline-block;background:#E6F2E6;color:#2D6A2D;padding:2px 9px;border-radius:20px;font-weight:600;margin-right:6px">' + confirmed + ' confirmed</span>' +
  '<span style="display:inline-block;background:#FDF0E4;color:#C05C0A;padding:2px 9px;border-radius:20px;font-weight:600">' + missing + ' going to client</span>' +
  '</p>' +
  '</td></tr>' +

  '<tr><td style="padding:0 24px 24px">' +
  '<a href="' + reviewUrl + '" style="display:inline-block;background:#E8740A;color:#fff;text-decoration:none;font-size:14px;font-weight:600;padding:13px 28px;border-radius:8px">Open Review Page &rarr;</a>' +
  '<p style="margin:12px 0 0;font-size:11px;color:#9A9A9A;line-height:1.6">Editing and approval happen on the page. Approving there creates a Gmail draft — nothing sends automatically.</p>' +
  '</td></tr>' +

  '<tr><td style="padding:14px 24px;border-top:1px solid #EDE8E1;background:#FAFAF7">' +
  '<p style="margin:0;font-size:11px;color:#9A9A9A;line-height:1.6">Adicot Intake Pipeline &nbsp;&middot;&nbsp; ' + e(jobNo) + ' &nbsp;&middot;&nbsp; ' + e(data.dateReceived||'') + '</p>' +
  '</td></tr>' +

  '</table></td></tr></table></body></html>';
}


// ── REQUEST ROUTER ────────────────────────────────────────────────────────────

// No actions left to dispatch — the client portal (signing, answering
// questions, saving progress) is now handled entirely by Flask's
// /portal/<token> route, writing straight to the Sheet via _cms.update_project().
// This endpoint is kept only so the deployed web app doesn't error on a stray
// POST; nothing in this codebase sends one anymore.
function doPost(e) {
  try {
    _logToSheet('doPost called');
    var payload = JSON.parse(e.postData.contents);
    return _respond('error', 'Unknown action: ' + (payload.action || 'none') + ' — this endpoint no longer accepts actions.');
  } catch (err) {
    _logToSheet('doPost ERROR: ' + err.message);
    return _respond('error', err.message);
  }
}

// doGet retained only for health checks. Review, approval, client answers,
// and signing all happen on Render now (job_star_save() / /portal/<token>);
// this web app's POST endpoint has no live actions left (see doPost above).
function doGet(e) {
  return HtmlService.createHtmlOutput(
    '<p style="font-family:Arial;padding:32px">Adicot intake endpoint is active. Review happens at /job/&lt;id&gt;/star on Render.</p>'
  );
}


// ── STEP 2 PIPELINE: GMAIL → CLAUDE → GOOGLE SHEET ───────────────────────────

function processIntakeEmails() {
  try {
    var processedLabel = GmailApp.getUserLabelByName(PROCESSED_LABEL);
    if (!processedLabel) processedLabel = GmailApp.createLabel(PROCESSED_LABEL);
    var intakeLabel = GmailApp.getUserLabelByName(INTAKE_LABEL);
    if (!intakeLabel) {
      _logToSheet('processIntakeEmails: label "' + INTAKE_LABEL + '" not found');
      return;
    }
    var threads = intakeLabel.getThreads(0, 20);
    if (!threads.length) return;
    for (var t = 0; t < threads.length; t++) {
      if (_isNoProject(threads[t])) continue;
      try {
        _processIntakeThread(threads[t], processedLabel, intakeLabel);
      } catch (err) {
        _logToSheet('processIntakeEmails thread error: ' + err.message);
      }
    }
  } catch (err) {
    _logToSheet('processIntakeEmails ERROR: ' + err.message);
  }
}

function _processIntakeThread(thread, processedLabel, intakeLabel) {
  var messages  = thread.getMessages();
  var latest    = messages[messages.length - 1];
  var subject   = thread.getFirstMessageSubject();
  var fromEmail = latest.getFrom();
  var body      = latest.getPlainBody();
  var received  = latest.getDate();

  _logToSheet('Processing intake: ' + subject + ' from ' + fromEmail);

  var emailData = _extractWithClaude(subject, fromEmail, body) || {};
  var attachmentResults = _processAttachments(latest);
  var merged = _mergeExtractions(emailData, attachmentResults.extractions);
  var snippets = attachmentResults.snippets;

  if (!merged || Object.keys(merged).length === 0) {
    _logToSheet('No data extracted for: ' + subject);
    _swapLabel(thread, intakeLabel, processedLabel);
    postToSlack(null, [
      { type: 'header', text: { type: 'plain_text', text: '⚠️ Intake extraction failed' } },
      { type: 'section', text: { type: 'mrkdwn', text: '*Subject:* ' + subject + '\n*From:* ' + fromEmail + '\n\nCould not extract data. Review manually.' } },
    ]);
    return;
  }

  var jobNo = _generateJobNo(merged.clientCompany || merged.clientLastName || 'UNK');

  if ((!merged.subClient || !merged.subClient.trim()) && merged.clientCode !== 'Crown') {
    merged.subClient = _deriveSubClient(merged.projectName || '');
  }

  var projectFolder = buildProjectFolderName(
    merged.clientCode || '',
    merged.subClient  || '',
    merged.locationDisambig || ''
  );

  // jobNo = the project folder name ([ClientCode]-[SubClient]); fall back to
  // the initials+date code only if we couldn't build a folder name.
  if (projectFolder) jobNo = projectFolder;

  var newClientFlag = '';
  if (merged._isNewClient && merged.clientCode) {
    var added = _addClientCode(
      merged.clientCode,
      merged._proposedClientName || merged.clientCompany || merged.clientCode,
      merged._proposedAliases || merged.clientCompany || ''
    );
    if (added) {
      newClientFlag = merged.clientCode;
      _logToSheet('New client code auto-added: ' + merged.clientCode);
    }
  }

  var driveFolderId = '', driveFolderUrl = '';
  try {
    var pf = createProjectFolder(
      merged.clientCode || '',
      merged.subClient  || '',
      merged.locationDisambig || ''
    );
    driveFolderId  = pf.id;
    driveFolderUrl = pf.url;
    _logToSheet('Project folder created: ' + projectFolder + ' (' + driveFolderId + ')');
  } catch(e3) {
    _logToSheet('createProjectFolder error: ' + e3.message);
  }

  // Save the client's intake attachments into 1-From Client right away.
  if (driveFolderId) {
    try { _fileThreadClientAttachments(thread, driveFolderId); }
    catch (e) { _logToSheet('intake attach filing error: ' + e.message); }
  }

  var lightingWattsPerSF = merged.lightingWattsPerSF || null;
  var lpdSpaceType = merged.lpdSpaceType || _getLpdSpaceType(merged.occupancyType || '') || '';
  if (!lightingWattsPerSF && lpdSpaceType && LPD_2024_GS[lpdSpaceType]) {
    lightingWattsPerSF = LPD_2024_GS[lpdSpaceType];
  }

  var data = {
    jobNo:              jobNo,
    projectName:        merged.projectName     || jobNo,
    projectFolder:      projectFolder,
    clientCode:         merged.clientCode      || '',
    subClient:          merged.subClient       || '',
    locationDisambig:   merged.locationDisambig|| '',
    community:          merged.community       || '',
    subdivision:        merged.subdivision     || '',
    repeatClient:       merged.repeatClient    || false,
    lpdSpaceType:       lpdSpaceType,
    projectAddress:     merged.projectAddress  || '',
    propertyOwner:      merged.propertyOwner   || '',
    clientEmail:        merged.clientEmail     || _parseEmail(fromEmail),
    clientFirst:        merged.clientFirst     || '',
    clientLast:         merged.clientLast      || '',
    clientPhone:        merged.clientPhone     || '',
    clientCompany:      merged.clientCompany   || '',
    clientName:         ((merged.clientFirst || '') + ' ' + (merged.clientLast || '')).trim(),
    quoteTO:            merged.clientCompany   || merged.clientFirst || '',
    productService:     merged.productService  || '',
    sf:                 merged.sf              || 0,
    occupancyType:      merged.occupancyType   || '',
    buildingStatus:     merged.buildingStatus  || '',
    description:        merged.description     || body.substring(0, 500),
    projectCity:        merged.projectCity     || '',
    projectState:       merged.projectState    || '',
    projectZip:         merged.projectZip      || '',
    projectCounty:      merged.projectCounty   || '',
    numStories:         merged.numStories      || '',
    dateReceived:       Utilities.formatDate(received, Session.getScriptTimeZone(), 'M/d/yyyy'),
    status:             'Pending Review',
    roofRValue:         merged.roofRValue         || '',
    roofColor:          merged.roofColor          || '',
    roofCover:          merged.roofCover          || '',
    deckType:           merged.deckType           || '',
    insulPosition:      merged.insulPosition      || '',
    suspCeiling:        merged.suspCeiling        || '',
    atticCond:          merged.atticCond          || '',
    wallConstruction:   merged.wallConstruction   || '',
    wallFinish:         merged.wallFinish         || '',
    wallColor:          merged.wallColor          || '',
    wallRValue:         merged.wallRValue         || '',
    wallHeight:         merged.wallHeight         || '',
    partConstruction:   merged.partConstruction   || '',
    partRValue:         merged.partRValue         || '',
    floorType:          merged.floorType          || '',
    floorRValue:        merged.floorRValue        || '',
    glassU:             merged.glassU             || null,
    glassSHGC:          merged.glassSHGC          || null,
    glassOperU:         merged.glassOperU         || null,
    glassOperSHGC:      merged.glassOperSHGC      || null,
    glassSGDU:          merged.glassSGDU          || null,
    glassSGDSHGC:       merged.glassSGDSHGC       || null,
    glassFrame:         merged.glassFrame         || '',
    glazingType:        merged.glazingType        || '',
    glazingTint:        merged.glazingTint        || '',
    skylights:          merged.skylights          || '',
    doorType:           merged.doorType           || '',
    lightingWattsPerSF: lightingWattsPerSF,
    equipWattsPerSF:    merged.equipWattsPerSF    || null,
    orientation:        merged.orientation        || '',
    occupants:          merged.occupants          || null,
    ceilingHeight:      merged.ceilingHeight      || '',
    heatGenEquipment:   merged.heatGenEquipment   || '',
    infiltration:       merged.infiltration       || '',
    changeRate:         merged.changeRate         || '',
    acNewExisting:      merged.acNewExisting      || '',
    acMounting:         merged.acMounting         || '',
    systemType:         merged.systemType         || '',
    hvacType:           merged.hvacType           || '',
    heatType:           merged.heatType           || '',
    coolingEff:         merged.coolingEff         || '',
    heatingEff:         merged.heatingEff         || '',
    efficiencyTier:     merged.efficiencyTier     || '',
    manufacturer:       merged.manufacturer       || '',
    hasOutsideAir:      merged.hasOutsideAir      || '',
    hasExhaust:         merged.hasExhaust         || '',
    hasStrip:           merged.hasStrip           || '',
    heatStripCOP:       merged.heatStripCOP       || '',
    hwType:             merged.hwType             || '',
    hwEfficiency:       merged.hwEfficiency       || '',
    hwCapacityGal:      merged.hwCapacityGal      || '',
    extLightDescription:       merged.extLightDescription       || '',
    extLightCategory:          merged.extLightCategory          || '',
    extLightNumLuminaires:     merged.extLightNumLuminaires     || '',
    extLightWattsPerLuminaire: merged.extLightWattsPerLuminaire || '',
    extLightAreaLengthUnits:   merged.extLightAreaLengthUnits   || '',
    extLightControlType:       merged.extLightControlType       || '',
    osaLowDry:          merged.osaLowDry          || '',
    osaDailyRange:      merged.osaDailyRange      || '',
    indoorTemp:         merged.indoorTemp         || '75',
    indoorRH:           merged.indoorRH           || '50',
    snippetProjectAddress:   snippets.titleBlock  || '',
    snippetRoofRValue:       snippets.rcp         || snippets.energyNotes || snippets.titleBlock || '',
    snippetWallConstruction: snippets.rcp         || snippets.energyNotes || snippets.titleBlock || '',
    snippetGlassValues:      snippets.rcp         || snippets.energyNotes || snippets.titleBlock || '',
    snippetLightingWsf:      snippets.rcp         || snippets.energyNotes || snippets.titleBlock || '',
    snippetCeilingHeight:    snippets.rcp         || snippets.floorPlan   || snippets.titleBlock || '',
    driveFolderId:           driveFolderId,
    driveFolderUrl:          driveFolderUrl,
    newClientFlag:           newClientFlag,
  };

  // ── LIVE SNIPPET CROPPING ───────────────────────────────────────────────
  // Crop every located field across ALL intake PDFs, upload each to the
  // project's "1-From Client/snippets" folder, and build ONE field->url map
  // (first non-empty URL per field wins, so a real crop is never overwritten
  // by a later blank). Stored as a JSON string in data.snippetMap; the admin
  // review page reads it and shows each field's thumbnail next to that field.
  data.snippetMap = '';
  if (driveFolderId && attachmentResults.pdfSources && attachmentResults.pdfSources.length) {
    var snippetMapObj = {};
    for (var sIdx = 0; sIdx < attachmentResults.pdfSources.length; sIdx++) {
      var ps = attachmentResults.pdfSources[sIdx];
      try {
        var cropRes = _cropFieldsToSnippets(ps.pdfBytes, ps.sources, merged, driveFolderId);
        if (cropRes && cropRes.map) {
          Object.keys(cropRes.map).forEach(function(field) {
            if (!snippetMapObj[field] && cropRes.map[field]) snippetMapObj[field] = cropRes.map[field];
          });
        }
        if (cropRes && cropRes.errors && cropRes.errors.length) {
          _logToSheet('crop errors (' + ps.name + '): ' + cropRes.errors.join(' | '));
        }
      } catch (cropErr) {
        _logToSheet('_cropFieldsToSnippets error for ' + ps.name + ': ' + cropErr.message);
      }
    }
    if (Object.keys(snippetMapObj).length) {
      data.snippetMap = JSON.stringify(snippetMapObj);
      _logToSheet('snippetMap built: ' + Object.keys(snippetMapObj).length + ' fields');
    }
  }

  var sheetResult = notifyProjectsSheet({ ...data, totalCost: 0 }, null);
  var projectId = sheetResult && sheetResult.id ? sheetResult.id : '';

  _swapLabel(thread, intakeLabel, processedLabel);
  _applyProjectLabel(thread, data, 'intake thread (incoming client email)');   // ADD

  var attachCount = attachmentResults.extractions.length;
  var snippetCount = Object.values(snippets).filter(Boolean).length;

  try {
    _sendAdminReviewEmail(data, projectId);
  } catch(err) {
    _logToSheet('_sendAdminReviewEmail ERROR: ' + err.message);
  }

  postToSlack(null, [
    { type: 'header', text: { type: 'plain_text', text: '✉️ New intake — ' + jobNo } },
    { type: 'section', fields: [
      { type: 'mrkdwn', text: '*Project:*\n' + (projectFolder || data.projectName) },
      { type: 'mrkdwn', text: '*Client:*\n' + data.clientName + (data.clientCompany ? ' · ' + data.clientCompany : '') },
      { type: 'mrkdwn', text: '*Service:*\n' + (data.productService || '—') },
      { type: 'mrkdwn', text: '*Area:*\n' + (data.sf ? data.sf + ' SF' : '—') },
      { type: 'mrkdwn', text: '*Drawings:*\n' + attachCount + ' scanned · ' + snippetCount + ' snippets' },
    ]},
    { type: 'context', elements: [{ type: 'mrkdwn', text: subject + ' · ' + fromEmail }] },
  ]);

  _logToSheet('Intake processed: ' + jobNo + ' | ' + attachCount + ' attachments | projectId: ' + projectId);
}


// ── ATTACHMENT PROCESSING ─────────────────────────────────────────────────────

function _processAttachments(message) {
  var result = { extractions: [], snippets: {}, pdfSources: [] };
  var MIN_DRAWING_BYTES = 80 * 1024;
  var LOGO_NAME_PATTERN = /logo|signature|banner|letterhead|adicot_eng/i;

  var attachments = message.getAttachments({ includeInlineImages: true });
  if (!attachments || !attachments.length) return result;

  for (var i = 0; i < attachments.length; i++) {
    var att = attachments[i];
    var name     = att.getName() || 'attachment';
    var mimeType = att.getContentType() || '';
    var isPdf    = mimeType === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
    var isImage  = mimeType.startsWith('image/') || /\.(png|jpg|jpeg|gif|webp)$/i.test(name);

    if (!isPdf && !isImage) continue;

    try {
      var bytes = att.getBytes();

      if (isImage && !isPdf && bytes.length < MIN_DRAWING_BYTES) {
        _logToSheet('Skipping small inline image (likely logo): ' + name + ' (' + Math.round(bytes.length/1024) + 'KB)');
        continue;
      }
      if (LOGO_NAME_PATTERN.test(name)) {
        _logToSheet('Skipping logo-named file: ' + name);
        continue;
      }
      if (bytes.length > MAX_PDF_BYTES) {
        _logToSheet('Attachment too large, skipping: ' + name + ' (' + Math.round(bytes.length/1024/1024) + 'MB)');
        continue;
      }

      var b64       = Utilities.base64Encode(bytes);
      var mediaType = isPdf ? 'application/pdf' : mimeType;

      var extracted = _extractFromAttachment(b64, mediaType, name);
      if (extracted) {
        extracted._filename = name;
        result.extractions.push(extracted);
        _logToSheet('Extracted from ' + name + ': drawingType=' + (extracted._drawingType || 'unknown'));
      }

      // Carry every PDF that returned _sources forward for live cropping. Each
      // PDF with locatable fields contributes its snippets; the crop runs later
      // in _processIntakeThread once the project Drive folder exists. We keep
      // the raw bytes (not the base64) so _cropFieldsToSnippets re-encodes once.
      if (isPdf && extracted && extracted._sources &&
          Object.keys(extracted._sources).length) {
        result.pdfSources.push({ name: name, pdfBytes: bytes, sources: extracted._sources });
      }

      var snippetUrl = _getSnippetUrl(bytes, name, isPdf);
      if (snippetUrl) {
        var drawingType = (extracted && extracted._drawingType) ? extracted._drawingType : 'unknown';
        _mapSnippetUrl(result.snippets, drawingType, snippetUrl);
        _logToSheet('Snippet URL for ' + name + ': ' + snippetUrl);
      }

    } catch (err) {
      _logToSheet('Attachment processing error for ' + name + ': ' + err.message);
    }
  }

  return result;
}

function _extractFromAttachment(b64, mediaType, filename) {
  try {
    var apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
    if (!apiKey) return null;

    var contentType = mediaType === 'application/pdf' ? 'document' : 'image';

    var prompt = [
      'You are an expert HVAC/mechanical engineer and licensed PE reviewing architectural and engineering drawings.',
      'Your job is to extract every piece of information needed to fill out an HVAC load calculation work order.',
      'You know how architectural drawings are organized — use that knowledge to find data in the right places.',
      '',
      '=== WHERE TO FIND EACH DATA POINT ===',
      '',
      'TITLE BLOCK (usually bottom-right or cover sheet):',
      '  - Project name, property owner name, project address (street, city, state, zip)',
      '  - Also record city, state, zip, and county as SEPARATE fields (projectCity/state/zip/county), not just the combined address string',
      '  - Architect/engineer firm name, contact name, phone, email',
      '  - Sheet scale, drawing date',
      '  - Number of stories — "2-STORY", "SINGLE STORY", floor plan labeled "2ND FLOOR", etc.',
      '',
      'FLOOR PLAN TITLE / NOTES (text near the floor plan drawing):',
      '  - Total conditioned area in SF — look for "SF", "SQ FT", "LEASE", "AREA" near the plan title',
      '  - Occupancy count — look for "OCCUPANCY OF XX" or "OCC: XX"',
      '  - Building status — "NEW CONSTRUCTION", "TENANT BUILDOUT", "INTERIOR RENOVATION", "ADDITION", "RENOVATION"',
      '  - North arrow direction tells you building orientation',
      '',
      'REFLECTED CEILING PLAN (RCP) NOTES / CEILING NOTES:',
      '  - Default ceiling height — "CEILING HEIGHT AT X\'-Y" AFF" or "CLG HT = X\'-Y""',
      '  - Ceiling type — suspended ACT (T-bar grid), GWB, open to structure',
      '  - Lighting power density in W/SF — look for lighting schedules or power density notes',
      '',
      'WALL TYPES / ASSEMBLY SCHEDULE (table listing wall types A, B, C...):',
      '  - Wall construction: CMU, masonry, steel stud, wood frame, ICF',
      '  - Wall R-value from insulation specs (e.g. "R-5.7 continuous", "R-13 batt")',
      '  - Wall height from sections or elevation notes',
      '  - Exterior finish: stucco, EIFS, brick, metal panel',
      '  - Interior PARTITION wall construction and R-value (metal stud + batt, wood stud, CMU) — separate from exterior wall type',
      '  - FLOOR type and R-value: slab on grade, floor over unconditioned space, suspended floor — from foundation/section details',
      '',
      'ROOF / BUILDING SECTIONS / GENERAL NOTES:',
      '  - Roof R-value — "R-19 ROOF INSULATION", "R-30 above deck", etc.',
      '  - Insulation position: above deck, below deck/at ceiling, both',
      '  - Roof deck type: steel deck, concrete deck, wood deck, metal frame, wood frame',
      '  - Roof covering: TPO, EPDM, BUR, metal, tile, shingle',
      '  - Attic/plenum: vented attic vs sealed/conditioned plenum',
      '  - Suspended ceiling type below deck',
      '',
      'WINDOW / DOOR SCHEDULE OR GLAZING NOTES:',
      '  - Glass U-factor (e.g. "U=0.28", "U-FACTOR: 0.35") — this is the FIXED window value (glassU/glassSHGC)',
      '  - Glass SHGC (e.g. "SHGC=0.25")',
      '  - If only glass type is listed (e.g. "SINGLE PANE CLEAR"), infer: single pane clear = U~1.04/SHGC~0.86; double pane clear = U~0.48/SHGC~0.76; double pane low-e = U~0.28/SHGC~0.25',
      '  - OPERABLE windows (casement, awning, sliders that open) often have a separate, worse U/SHGC row — record as glassOperU/glassOperSHGC if the schedule distinguishes fixed vs operable',
      '  - Sliding GLASS DOORS (patio doors) frequently have their own U/SHGC row — record as glassSGDU/glassSGDSHGC',
      '  - Glass frame material: aluminum, vinyl, wood, fiberglass, metal (glassFrame)',
      '  - Glazing configuration: single/double/triple pane (glazingType) and tint: clear, tinted, low-e, reflective (glazingTint)',
      '  - Skylights: note quantity/size/type if shown on the roof plan or window schedule (skylights)',
      '  - Door type: insulated metal, hollow metal, solid wood, storefront/glass',
      '',
      'EQUIPMENT SCHEDULE:',
      '  - List all heat-generating equipment with BTU/h or watts if shown',
      '  - Medical: dental chairs, autoclaves, sterilizers, compressors, imaging equipment',
      '  - Restaurant: fryers, griddles, ovens, ranges — note linear footage under hood',
      '  - Office: server rooms, lab equipment',
      '  - Plug/equipment load power density in W/SF if a schedule or energy note states it (equipWattsPerSF) — separate from lighting',
      '',
      'LIGHTING SCHEDULE / ELECTRICAL NOTES:',
      '  - Lighting watts per SF — may be stated directly or calculable from fixture schedule',
      '  - Only extract if explicitly stated or calculable from the drawings — do not infer',
      '',
      'MECHANICAL / HVAC SCHEDULE (equipment schedule, mechanical notes, or site plan callouts):',
      '  - New vs existing equipment (acNewExisting): "NEW", "EXISTING", "EXISTING — REUSE", "EXISTING TO REMAIN"',
      '  - Mounting/location (acMounting): rooftop, ground-level slab, indoor closet, split system',
      '  - System type (systemType): split DX, package, VRF, heat pump, chiller',
      '  - HVAC type (hvacType): split, package, ductless mini-split',
      '  - Heat type (heatType): heat pump, gas furnace, electric strip, boiler',
      '  - Cooling/heating efficiency (coolingEff/heatingEff): SEER/SEER2, EER, HSPF/HSPF2 values if a model or schedule states them',
      '  - Efficiency tier (efficiencyTier): standard, high-efficiency, premium — only if the drawings characterize it that way',
      '  - Manufacturer (manufacturer): Carrier, Trane, Lennox, etc. if a specific unit is called out',
      '  - Outside air / exhaust provisions (hasOutsideAir, hasExhaust): note if dedicated OSA or exhaust fans are shown',
      '  - Electric heat strips (hasStrip, heatStripCOP): note if a strip heater kit is called out and its COP if stated',
      '  - Infiltration/tightness (infiltration): "Tight", "Average", "Leaky" if characterized; air change rate (changeRate) if an ACH value is stated',
      '',
      'WATER HEATER / PLUMBING NOTES:',
      '  - Water heater type (hwType): tank — gas, tank — electric, tankless, heat pump water heater',
      '  - Efficiency (hwEfficiency) and tank capacity in gallons (hwCapacityGal) if a model/schedule states them',
      '',
      'EXTERIOR LIGHTING / SITE PLAN (site electrical plan, exterior lighting schedule):',
      '  - Description of exterior fixture type (extLightDescription) — wall pack, pole light, canopy light, etc.',
      '  - Application category (extLightCategory) — building facade, parking lot, canopy, walkway',
      '  - Number of luminaires (extLightNumLuminaires) and watts per luminaire (extLightWattsPerLuminaire) if a fixture schedule states them',
      '  - Area or length being lit and its units (extLightAreaLengthUnits) — e.g. parking lot SF or walkway linear feet',
      '  - Control type (extLightControlType) — photocell, timer, occupancy sensor',
      '  - Only extract exterior lighting fields if explicitly shown on a site/electrical plan — do not infer',
      '',
      'ENERGY COMPLIANCE / MECHANICAL GENERAL NOTES (energy code compliance sheet, mechanical general notes):',
      '  - Winter design dry-bulb (osaLowDry) — look for "99% HEATING DB", "WINTER DESIGN TEMP", or an ASHRAE climate data table row for the design location',
      '  - Summer mean daily range (osaDailyRange) — look for "MEAN DAILY RANGE", "MDR", or the corresponding ASHRAE table column',
      '  - Indoor design temperature (indoorTemp) and relative humidity (indoorRH) — usually stated as a fixed assumption (e.g. "75°F / 50% RH"); only override the standard 75/50 default if the drawings explicitly state something different',
      '',
      'CLIENT / PROJECT IDENTITY:',
      '  - clientCode: the CLIENT FIRM that hired Adicot (architect, builder, design firm) — NOT the property owner, NOT the end-occupant, NOT a product manufacturer (e.g. PGT, window/door brands are NOT clients).',
      '  - Map to one of these KNOWN CLIENT CODES if the firm matches:',
      _clientCodesPromptBlock(),
      '  - If this sheet is a product approval / NOA / manufacturer spec (not a project drawing), set clientCode to null — do not invent one from the manufacturer name.',
      '  - subClient: the specific sub-client, doctor name, gym brand, or project descriptor within the client org (e.g. "Dr Watts", "F45 Gym", "G&B", "4 Unit Coach").',
      '  - locationDisambig: location or parenthetical to disambiguate (e.g. "(Apollo)", "Spring Hill", "MA", "Largo"). Omit if not needed.',
      '',
      '=== DRAWING TYPE CLASSIFICATION ===',
      'titleBlock, floorPlan, rcp, wallSection, energyNotes, elevation, mechanical, equipSchedule, esr, other',
      '',
      '=== SOURCE LOCATIONS (_sources) — WHERE EACH VALUE SITS ON THE PAGE ===',
      'For every field you fill with a real value (not null), record WHERE on the',
      'page you read it, so a cropped image of that spot can be shown next to the',
      'value for verification. You are looking at the rasterized page image — point',
      'at the value with a bounding box.',
      'Add a "_sources" object. Each key is a field name from the JSON below; each value is:',
      '  { "page": <1-based page number in this PDF>, "bbox": [x, y, w, h] }',
      'bbox is the rectangle around the value, in NORMALIZED page fractions:',
      '  - x = left edge as a fraction of page WIDTH  (0 = far left, 1 = far right)',
      '  - y = top edge as a fraction of page HEIGHT  (0 = top, 1 = bottom)',
      '  - w = box width  as a fraction of page width',
      '  - h = box height as a fraction of page height',
      'Origin is the TOP-LEFT corner of the page. All four numbers are between 0 and 1.',
      'Example: a value in the bottom-right title block might be [0.78, 0.90, 0.14, 0.05].',
      '',
      'BBOX RULES:',
      '  - Draw the box around the VALUE and its immediate label, not the whole sheet.',
      '    Tight enough to identify the value; loose enough to include its label/units.',
      '  - A good box is usually 0.05–0.35 wide and 0.02–0.15 tall. If you are boxing',
      '    half the sheet, you are being too loose — find the specific spot.',
      '  - Box what you actually SAW and read. Do not guess a location for an inferred',
      '    value — OMIT that field from _sources instead.',
      '',
      'DEDUP: if two fields are read from the SAME spot (e.g. glassU and glassSHGC in',
      'one schedule row, or projectName and projectAddress in the title block), give',
      'them the SAME page and an IDENTICAL bbox — one image of that spot is reused for',
      'both. OMIT a field from _sources entirely if you could not locate it on the page,',
      'or if the value was inferred rather than read. Only record sources for values you',
      'actually SAW.',
      '',
      'Return ONLY valid JSON — no markdown, no explanation, no preamble:',
      '{',
      '  "_drawingType": string,',
      '  "_notesFound": string,',
      '  "clientCode": string,',
      '  "subClient": string,',
      '  "locationDisambig": string,',
      '  "community": string,',
      '  "subdivision": string,',
      '  "projectName": string,',
      '  "projectAddress": string,',
      '  "projectCity": string,',
      '  "projectState": string,',
      '  "projectZip": string,',
      '  "projectCounty": string,',
      '  "numStories": string,',
      '  "propertyOwner": string,',
      '  "clientFirst": string,',
      '  "clientLast": string,',
      '  "clientCompany": string,',
      '  "clientPhone": string,',
      '  "clientEmail": string,',
      '  "productService": string,',
      '  "sf": number,',
      '  "occupancyType": string,',
      '  "buildingStatus": string,',
      '  "occupants": number,',
      '  "orientation": string,',
      '  "ceilingHeight": string,',
      '  "suspCeiling": string,',
      '  "atticCond": string,',
      '  "deckType": string,',
      '  "roofCover": string,',
      '  "insulPosition": string,',
      '  "roofRValue": string,',
      '  "roofColor": string,',
      '  "wallConstruction": string,',
      '  "wallFinish": string,',
      '  "wallColor": string,',
      '  "wallRValue": string,',
      '  "wallHeight": string,',
      '  "partConstruction": string,',
      '  "partRValue": string,',
      '  "floorType": string,',
      '  "floorRValue": string,',
      '  "doorType": string,',
      '  "glassU": number,',
      '  "glassSHGC": number,',
      '  "glassOperU": number,',
      '  "glassOperSHGC": number,',
      '  "glassSGDU": number,',
      '  "glassSGDSHGC": number,',
      '  "glassFrame": string,',
      '  "glazingType": string,',
      '  "glazingTint": string,',
      '  "skylights": string,',
      '  "lightingWattsPerSF": number,',
      '  "equipWattsPerSF": number,',
      '  "heatGenEquipment": string,',
      '  "infiltration": string,',
      '  "changeRate": string,',
      '  "acNewExisting": string,',
      '  "acMounting": string,',
      '  "systemType": string,',
      '  "hvacType": string,',
      '  "heatType": string,',
      '  "coolingEff": string,',
      '  "heatingEff": string,',
      '  "efficiencyTier": string,',
      '  "manufacturer": string,',
      '  "hasOutsideAir": string,',
      '  "hasExhaust": string,',
      '  "hasStrip": string,',
      '  "heatStripCOP": string,',
      '  "hwType": string,',
      '  "hwEfficiency": string,',
      '  "hwCapacityGal": string,',
      '  "extLightDescription": string,',
      '  "extLightCategory": string,',
      '  "extLightNumLuminaires": string,',
      '  "extLightWattsPerLuminaire": string,',
      '  "extLightAreaLengthUnits": string,',
      '  "extLightControlType": string,',
      '  "osaLowDry": string,',
      '  "osaDailyRange": string,',
      '  "indoorTemp": string,',
      '  "indoorRH": string,',
      '  "description": string,',
      '  "_sources": { "<fieldName>": { "page": number, "bbox": [number, number, number, number] } }',
      '}',
      '',
      'Use null for any field not found or not inferable. Never return 0 for sf or occupants — use null if unknown.',
      'For lightingWattsPerSF and equipWattsPerSF: only return a value if explicitly stated or directly calculable from the drawings. Return null otherwise.',
      'For indoorTemp/indoorRH: only return a value if the drawings explicitly state design conditions different from the standard 75°F / 50% RH assumption — otherwise return null and the standard default will be used.',
    ].join('\n');

    var messageContent = [
      { type: contentType, source: { type: 'base64', media_type: mediaType, data: b64 } },
      { type: 'text', text: prompt },
    ];

    var response = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', {
      method:      'post',
      contentType: 'application/json',
      headers: {
        'x-api-key':         apiKey,
        'anthropic-version': '2023-06-01',
      },
      payload: JSON.stringify({
        model:      MODEL_SONNET,
        max_tokens: 4096,
        messages:   [{ role: 'user', content: messageContent }],
      }),
      muteHttpExceptions: true,
    });

    var result = JSON.parse(response.getContentText());
    if (!result.content || !result.content[0]) {
      _logToSheet('Claude attachment API error: ' + JSON.stringify(result).substring(0, 300));
      return null;
    }
    var text  = result.content[0].text.trim();
    var clean = text.replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();
    return JSON.parse(clean);

  } catch (err) {
    _logToSheet('_extractFromAttachment ERROR for ' + filename + ': ' + err.message);
    return null;
  }
}

function _getSnippetUrl(bytes, filename, isPdf) {
  var tempFileId = null;
  try {
    var mimeType = isPdf ? 'application/pdf' : 'image/jpeg';
    var blob     = Utilities.newBlob(bytes, mimeType, filename);
    var tmpFile  = DriveApp.createFile(blob);
    tmpFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    tempFileId   = tmpFile.getId();

    Utilities.sleep(3000);

    var thumbnailLink = null;
    try {
      var fileInfo = Drive.Files.get(tempFileId, { fields: 'thumbnailLink' });
      thumbnailLink  = fileInfo.thumbnailLink;
    } catch (driveErr) {
      _logToSheet('Drive thumbnail error for ' + filename + ': ' + driveErr.message);
      return null;
    }

    if (!thumbnailLink) {
      _logToSheet('No thumbnail generated for: ' + filename);
      return null;
    }

    var url = thumbnailLink.replace(/=s\d+$/, '=s1200');
    _logToSheet('Snippet URL for ' + filename + ': ' + url);
    return url;

  } catch (err) {
    _logToSheet('_getSnippetUrl ERROR for ' + filename + ': ' + err.message);
    return null;
  } finally {
    if (tempFileId) {
      try { DriveApp.getFileById(tempFileId).setTrashed(true); } catch(_) {}
    }
  }
}

function _mapSnippetUrl(snippets, drawingType, url) {
  switch (drawingType) {
    case 'titleBlock':   snippets.titleBlock  = snippets.titleBlock  || url; break;
    case 'floorPlan':    snippets.floorPlan   = snippets.floorPlan   || url; break;
    case 'rcp':          snippets.rcp         = snippets.rcp         || url; break;
    case 'energyNotes':  snippets.energyNotes = snippets.energyNotes || url; break;
    case 'elevation':    snippets.elevation   = snippets.elevation   || url; break;
    case 'mechanical':   snippets.mechanical  = snippets.mechanical  || url; break;
    case 'esr':          snippets.titleBlock  = snippets.titleBlock  || url; break;
    default:
      snippets.titleBlock = snippets.titleBlock || url;
      snippets.rcp        = snippets.rcp        || url;
      break;
  }
}

function _mergeExtractions(emailData, pdfExtractions) {
  var merged = Object.assign({}, emailData);

  var IDENTITY_FIELDS = ['clientCode','subClient','locationDisambig','community','subdivision','projectName',
    'projectAddress','projectCity','projectState','projectZip','projectCounty','propertyOwner',
    'clientFirst','clientLast','clientPhone','clientEmail','clientCompany','productService'];

  var TECHNICAL_FIELDS = ['sf','occupancyType','buildingStatus','occupants','orientation',
    'ceilingHeight','deckType','roofCover','insulPosition','suspCeiling','atticCond',
    'roofRValue','roofColor','wallConstruction','wallFinish','wallColor','wallRValue',
    'wallHeight','partConstruction','partRValue','floorType','floorRValue',
    'doorType','glassU','glassSHGC','glassOperU','glassOperSHGC','glassSGDU','glassSGDSHGC',
    'glassFrame','glazingType','glazingTint','skylights',
    'lightingWattsPerSF','equipWattsPerSF','heatGenEquipment','infiltration','changeRate',
    'acNewExisting','acMounting','systemType','hvacType','heatType','coolingEff','heatingEff',
    'efficiencyTier','manufacturer','hasOutsideAir','hasExhaust','hasStrip','heatStripCOP',
    'hwType','hwEfficiency','hwCapacityGal',
    'extLightDescription','extLightCategory','extLightNumLuminaires','extLightWattsPerLuminaire',
    'extLightAreaLengthUnits','extLightControlType',
    'osaLowDry','osaDailyRange','indoorTemp','indoorRH','numStories',
    'description'];

  function _empty(v) { return v === null || v === undefined || v === '' || v === 0; }

  for (var p = 0; p < pdfExtractions.length; p++) {
    var pdf = pdfExtractions[p];
    if (!pdf) continue;

    var dt = (pdf._drawingType || '').toLowerCase();
    var notes = (pdf._notesFound || '').toLowerCase();
    var isNonProject = dt.indexOf('other') !== -1 ||
                       notes.indexOf('notice of acceptance') !== -1 ||
                       notes.indexOf('noa') !== -1 ||
                       notes.indexOf('product approval') !== -1 ||
                       notes.indexOf('product control') !== -1;

    for (var t = 0; t < TECHNICAL_FIELDS.length; t++) {
      var tf = TECHNICAL_FIELDS[t];
      if (_empty(merged[tf]) && !_empty(pdf[tf])) merged[tf] = pdf[tf];
    }

    if (!isNonProject) {
      for (var k = 0; k < IDENTITY_FIELDS.length; k++) {
        var idf = IDENTITY_FIELDS[k];
        if (_empty(merged[idf]) && !_empty(pdf[idf])) merged[idf] = pdf[idf];
      }
    }
  }

  return merged;
}


// ── EMAIL BODY EXTRACTION ─────────────────────────────────────────────────────

function _extractWithClaude(subject, fromEmail, body) {
  try {
    var apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
    if (!apiKey) { _logToSheet('ANTHROPIC_API_KEY not set'); return null; }

    var prompt = [
      'You are an intake processor for Adicot Engineering, an HVAC/mechanical engineering firm.',
      'Extract structured data from the following email inquiry and return ONLY valid JSON — no markdown, no explanation.',
      '',
      'Email subject: ' + subject,
      'From: ' + fromEmail,
      'Body:',
      body,
      '',
      '=== KNOWN CLIENT CODES (map to one of these if the firm matches; only propose a NEW code if clearly none apply) ===',
      _clientCodesPromptBlock(),
      'IMPORTANT: clientCode is the CLIENT FIRM that sends Adicot work (architect, builder, design firm) — NOT the property owner, NOT the end-occupant, NOT a product manufacturer. If you must propose a new code, also set "_isNewClient": true and give "_proposedClientName" and "_proposedAliases".',
      '',
      'Return a JSON object with these fields (use null for anything not mentioned):',
      '{',
      '  "clientCode": string,',
      '  "_isNewClient": boolean,',
      '  "_proposedClientName": string,',
      '  "_proposedAliases": string,',
      '  "subClient": string,',
      '  "locationDisambig": string,',
      '  "community": string,',
      '  "subdivision": string,',
      '  "projectName": string,',
      '  "projectAddress": string,',
      '  "projectCity": string,',
      '  "projectState": string,',
      '  "projectZip": string,',
      '  "projectCounty": string,',
      '  "propertyOwner": string,',
      '  "clientFirst": string,',
      '  "clientLast": string,',
      '  "clientEmail": string,',
      '  "clientPhone": string,',
      '  "clientCompany": string,',
      '  "productService": string,',
      '  "sf": number,',
      '  "occupancyType": string,',
      '  "buildingStatus": string,',
      '  "occupants": number,',
      '  "ceilingHeight": string,',
      '  "orientation": string,',
      '  "roofRValue": string,',
      '  "roofColor": string,',
      '  "wallConstruction": string,',
      '  "wallFinish": string,',
      '  "wallRValue": string,',
      '  "glassU": number,',
      '  "glassSHGC": number,',
      '  "lightingWattsPerSF": number,',
      '  "description": string',
      '}',
    ].join('\n');

    var response = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', {
      method: 'post', contentType: 'application/json',
      headers: { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
      payload: JSON.stringify({
        model:      MODEL_HAIKU,
        max_tokens: 1024,
        messages:   [{ role: 'user', content: prompt }],
      }),
      muteHttpExceptions: true,
    });

    var result = JSON.parse(response.getContentText());
    if (!result.content || !result.content[0]) { _logToSheet('Claude email API error: ' + JSON.stringify(result).substring(0, 200)); return null; }
    var text  = result.content[0].text.trim();
    var clean = text.replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();
    return JSON.parse(clean);

  } catch (err) {
    _logToSheet('_extractWithClaude ERROR: ' + err.message);
    return null;
  }
}


// ── TRIGGER MANAGEMENT ────────────────────────────────────────────────────────

function installIntakeTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'processIntakeEmails') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('processIntakeEmails').timeBased().everyMinutes(5).create();
  _logToSheet('installIntakeTrigger: trigger installed');
}

function testIntakePipeline() {
  var intakeLabel = GmailApp.getUserLabelByName(INTAKE_LABEL);
  if (!intakeLabel) { Logger.log('Label not found: ' + INTAKE_LABEL); return; }
  var threads = intakeLabel.getThreads(0, 1);
  if (!threads.length) { Logger.log('No threads with label: ' + INTAKE_LABEL); return; }
  var thread  = threads[0];
  var msg     = thread.getMessages()[thread.getMessages().length - 1];
  var subject = thread.getFirstMessageSubject();
  var from    = msg.getFrom();
  var body    = msg.getPlainBody();
  Logger.log('Subject: ' + subject);
  Logger.log('From: ' + from);

  var emailData = _extractWithClaude(subject, from, body);
  Logger.log('Email extracted: ' + JSON.stringify(emailData, null, 2));
}
// Reads the latest intake thread's first PDF attachment, runs the Sonnet
// extraction, and logs the _sources boxes so they can be eyeballed BEFORE the
// Flask crop service exists. Run this manually from the editor.
function testSnippetExtraction() {
  var intakeLabel = GmailApp.getUserLabelByName(INTAKE_LABEL);
  if (!intakeLabel) { Logger.log('Label not found: ' + INTAKE_LABEL); return; }
  var threads = intakeLabel.getThreads(0, 1);
  if (!threads.length) { Logger.log('No threads with label: ' + INTAKE_LABEL); return; }
  var thread = threads[0];
  var msg = thread.getMessages()[thread.getMessages().length - 1];
  Logger.log('Subject: ' + thread.getFirstMessageSubject());

  var attachments = msg.getAttachments({ includeInlineImages: true });
  var done = false;
  for (var i = 0; i < attachments.length && !done; i++) {
    var att = attachments[i];
    var name = att.getName() || 'attachment';
    var mimeType = att.getContentType() || '';
    var isPdf = mimeType === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
    if (!isPdf) continue;

    var bytes = att.getBytes();
    if (bytes.length > MAX_PDF_BYTES) { Logger.log('Skipping (too large): ' + name); continue; }

    Logger.log('Extracting: ' + name + ' (' + Math.round(bytes.length/1024) + 'KB)');
    var b64 = Utilities.base64Encode(bytes);
    var extracted = _extractFromAttachment(b64, 'application/pdf', name);
    if (!extracted) { Logger.log('Extraction returned null for ' + name); continue; }

    Logger.log('drawingType: ' + (extracted._drawingType || 'unknown'));
    if (extracted._sources) {
      Logger.log('_sources:\n' + JSON.stringify(extracted._sources, null, 2));
      Logger.log('Field count with sources: ' + Object.keys(extracted._sources).length);
    } else {
      Logger.log('NO _sources returned — check the prompt edit landed.');
    }
    Logger.log('Full extraction:\n' + JSON.stringify(extracted, null, 2));
    done = true;
  }
  if (!done) Logger.log('No PDF attachment found on the latest intake thread.');
}

// =============================================================================
// SNIPPET OVERLAY TEST — paste into AdicotProjects.gs (near testSnippetExtraction)
// =============================================================================
// Sends the latest intake PDF + its _sources boxes to the Flask /crop route in
// OVERLAY mode. Flask draws every (padded) box as a red rectangle on the real
// drawing page and returns it. We save that image to Drive and log the link so
// the boxes can be eyeballed on the actual page BEFORE trusting live crops.
//
// SETUP (one time):
//   Apps Script -> Project Settings -> Script Properties, add:
//     CROP_TOKEN   = <the same long random string Miles sets on Render>
//     CROP_URL     = https://adicot-load-calc-doc.onrender.com/crop
//
// Run testSnippetOverlay() from the editor, then open the logged link.
// =============================================================================

function testSnippetOverlay() {
  var props    = PropertiesService.getScriptProperties();
  var cropUrl  = props.getProperty('CROP_URL');
  var cropTok  = props.getProperty('CROP_TOKEN');
  if (!cropUrl || !cropTok) {
    Logger.log('Set CROP_URL and CROP_TOKEN in Script Properties first.');
    return;
  }

  var intakeLabel = GmailApp.getUserLabelByName(INTAKE_LABEL);
  if (!intakeLabel) { Logger.log('Label not found: ' + INTAKE_LABEL); return; }
  var threads = intakeLabel.getThreads(0, 1);
  if (!threads.length) { Logger.log('No threads with label: ' + INTAKE_LABEL); return; }
  var thread = threads[0];
  var msg    = thread.getMessages()[thread.getMessages().length - 1];
  Logger.log('Subject: ' + thread.getFirstMessageSubject());

  // Find the first PDF attachment
  var attachments = msg.getAttachments({ includeInlineImages: true });
  var pdfAtt = null;
  for (var i = 0; i < attachments.length; i++) {
    var a = attachments[i];
    var nm = a.getName() || '';
    var mt = a.getContentType() || '';
    if (mt === 'application/pdf' || nm.toLowerCase().endsWith('.pdf')) {
      if (a.getBytes().length <= MAX_PDF_BYTES) { pdfAtt = a; break; }
    }
  }
  if (!pdfAtt) { Logger.log('No PDF attachment found on the latest intake thread.'); return; }

  var bytes = pdfAtt.getBytes();
  var b64   = Utilities.base64Encode(bytes);
  Logger.log('PDF: ' + pdfAtt.getName() + ' (' + Math.round(bytes.length/1024) + 'KB)');

  // Extract _sources
  var extracted = _extractFromAttachment(b64, 'application/pdf', pdfAtt.getName());
  if (!extracted || !extracted._sources) {
    Logger.log('No _sources from extraction — cannot overlay.');
    return;
  }
  Logger.log('Fields with sources: ' + Object.keys(extracted._sources).length);

  // Call /crop in overlay mode
  var payload = JSON.stringify({
    pdf_b64: b64,
    sources: extracted._sources,
    overlay: true,
  });
  var resp = UrlFetchApp.fetch(cropUrl, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Crop-Token': cropTok },
    payload: payload,
    muteHttpExceptions: true,
  });
  var code = resp.getResponseCode();
  if (code !== 200) {
    Logger.log('Crop route returned ' + code + ': ' + resp.getContentText().substring(0, 300));
    return;
  }
  var result = JSON.parse(resp.getContentText());
  if (!result.ok) {
    Logger.log('Overlay failed: ' + JSON.stringify(result.errors || result).substring(0, 300));
    return;
  }

  // Save each returned page image to Drive (root) and log links
  var pages = result.pages || {};
  var keys = Object.keys(pages);
  if (!keys.length) { Logger.log('No overlay pages returned.'); return; }

  for (var k = 0; k < keys.length; k++) {
    var pageNo = keys[k];
    var imgBytes = Utilities.base64Decode(pages[pageNo]);
    var blob = Utilities.newBlob(imgBytes, 'image/jpeg',
      'overlay_' + pdfAtt.getName().replace(/\.pdf$/i,'') + '_p' + pageNo + '.jpg');
    var file = DriveApp.createFile(blob);
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    Logger.log('Overlay page ' + pageNo + ': ' + file.getUrl());
  }
  Logger.log('Open the link(s) above to see the red boxes on the real drawing.');
}
function listAllLabels() {
  GmailApp.getUserLabels().forEach(function(l) { Logger.log(l.getName()); });
}
// =============================================================================
// LIVE SNIPPET CROPPING — paste into AdicotProjects.gs
// =============================================================================
// Sends a drawing PDF + its _sources sections to the Flask /crop route, gets one
// cropped JPEG per field, uploads each into the project's Drive folder under
// "1-From Client/snippets/", and returns a { field: imageUrl } map.
//
// The map is stored in ONE CMS field (snippetMap, JSON string). The review page
// reads it and shows each field's thumbnail next to that field. Adding fields
// later needs no CMS change.
//
// Uses CROP_URL + CROP_TOKEN from Script Properties (same as the overlay test).
// Drive writes use the existing Shared Drive pattern (Drive.Files.create,
// supportsAllDrives), same as createProjectFolder.
// =============================================================================

// Find (or create) the snippets folder inside a project's "1-From Client".
// createProjectFolder already makes "1-From Client"; we add "snippets" under it.
function _getSnippetsFolderId(projectFolderId) {
  if (!projectFolderId) return null;
  try {
    var fromClient = _findOrCreateFolder('1-From Client', projectFolderId);
    var snippets   = _findOrCreateFolder('snippets', fromClient.id);
    return snippets.id;
  } catch (err) {
    _logToSheet('_getSnippetsFolderId ERROR: ' + err.message);
    return null;
  }
}

// Upload one JPEG (raw bytes) into the snippets folder, return a thumbnail URL.
function _uploadSnippet(bytes, filename, snippetsFolderId) {
  var blob = Utilities.newBlob(bytes, 'image/jpeg', filename);
  var created = Drive.Files.create(
    { name: filename, parents: [snippetsFolderId] },
    blob,
    { supportsAllDrives: true, fields: 'id' }
  );
  var fileId = created.id;
  // Link-share so the thumbnail renders on the review page.
  try {
    Drive.Permissions.create(
      { role: 'reader', type: 'anyone' },
      fileId,
      { supportsAllDrives: true }
    );
  } catch (permErr) {
    _logToSheet('_uploadSnippet permission warn for ' + filename + ': ' + permErr.message);
  }
  // Thumbnail URL (renders inline). Falls back to file view link if absent.
  var info = Drive.Files.get(fileId, { fields: 'thumbnailLink,webViewLink', supportsAllDrives: true });
  var url = info.thumbnailLink ? info.thumbnailLink.replace(/=s\d+$/, '=s2000')
                               : info.webViewLink;
  return url;
}

// Main entry: crop every located field and build the field->url map.
//   pdfBytes        : raw PDF bytes (one drawing)
//   sources         : the _sources object from extraction
//   finalRecord     : the merged record — only crop fields that have a real value
//   projectFolderId : driveFolderId of the project (from createProjectFolder)
// Returns { map: {field:url}, count: n, errors: [...] }
function _cropFieldsToSnippets(pdfBytes, sources, finalRecord, projectFolderId) {
  var out = { map: {}, count: 0, errors: [] };
  if (!sources || !Object.keys(sources).length) return out;

  var props   = PropertiesService.getScriptProperties();
  var cropUrl = props.getProperty('CROP_URL');
  var cropTok = props.getProperty('CROP_TOKEN');
  if (!cropUrl || !cropTok) { out.errors.push('CROP_URL/CROP_TOKEN not set'); return out; }

  var snippetsFolderId = _getSnippetsFolderId(projectFolderId);
  if (!snippetsFolderId) { out.errors.push('no snippets folder'); return out; }

  // Only crop fields that survived into the final record (skip values that lost
  // the merge or are blank — no point cropping data we didn't keep).
  function _has(v) { return v !== null && v !== undefined && v !== '' && v !== 0; }
  var wanted = Object.keys(sources).filter(function(f) {
    // glass pair: keep if either glass value is present
    if (f === 'glassU' || f === 'glassSHGC') return _has(finalRecord.glassU) || _has(finalRecord.glassSHGC);
    return _has(finalRecord[f]);
  });
  if (!wanted.length) return out;

  var payload = JSON.stringify({
    pdf_b64: Utilities.base64Encode(pdfBytes),
    sources: sources,
    fields:  wanted,
  });

  var resp;
  try {
    resp = UrlFetchApp.fetch(cropUrl, {
      method: 'post', contentType: 'application/json',
      headers: { 'X-Crop-Token': cropTok },
      payload: payload, muteHttpExceptions: true,
    });
  } catch (fetchErr) {
    out.errors.push('crop fetch failed: ' + fetchErr.message);
    return out;
  }

  if (resp.getResponseCode() !== 200) {
    out.errors.push('crop route ' + resp.getResponseCode() + ': ' + resp.getContentText().substring(0, 200));
    return out;
  }
  var result = JSON.parse(resp.getContentText());
  if (!result.ok) { out.errors.push('crop not ok: ' + JSON.stringify(result.errors || {}).substring(0,200)); return out; }

  var crops  = result.crops  || {};
  var shared = result.shared || {};

  // Upload each unique crop once; record its URL by field.
  var urlByField = {};
  Object.keys(crops).forEach(function(field) {
    try {
      var c = crops[field];
      var bytes = Utilities.base64Decode(c.b64);
      var fname = 'snip_' + field + '_p' + (c.page || 1) + '.jpg';
      var url = _uploadSnippet(bytes, fname, snippetsFolderId);
      urlByField[field] = url;
      out.map[field] = url;
      out.count++;
    } catch (upErr) {
      out.errors.push(field + ' upload: ' + upErr.message);
    }
  });

  // Fields that reused another field's crop point at the same URL.
  Object.keys(shared).forEach(function(field) {
    var src = shared[field];
    if (urlByField[src]) out.map[field] = urlByField[src];
  });

  return out;
}
// =============================================================================
// LIVE CROP TEST — paste into Code.gs near testSnippetOverlay
// =============================================================================
// Proves the live crop path end-to-end WITHOUT touching the intake pipeline:
// extracts the latest intake PDF, calls _cropFieldsToSnippets against a TEMP
// Drive folder (not a project folder), and logs each field's snippet URL so the
// real cropped section images can be opened and judged.
//
// Requires _cropFieldsToSnippets (from gs_live_crop.js) to be pasted in already.
// =============================================================================

function testLiveCrop() {
  var intakeLabel = GmailApp.getUserLabelByName(INTAKE_LABEL);
  if (!intakeLabel) { Logger.log('Label not found: ' + INTAKE_LABEL); return; }
  var threads = intakeLabel.getThreads(0, 1);
  if (!threads.length) { Logger.log('No threads with label: ' + INTAKE_LABEL); return; }
  var thread = threads[0];
  var msg    = thread.getMessages()[thread.getMessages().length - 1];
  Logger.log('Subject: ' + thread.getFirstMessageSubject());

  // first PDF attachment
  var attachments = msg.getAttachments({ includeInlineImages: true });
  var pdfAtt = null;
  for (var i = 0; i < attachments.length; i++) {
    var a = attachments[i], nm = a.getName() || '', mt = a.getContentType() || '';
    if ((mt === 'application/pdf' || nm.toLowerCase().endsWith('.pdf')) &&
        a.getBytes().length <= MAX_PDF_BYTES) { pdfAtt = a; break; }
  }
  if (!pdfAtt) { Logger.log('No PDF attachment found.'); return; }

  var bytes = pdfAtt.getBytes();
  var b64   = Utilities.base64Encode(bytes);
  Logger.log('PDF: ' + pdfAtt.getName());

  var extracted = _extractFromAttachment(b64, 'application/pdf', pdfAtt.getName());
  if (!extracted || !extracted._sources) { Logger.log('No _sources — rerun.'); return; }
  Logger.log('Fields with sources: ' + Object.keys(extracted._sources).length);

  // Make a throwaway Drive folder to receive the crops for this test.
  var testFolder = DriveApp.createFolder('SNIPPET TEST ' + new Date().toISOString());
  var testFolderId = testFolder.getId();
  Logger.log('Test folder: ' + testFolder.getUrl());

  // _cropFieldsToSnippets expects a PROJECT folder id and builds
  // "1-From Client/snippets" under it — for the test we just pass the throwaway
  // folder; it will create those subfolders inside it. The final record is the
  // extraction itself (so _has() keeps the fields that have values).
  var res = _cropFieldsToSnippets(bytes, extracted._sources, extracted, testFolderId);

  Logger.log('Crops uploaded: ' + res.count);
  if (res.errors && res.errors.length) Logger.log('Errors: ' + res.errors.join(' | '));
  Object.keys(res.map).forEach(function(field) {
    Logger.log('  ' + field + ' -> ' + res.map[field]);
  });
  Logger.log('Open the test folder link above to see all cropped section images.');
}

function _buildAdminReviewLink(data) {
  // Points at the Flask app's /job/<id>/star (Render), not the retired Wix
  // admin-review page. Unlike the old Wix page, this route is keyed only by
  // the Sheets row _id — there's no jobNo-based fallback route to fall back to.
  var id = data._id || data.projectId || '';
  return PORTAL_BASE_URL + '/job/' + encodeURIComponent(id) + '/star';
}

function _esc(s)     { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function _escAttr(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }


// ── APPEND A NEW PROJECT ROW (legacy — no longer called; see notifyProjectsSheet) ─

function appendProjectRow(data) {
  var sheet    = _getSheet();
  var lastRow  = sheet.getLastRow() + 1;
  var sfPerDol = (data.totalCost && data.sf) ? (data.totalCost / data.sf).toFixed(2) : "";
  var row      = new Array(COL.DRIVE_FOLDER).fill("");
  row[COL.DATE-1]              = data.dateReceived   || new Date().toLocaleDateString("en-US");
  row[COL.QUOTE_TO-1]          = data.quoteTO        || "";
  row[COL.PROJECT_NAME-1]      = data.projectFolder  || data.projectName || "";
  row[COL.PROPERTY_OWNER-1]    = data.propertyOwner  || "";
  row[COL.PROJECT_ADDRESS-1]   = data.projectAddress || "";
  row[COL.TOTAL_COST-1]        = data.totalCost      || "";
  row[COL.SF-1]                = data.sf             || "";
  row[COL.SF_PER_DOLLAR-1]     = sfPerDol;
  row[COL.PRODUCT_SERVICE-1]   = data.productService || "";
  row[COL.STATUS-1]            = data.status         || "Pending";
  row[COL.OCCUPANCY-1]         = data.occupancyType  || data.occupancy || "";
  row[COL.JOB_NO-1]            = data.jobNo          || "";
  row[COL.DESCRIPTION-1]       = data.description    || "";
  row[COL.STATE-1]             = data.state          || "";
  row[COL.COUNTY-1]            = data.county         || "";
  row[COL.DATE_RECEIVED-1]     = data.dateReceived   || "";
  row[COL.FORM_VERSION-1]      = "v2";
  row[COL.BUILDING_STATUS-1]   = data.buildingStatus    || "";
  row[COL.ORIENTATION-1]       = data.orientation       || "";
  row[COL.OCCUPANTS-1]         = data.occupants         || "";
  row[COL.ROOF_DECK_TYPE-1]    = data.deckType          || "";
  row[COL.ROOF_INSUL_POS-1]    = data.insulPosition     || "";
  row[COL.ROOF_SUSP_CEIL-1]    = data.suspCeiling       || "";
  row[COL.ROOF_R_VALUE-1]      = data.roofRValue        || "";
  row[COL.ROOF_COLOR-1]        = data.roofColor         || "";
  row[COL.CEIL_HEIGHT-1]       = data.ceilingHeight     || "";
  row[COL.WALL_FINISH-1]       = data.wallFinish        || "";
  row[COL.WALL_CONSTRUCTION-1] = data.wallConstruction  || "";
  row[COL.WALL_COLOR-1]        = data.wallColor         || "";
  row[COL.WALL_R_VALUE-1]      = data.wallRValue        || "";
  row[COL.WALL_HEIGHT-1]       = data.wallHeight        || "";
  row[COL.GLASS_FIXED_U-1]     = data.glassU            || "";
  row[COL.GLASS_FIXED_SHGC-1]  = data.glassSHGC         || "";
  row[COL.DOOR_TYPE-1]         = data.doorType          || "";
  row[COL.LIGHTING_OCC-1]      = data.lpdSpaceType      || data.lightingOcc || "";
  row[COL.LIGHTING_WPF-1]      = data.lightingWattsPerSF || data.lightingWpf || "";
  row[COL.HEAT_GEN_EQUIP-1]    = data.heatGenEquipment  || data.heatGenEquip || "";
  row[COL.AC_NEW_EXISTING-1]   = data.acNewExisting     || "";
  row[COL.AC_MOUNTING-1]       = data.acMounting        || "";
  row[COL.PROJECT_NOTES-1]     = data.projectNotes      || "";
  row[COL.DRIVE_FOLDER-1]      = data.driveFolderUrl    || data.driveFolderLink || "";

  try {
    sheet.getRange(lastRow, 1, 1, row.length).setValues([row]);
  } catch (validationErr) {
    _logToSheet('appendProjectRow: column B validation failed — writing row without it');
    row[COL.QUOTE_TO - 1] = '';
    sheet.getRange(lastRow, 1, 1, row.length).setValues([row]);
  }

  SpreadsheetApp.flush();
  return lastRow;
}


// ── SLACK ─────────────────────────────────────────────────────────────────────

function postToSlack(message, blocks) {
  UrlFetchApp.fetch(SLACK_WEBHOOK, { method: "post", contentType: "application/json", payload: JSON.stringify(blocks ? { blocks: blocks } : { text: message }) });
}


// ── CLIENT CODE REGISTRY ──────────────────────────────────────────────────────
// Lives in a "Client Codes" tab of the same spreadsheet (SHEET_ID) — columns
// clientCode, clientName, aliases — created automatically on first use.
// Replaces the old Wix _functions/clientCodes + addClientCode endpoints.

const CLIENT_CODES_TAB_NAME = "Client Codes";
var _clientCodesCache = null;

function _getClientCodesSheet() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var sheet = ss.getSheetByName(CLIENT_CODES_TAB_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(CLIENT_CODES_TAB_NAME);
    sheet.getRange(1, 1, 1, 3).setValues([["clientCode", "clientName", "aliases"]]);
  }
  return sheet;
}

function _getClientCodes() {
  if (_clientCodesCache) return _clientCodesCache;
  try {
    var sheet = _getClientCodesSheet();
    var rows = sheet.getDataRange().getValues();
    var codes = [];
    for (var i = 1; i < rows.length; i++) {   // skip header row
      var row = rows[i];
      if (!row[0]) continue;
      codes.push({ clientCode: row[0], clientName: row[1] || '', aliases: row[2] || '' });
    }
    _clientCodesCache = codes;
  } catch (err) {
    _logToSheet('_getClientCodes ERROR: ' + err.message);
    _clientCodesCache = [];
  }
  return _clientCodesCache;
}

function _clientCodesPromptBlock() {
  var codes = _getClientCodes();
  if (!codes.length) return 'No known client codes yet — propose a new short code based on the client firm name.';
  return codes.map(function(c) {
    return '- ' + c.clientCode + ' (' + (c.clientName || '') + ')' +
           (c.aliases ? ' — aliases: ' + c.aliases : '');
  }).join('\n');
}

function _addClientCode(clientCode, clientName, aliases) {
  try {
    var sheet = _getClientCodesSheet();
    sheet.appendRow([clientCode, clientName || '', aliases || '']);
    SpreadsheetApp.flush();
    _clientCodesCache = null;
    return true;
  } catch (err) {
    _logToSheet('_addClientCode ERROR: ' + err.message);
    return false;
  }
}


// ── NOTIFY PROJECTS SHEET ───────────────────────────────────────────────────
// Replaces notifyWix(): writes ONE row to the new "Projects" tab (single
// setValues call) instead of POSTing to Wix, mints a magic-link token, and
// returns the client-facing portal URL for the intake-notification email. If
// sheetRowIndex is already known (an existing row being re-notified), pass it
// in to update that row instead of appending a new one — otherwise leave it
// null/undefined to always append.

function notifyProjectsSheet(data, sheetRowIndex) {
  try {
    // ASHRAE 2025 outdoor design conditions for the proposal Specification block.
    if (!data.weatherData && data.projectAddress) {
      var _wx = getWeatherStationData(data.projectAddress);
      data.weatherData = _wx ? JSON.stringify(_wx) : '';
    }

    var sheet = _getProjectsSheet();
    var id = data._id || _generateRowId();

    var fields = {
      _id: id,
      legacy_wix_id: "",   // brand-new intake, no Wix predecessor
      createdDate: new Date().toISOString(),
      status: "Pending Review",

      title: data.projectFolder || data.projectName || "",
      propertyOwner: data.propertyOwner || "",
      projectAddress: data.projectAddress || "",
      projectCity: data.projectCity || "",
      projectState: data.projectState || "",
      projectZip: data.projectZip || "",
      projectCounty: data.projectCounty || "",
      clientName: ((data.clientFirst || "") + " " + (data.clientLast || "")).trim(),
      clientCompany: data.clientCompany || "",
      clientEmail: data.clientEmail || "",
      clientPhone: data.clientPhone || "",
      productService: data.productService || "",
      clientCode: data.clientCode || "",
      subClient: data.subClient || "",
      community: data.community || "",
      subdivision: data.subdivision || "",
      locationDisambig: data.locationDisambig || "",
      jobNo: data.jobNo || "",
      totalCost: data.totalCost || 0,
      sf: data.sf || 0,
      description: data.description || "",
      projectFolder: data.projectFolder || "",
      weatherData: data.weatherData || "",
      // Prefer whatever Claude read off the drawings; fall back to the ASHRAE
      // station lookup (_wx, computed above) already run for this address.
      latitude: data.latitude || (_wx && _wx.lat) || "",
      elevation: data.elevation || (_wx && _wx.elev) || "",
      osaLowDry: data.osaLowDry || (_wx && _wx.heatingDB99) || "",
      osaDailyRange: data.osaDailyRange || (_wx && _wx.hottestMonthDBRange) || "",
      indoorTemp: data.indoorTemp || "75",
      indoorRH: data.indoorRH || "50",
      numStories: data.numStories || "",

      buildingStatus: data.buildingStatus || "",
      occupancyType: data.occupancyType || "",
      lpdSpaceType: data.lpdSpaceType || "",
      orientation: data.orientation || "",
      occupants: data.occupants || 0,

      roofRValue: data.roofRValue || "",
      roofColor: data.roofColor || "",
      roofCover: data.roofCover || "",
      deckType: data.deckType || "",
      insulPosition: data.insulPosition || "",
      suspCeiling: data.suspCeiling || "",
      atticCond: data.atticCond || "",
      ceilingHeight: data.ceilingHeight || "",

      wallConstruction: data.wallConstruction || "",
      wallFinish: data.wallFinish || "",
      wallColor: data.wallColor || "",
      wallRValue: data.wallRValue || "",
      wallHeight: data.wallHeight || "",
      partConstruction: data.partConstruction || "",
      partRValue: data.partRValue || "",
      floorType: data.floorType || "",
      floorRValue: data.floorRValue || "",
      glassU: data.glassU || 0,
      glassSHGC: data.glassSHGC || 0,
      glassOperU: data.glassOperU || "",
      glassOperSHGC: data.glassOperSHGC || "",
      glassSGDU: data.glassSGDU || "",
      glassSGDSHGC: data.glassSGDSHGC || "",
      glassFrame: data.glassFrame || "",
      glazingType: data.glazingType || "",
      glazingTint: data.glazingTint || "",
      skylights: data.skylights || "",
      doorType: data.doorType || "",

      lightingWattsPerSF: data.lightingWattsPerSF || 0,
      equipWattsPerSF: data.equipWattsPerSF || "",
      heatGenEquipment: data.heatGenEquipment || "",
      infiltration: data.infiltration || "",
      changeRate: data.changeRate || "",

      acNewExisting: data.acNewExisting || "",
      acMounting: data.acMounting || "",
      systemType: data.systemType || "",
      hvacType: data.hvacType || "",
      heatType: data.heatType || "",
      coolingEff: data.coolingEff || "",
      heatingEff: data.heatingEff || "",
      efficiencyTier: data.efficiencyTier || "",
      manufacturer: data.manufacturer || "",
      hasOutsideAir: data.hasOutsideAir || "",
      hasExhaust: data.hasExhaust || "",
      hasStrip: data.hasStrip || "",
      heatStripCOP: data.heatStripCOP || "",

      hwType: data.hwType || "",
      hwEfficiency: data.hwEfficiency || "",
      hwCapacityGal: data.hwCapacityGal || "",

      extLightDescription: data.extLightDescription || "",
      extLightCategory: data.extLightCategory || "",
      extLightNumLuminaires: data.extLightNumLuminaires || "",
      extLightWattsPerLuminaire: data.extLightWattsPerLuminaire || "",
      extLightAreaLengthUnits: data.extLightAreaLengthUnits || "",
      extLightControlType: data.extLightControlType || "",

      driveFolderId: data.driveFolderId || "",
      driveFolderUrl: data.driveFolderUrl || "",
      snippetRoofRValue: data.snippetRoofRValue || "",
      snippetWallConstruction: data.snippetWallConstruction || "",
      snippetGlassValues: data.snippetGlassValues || "",
      snippetCeilingHeight: data.snippetCeilingHeight || "",
      snippetLightingWsf: data.snippetLightingWsf || "",
      snippetProjectAddress: data.snippetProjectAddress || "",
    };

    // Single row array, in SHEET_COLUMNS order — matches sheets_client.py's
    // _dict_to_row() exactly, so either side can read what the other wrote.
    var row = SHEET_COLUMNS.map(function (key) {
      return (key in fields) ? fields[key] : "";
    });

    var targetRow = sheetRowIndex || (sheet.getLastRow() + 1);
    sheet.getRange(targetRow, 1, 1, row.length).setValues([row]);
    SpreadsheetApp.flush();

    var token = _makePortalToken(id, 180);
    var portalUrl = PORTAL_BASE_URL + "/portal/" + token;
    _logToSheet("Sheet row " + (sheetRowIndex ? "updated" : "created") +
                " at row " + targetRow + " (id " + id + "); portal link minted.");
    return { ok: true, id: id, sheetRowIndex: targetRow, portalUrl: portalUrl };
  } catch (err) {
    _logToSheet("notifyProjectsSheet ERROR: " + err.message);
    return null;
  }
}


// ── HELPERS ───────────────────────────────────────────────────────────────────

function _getSheet() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  return ss.getSheetByName(TAB_NAME) || ss.getSheets()[0];
}

function _generateJobNo(companyOrName) {
  var initials = String(companyOrName).replace(/[^a-zA-Z\s]/g,'').split(/\s+/).map(function(w){return w.charAt(0).toUpperCase();}).join('').substring(0,4);
  var stamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyMMdd');
  return (initials||'UNK')+'-'+stamp;
}

function _parseEmail(fromStr) {
  var m = fromStr.match(/<([^>]+)>/);
  return m ? m[1] : fromStr.trim();
}

function _swapLabel(thread, removeLabel, addLabel) {
  try { thread.removeLabel(removeLabel); } catch(_) {}
  try { thread.addLabel(addLabel); } catch(_) {}
}

function _respond(status, message) {
  return ContentService.createTextOutput(JSON.stringify({ status: status, message: message })).setMimeType(ContentService.MimeType.JSON);
}

function _logToSheet(message) {
  try {
    var ss = SpreadsheetApp.openById(SHEET_ID);
    var log  = ss.getSheetByName("Script Log");
    if (!log) log = ss.insertSheet("Script Log");
    log.appendRow([new Date().toISOString(), message]);
  } catch (_) {}
}

// One-time: fill weatherData on existing Projects from the Sheet's addresses.
// Batched + resumable. Run it; if the log says "RUN AGAIN", run it again until
// it logs "BACKFILL COMPLETE".
//
// STILL HITS WIX: this and sendReviewEmailForProject() below are manual, run-
// from-the-editor utilities, not part of the automatic pipeline — left as-is
// during the Wix cleanup since they're not reachable from any live trigger,
// but they'll fail (or return stale data) since these Wix Velo functions may
// no longer be maintained. Needs its own Sheets-based rewrite if ever needed
// again.
function backfillWeatherData() {
  var listResp = UrlFetchApp.fetch(
    'https://www.adicotengineeringinc.com/_functions/projectsNeedingWeather',
    { muteHttpExceptions: true });
  var list = JSON.parse(listResp.getContentText());
  if (list.status !== 'ok') { Logger.log('list error: ' + listResp.getContentText().slice(0, 300)); return; }
  var items = list.items || [];
  Logger.log('Projects still needing weather: ' + list.total + ' (processing ' + items.length + ' this run)');

  var done = 0, noAddr = 0, noStation = 0, failed = 0;
  for (var k = 0; k < items.length; k++) {
    var it = items[k];
    if (!it.address || it.address.trim().length < 4) { noAddr++; continue; }
    var w = getWeatherStationData(it.address);
    if (!w) { noStation++; _logToSheet('backfill: no station for ' + (it.jobNo || it._id) + ' / ' + it.address); continue; }
    try {
      var resp = UrlFetchApp.fetch('https://www.adicotengineeringinc.com/_functions/setProjectWeather', {
        method: 'post', contentType: 'application/json',
        payload: JSON.stringify({ _id: it._id, weatherData: JSON.stringify(w) }),
        muteHttpExceptions: true });
      var r = JSON.parse(resp.getContentText());
      if (r.status === 'ok') done++; else { failed++; _logToSheet('backfill ' + r.status + ' for ' + (it.jobNo || it._id)); }
    } catch (e) { failed++; _logToSheet('backfill POST err ' + (it.jobNo || it._id) + ': ' + e.message); }
    Utilities.sleep(800);
  }
  Logger.log('Batch: updated=' + done + ' noAddress=' + noAddr + ' noStation=' + noStation + ' failed=' + failed +
             '. Remaining ≈ ' + (list.total - done) + '. RUN AGAIN until updated=0.');
}

// Manually fire the admin review notification email for a project added by
// hand in the Wix CMS. Pass the CMS record _id. Run from the editor.
// (See the WIX warning on backfillWeatherData above — same caveat applies.)
function sendReviewEmailForProject(projectId) {
  if (!projectId) { Logger.log('Pass a CMS _id.'); return; }
  try {
    var resp = UrlFetchApp.fetch(
      'https://www.adicotengineeringinc.com/_functions/getProject?id=' + encodeURIComponent(projectId),
      { muteHttpExceptions: true });
    var json = JSON.parse(resp.getContentText());
    if (json.status !== 'ok' || !json.project) {
      Logger.log('Project not found for _id: ' + projectId + ' — ' + resp.getContentText().substring(0, 200));
      return;
    }
    var p = json.project;
    var data = {
      jobNo:            p.jobNo            || '',
      projectName:      p.projectName      || '',
      projectFolder:    p.projectFolder    || '',
      clientName:       p.clientName        || ((p.clientFirstName||'') + ' ' + (p.clientLastName||'')).trim(),
      clientCompany:    p.clientCompany    || '',
      sf:               p.sf               || 0,
      productService:   p.productService   || '',
      dateReceived:     p.dateReceived     || Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'M/d/yyyy'),
      newClientFlag:    '',
      projectAddress:   p.projectAddress   || '',
      occupants:        p.occupants        || '',
      occupancyType:    p.occupancyType    || '',
      buildingStatus:   p.buildingStatus   || '',
      roofRValue:       p.roofRValue       || '',
      wallConstruction: p.wallConstruction || '',
      glassU:           p.glassU           || '',
      glassSHGC:        p.glassSHGC        || '',
      ceilingHeight:    p.ceilingHeight    || '',
      heatGenEquipment: p.heatGenEquipment || '',
      deckType:         p.deckType         || '',
      roofCover:        p.roofCover        || '',
      insulPosition:    p.insulPosition    || '',
      suspCeiling:      p.suspCeiling      || '',
      atticCond:        p.atticCond        || '',
      doorType:         p.doorType         || '',
    };
    _sendAdminReviewEmail(data, p._id);
    Logger.log('Review email sent for ' + (data.jobNo || data.projectFolder) + ' to ' + REVIEW_EMAIL);
  } catch (err) {
    Logger.log('sendReviewEmailForProject ERROR: ' + err.message);
  }
}

function runReviewEmailNow() {
  sendReviewEmailForProject('d75cc615-aa2f-4370-9705-12b3a745d304');
}
