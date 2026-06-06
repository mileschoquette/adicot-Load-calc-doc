// =============================================================================
// ADICOT PROJECTS — Google Apps Script
// =============================================================================
// Intake-to-proposal pipeline:
//   Gmail label -> Claude extraction -> Wix CMS + Sheet + Drive folder
//   -> static admin review NOTIFICATION email (button to hosted review page)
//   -> review/edit/approve on adicot.com page -> Gmail draft (questions|proposal)
//   -> client answers/sign -> status Active.
//
// NOTE: The AMP admin-review email has been retired. Review now happens on the
// hosted admin-review page (adicot.com, embedded #html1). This file sends a
// static notification email that links to that page. The page (via its Velo
// wrapper) writes the CMS record, then calls saveAndApprove here to create thep
// Gmail draft.
// =============================================================================


// ── CONFIGURATION ─────────────────────────────────────────────────────────────

const SHEET_ID      = "1wFV-0Z_Tswjuue0xfyVdZce_IZPjFwVdisvcZfOWcng";
const TAB_NAME      = "Adicot Projects";
const PORTAL_URL    = "https://www.adicotengineeringinc.com/projects";
const ADMIN_REVIEW_PAGE_URL = "https://www.adicotengineeringinc.com/admin-review"; // hosted review page (token-gated)
const ADMIN_EMAIL   = "admin@adicot.com";
const REVIEW_EMAIL  = "agc@adicot.com";
const SLACK_WEBHOOK = "REDACTED_SLACK_WEBHOOK"; // real value lives in the Apps Script editor — not committed

const INTAKE_LABEL    = "Projects/x-Estimate/Intake";
const PROCESSED_LABEL = "Projects/x-Estimate/PSR Ready";

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
// NOTE: The Sheet has no columns for roofCover, atticCond, or engagementDays.
// Those live in the Wix CMS only (Roof Cover / Attic Cond / Engagement Days),
// which the review page's Velo wrapper writes directly. saveAndApprove below
// mirrors to the Sheet only the fields that HAVE a column, and never overwrites
// ROOF_COLOR with a roof-covering value (the old collision bug).


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


// ── WOOLF / LENNAR REPEAT-PROJECT DEFAULTS ────────────────────────────────────
// Woolf Engineering sends Lennar repeat buildings (4/12/16/30-unit condos).
// The building geometry is fixed; only community (-> fenestration), gas (-> water
// heater), location, and orientation vary. These two tables mirror the Wix public
// files communityFenestration.js and waterHeaterLookup.js. Apps Script cannot
// import Wix public modules, so the data is duplicated here.
// ** Keep these in sync with the Wix public files when communities are added. **

const WOOLF_CLIENT_CODE = 'WLF';

var _GCI_SPARTA = {
  fixed: { u: 1.05, shgc: 0.37 },
  sh:    { u: 1.06, shgc: 0.30 },
  sgd:   { u: 1.07, shgc: 0.31 },
};

const COMMUNITY_FENESTRATION_GS = {
  'Babcock National':    _GCI_SPARTA,
  'Calusa Country Club': _GCI_SPARTA,
  'Heritage Landing':    _GCI_SPARTA,
  'Legends Cove':        _GCI_SPARTA,
  'Wellen Park Golf':    _GCI_SPARTA,
  'Ave Maria Coach': {
    fixed: { u: 0.59, shgc: 0.39 },
    sh:    { u: 0.70, shgc: 0.36 },
    sgd:   { u: 1.06, shgc: 0.34 },
  },
  'Sunwalk': {
    fixed: { u: 1.02, shgc: 0.29 },
    sh:    { u: 1.09, shgc: 0.29 },
    sgd:   { u: 1.05, shgc: 0.27 },
  },
};

// ESR short names -> canonical community key.
const COMMUNITY_ALIASES_GS = {
  'babcock': 'Babcock National',
};

const WATER_HEATER_GS = {
  electric: { fuel: 'Electric',     type: 'Storage tank',                       uef: 0.92, label: '50 gal electric storage water heater, 0.92 UEF' },
  gas:      { fuel: 'Natural gas',  type: 'Instantaneous (condensing tankless)', uef: 0.98, label: 'Rinnai RX199iN gas instantaneous water heater, 0.98 UEF' },
};

function _getCommunityFenestration(community) {
  if (!community) return null;
  var raw = String(community).trim();
  if (!raw) return null;
  var key = Object.keys(COMMUNITY_FENESTRATION_GS).filter(function(k) {
    return k.toLowerCase() === raw.toLowerCase();
  })[0];
  if (!key) {
    var alias = COMMUNITY_ALIASES_GS[raw.toLowerCase()];
    if (alias) key = alias;
  }
  if (!key) return null;
  var spec = COMMUNITY_FENESTRATION_GS[key];
  return { matchedKey: key, fixed: spec.fixed, sh: spec.sh, sgd: spec.sgd };
}

function _getWaterHeater(hasGas) {
  var yes = hasGas === true ||
    (typeof hasGas === 'string' && ['yes','y','true','gas'].indexOf(String(hasGas).trim().toLowerCase()) !== -1);
  return yes ? WATER_HEATER_GS.gas : WATER_HEATER_GS.electric;
}

// Applies Woolf community/gas lookups to the merged record IN PLACE.
// INPUT PRECEDENCE: only fills values the client/drawings did NOT provide.
// All filled values are assumptions to be verified on the admin review page.
function _applyWoolfDefaults(merged) {
  if (!merged || (merged.clientCode || '').trim() !== WOOLF_CLIENT_CODE) return merged;

  function _empty(v) { return v === null || v === undefined || v === '' || v === 0; }

  // Woolf buildings are condos -> Multiple-family LPD type.
  if (_empty(merged.lpdSpaceType)) merged.lpdSpaceType = 'Multiple-family';

  // Community -> fenestration (Fixed glazing governs the single glassU/glassSHGC).
  var fen = _getCommunityFenestration(merged.community);
  if (fen) {
    if (_empty(merged.glassU))    merged.glassU    = fen.fixed.u;
    if (_empty(merged.glassSHGC)) merged.glassSHGC = fen.fixed.shgc;
    merged.community = fen.matchedKey; // normalize to canonical name
  }

  // Gas flag -> water heater (feeds heatGenEquipment note + EC).
  var wh = _getWaterHeater(merged.hasGas);
  if (_empty(merged.heatGenEquipment)) merged.heatGenEquipment = wh.label;

  // Repeat client: no online sign loop; contract PDF + invoice at approve.
  merged.repeatClient = true;

  return merged;
}


// ── PROJECT NAMING CONVENTION ─────────────────────────────────────────────────

function buildProjectFolderName(clientCode, subClient, locationDisambig) {
  if (!clientCode) return '';
  var name = clientCode.trim();

  // ── Woolf (Lennar repeat buildings): [unit] Unit-COMM-Bldg [#] ──────────
  // subClient = unit type ("16 Unit"); locationDisambig = building # ("3200").
  // Parent nesting (1-job/woolf-[unit] unit/) is handled in createProjectFolder.
  if (name === WOOLF_CLIENT_CODE) {
    var unit = (subClient || '').trim();
    var bldg = (locationDisambig || '').trim();
    var wname = unit ? unit : 'Unit';
    wname += '-COMM';
    if (bldg) wname += '-Bldg ' + bldg;
    return wname;
  }

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

  // Woolf nests under 1-job/woolf-[unit] unit/ instead of 1-job/WLF/
  var clientFolder;
  if (clientCode.trim() === WOOLF_CLIENT_CODE) {
    var unitLabel = (subClient || '').trim().toLowerCase();   // "16 unit"
    var woolfParentName = 'woolf-' + (unitLabel || 'unit');   // "woolf-16 unit"
    clientFolder = _findOrCreateFolder(woolfParentName, jobFolderId);
  } else {
    clientFolder = _findOrCreateFolder(clientCode.trim(), jobFolderId);
  }
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
  var reviewUrl = ADMIN_REVIEW_PAGE_URL
    + '?id='    + encodeURIComponent(projectId || '')
    + '&jobNo=' + encodeURIComponent(jobNo)
    + '&mode=admin';

  var html  = _buildAdminNotificationHtml(data, reviewUrl);
  var plain = _adminNotifyPlain(data, reviewUrl);

  GmailApp.sendEmail(REVIEW_EMAIL, subject, plain, {
    htmlBody: html,
    name:     'Adicot Intake Pipeline',
  });
  _logToSheet('Admin review notification email sent for ' + jobNo + ' to ' + REVIEW_EMAIL);
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

function doPost(e) {
  try {
    _logToSheet('doPost called');
    var payload = JSON.parse(e.postData.contents);

    if (payload.action === 'saveAndApprove') {
      var result = _handleSaveAndApprove(payload);
      return _respond(result.status, result.message || '');
    }
    if (payload.action === 'createProposal') {
      var r1 = createClientDraft(payload.projectData, payload.adminFields, payload.clientFields);
      return _respond(r1.status, r1.message || '');
    }
    if (payload.action === 'createQuestionsEmail') {
      var r2 = createQuestionsEmailDraft(payload.projectData, payload.adminFields, payload.clientFields);
      return _respond(r2.status, r2.message || '');
    }
    if (payload.action === 'clientAnswers') {
      var r3 = handleClientAnswers(payload.projectData || payload);
      return _respond(r3.status, r3.message || '');
    }
    if (payload.action === 'clientSigned') {
      var r4 = handleClientSigned(payload);
      return _respond(r4.status, r4.message || '');
    }
    return _respond('error', 'Unknown action: ' + (payload.action || 'none'));
  } catch (err) {
    _logToSheet('doPost ERROR: ' + err.message);
    return _respond('error', err.message);
  }
}

// doGet retained only for health checks / legacy links. Review + approval now
// happen on the hosted page; the page's Velo wrapper calls saveAndApprove.
function doGet(e) {
  return HtmlService.createHtmlOutput(
    '<p style="font-family:Arial;padding:32px">Adicot intake endpoint is active. Review happens on the admin review page.</p>'
  );
}


// ── STEP 2 PIPELINE: GMAIL → CLAUDE → WIX CMS ────────────────────────────────

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

  // Woolf repeat-project defaults: community -> fenestration, gas -> water heater,
  // condo lpdSpaceType, repeatClient flag. Fills only what the client didn't give.
  _applyWoolfDefaults(merged);

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
    state:              merged.state           || '',
    county:             merged.county          || '',
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
    glassU:             merged.glassU             || null,
    glassSHGC:          merged.glassSHGC          || null,
    doorType:           merged.doorType           || '',
    lightingWattsPerSF: lightingWattsPerSF,
    orientation:        merged.orientation        || '',
    occupants:          merged.occupants          || null,
    ceilingHeight:      merged.ceilingHeight      || '',
    heatGenEquipment:   merged.heatGenEquipment   || '',
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

  try { appendProjectRow({ ...data, totalCost: 0 }); } catch(e2) { _logToSheet('appendProjectRow error: ' + e2.message); }

  var wixResult = notifyWix(data, null);
  var projectId = wixResult && wixResult.projectId ? wixResult.projectId : '';

  _swapLabel(thread, intakeLabel, processedLabel);

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
      '  - Architect/engineer firm name, contact name, phone, email',
      '  - Sheet scale, drawing date',
      '',
      'WOOLF ENGINEERING / LENNAR ESR FORM (Engineering Services Request):',
      '  - If this is a Woolf Engineering ESR (header "ENGINEERING SERVICES REQUEST", builder "LENNAR HOMES"),',
      '    the CLIENT is Woolf Engineering (clientCode "WLF"), NOT Lennar and NOT the property owner.',
      '  - subClient: the unit count as "[N] Unit" from MODEL NAME / PLAN NAME (e.g. "2-story / 16 PLEX" -> "16 Unit").',
      '  - locationDisambig: the LOT/BUILDING # value (e.g. "3200").',
      '  - community: the COMMUNITY field value (e.g. "Babcock").',
      '  - subdivision: the SUBDIVISION field value (e.g. "Webbs Reserve 2 story").',
      '  - hasGas: read the GAS check boxes — true if YES is checked, false if NO is checked.',
      '  - county: the COUNTY field. address: the ADDRESS field (project site address).',
      '  - These are condos — occupancyType "Multifamily condo".',
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
      '  - Glass U-factor (e.g. "U=0.28", "U-FACTOR: 0.35")',
      '  - Glass SHGC (e.g. "SHGC=0.25")',
      '  - If only glass type is listed (e.g. "SINGLE PANE CLEAR"), infer: single pane clear = U~1.04/SHGC~0.86; double pane clear = U~0.48/SHGC~0.76; double pane low-e = U~0.28/SHGC~0.25',
      '  - Door type: insulated metal, hollow metal, solid wood, storefront/glass',
      '',
      'EQUIPMENT SCHEDULE:',
      '  - List all heat-generating equipment with BTU/h or watts if shown',
      '  - Medical: dental chairs, autoclaves, sterilizers, compressors, imaging equipment',
      '  - Restaurant: fryers, griddles, ovens, ranges — note linear footage under hood',
      '  - Office: server rooms, lab equipment',
      '',
      'LIGHTING SCHEDULE / ELECTRICAL NOTES:',
      '  - Lighting watts per SF — may be stated directly or calculable from fixture schedule',
      '  - Only extract if explicitly stated or calculable from the drawings — do not infer',
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
      '  "hasGas": boolean,',
      '  "projectName": string,',
      '  "projectAddress": string,',
      '  "state": string,',
      '  "county": string,',
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
      '  "doorType": string,',
      '  "glassU": number,',
      '  "glassSHGC": number,',
      '  "lightingWattsPerSF": number,',
      '  "heatGenEquipment": string,',
      '  "description": string,',
      '  "_sources": { "<fieldName>": { "page": number, "bbox": [number, number, number, number] } }',
      '}',
      '',
      'Use null for any field not found or not inferable. Never return 0 for sf or occupants — use null if unknown.',
      'For lightingWattsPerSF: only return a value if explicitly stated or directly calculable from the drawings. Return null otherwise.',
      'For hasGas: return true/false only if the ESR GAS field is clearly checked; otherwise null.',
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
    'projectAddress','propertyOwner','state','county','clientFirst','clientLast',
    'clientPhone','clientEmail','clientCompany','productService'];

  var TECHNICAL_FIELDS = ['sf','occupancyType','buildingStatus','occupants','orientation','hasGas',
    'ceilingHeight','deckType','roofCover','insulPosition','suspCeiling','atticCond',
    'roofRValue','roofColor','wallConstruction','wallFinish','wallColor','wallRValue',
    'wallHeight','doorType','glassU','glassSHGC','lightingWattsPerSF','heatGenEquipment',
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
      // hasGas is boolean — treat only null/undefined as empty (false is a real value).
      if (tf === 'hasGas') {
        if ((merged.hasGas === null || merged.hasGas === undefined) &&
            (pdf.hasGas === true || pdf.hasGas === false)) merged.hasGas = pdf.hasGas;
        continue;
      }
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
      'WOOLF / LENNAR: If the email or a Woolf "ENGINEERING SERVICES REQUEST" form mentions Woolf Engineering with builder Lennar Homes, set clientCode "WLF". Pull these if present:',
      '  - subClient: unit count as "[N] Unit" (from "16 PLEX" / "2-story 16-unit" etc. -> "16 Unit").',
      '  - locationDisambig: the LOT/BUILDING # (e.g. "3200").',
      '  - community: the community name (e.g. "Babcock"). subdivision: the subdivision (e.g. "Webbs Reserve 2 story").',
      '  - hasGas: true if gas service, false if no gas, null if unstated. These are condos (occupancyType "Multifamily condo").',
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
      '  "hasGas": boolean,',
      '  "projectName": string,',
      '  "projectAddress": string,',
      '  "state": string,',
      '  "county": string,',
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

// ── CREATE CLIENT (PROPOSAL) GMAIL DRAFT ──────────────────────────────────────

function createClientDraft(projectData, adminFields, clientFieldKeys) {
  try {
    var clientEmail = projectData.clientEmail;
    if (!clientEmail) return { status: 'error', message: 'No client email in project data.' };
    var merged     = Object.assign({}, projectData, adminFields || {});
    var firstName  = (merged.clientName || merged.clientFirst || '').split(/\s+/)[0] || 'there';
    var jobNum     = merged.jobNo || merged.jobNumber || '';
    var projName   = merged.title || merged.projectName || jobNum;
    var portalLink = _buildClientPageLink(merged, true);
    var emailHtml  = _buildClientSummaryEmail(merged, firstName, jobNum, projName, portalLink, true, clientFieldKeys || []);
    var plain      = _clientSummaryPlain(firstName, projName, portalLink, true);
    var subject    = jobNum + ' · ' + projName + ' — your proposal';
    GmailApp.createDraft(clientEmail, subject, plain, {
      htmlBody: emailHtml,
      name:     'Adrienne Gould-Choquette, PE',
      replyTo:  Session.getActiveUser().getEmail(),
    });
    _updateSheetStatus(merged.jobNo, 'Draft Created');
    return { status: 'ok' };
  } catch (err) {
    _logToSheet('createClientDraft ERROR: ' + err.message);
    return { status: 'error', message: err.toString() };
  }
}

function _buildClientPageLink(data, hasQuote) {
  var id    = data._id    || data.projectId || '';
  var jobNo = data.jobNo  || data.jobNumber || '';
  var base  = ADMIN_REVIEW_PAGE_URL + '?mode=client';
  if (id)         base += '&id='    + encodeURIComponent(id);
  else if (jobNo) base += '&jobNo=' + encodeURIComponent(jobNo);
  if (hasQuote)   base += '&quote=1';
  return base;
}

function _buildPortalLink(data) {
  var id    = data._id    || data.projectId || '';
  var jobNo = data.jobNo  || data.jobNumber || '';
  var base;
  if (id)         base = PORTAL_URL + '?_id='   + encodeURIComponent(id);
  else if (jobNo) base = PORTAL_URL + '?jobNo=' + encodeURIComponent(jobNo);
  else            base = PORTAL_URL;
  var cost = data.totalCost || '';
  if (cost) base += '&totalCost=' + encodeURIComponent(String(cost));
  return base;
}

function _buildClientEmailHtml(data, portalLink, clientFieldKeys) {
  var firstName   = (data.clientName || data.clientFirst || '').split(' ')[0] || 'there';
  var projectName = data.title || data.projectName || 'Your Project';
  var address     = data.projectAddress || '';
  var service     = data.productService || 'HVAC Engineering Services';
  var fee         = data.totalCost ? '$' + Number(data.totalCost).toLocaleString() : 'Per proposal';
  var qNote       = clientFieldKeys.length > 0
    ? '<p style="margin:0 0 6px;font-size:13px;color:#888;">A few quick questions are included — should only take a minute.</p>'
    : '';
  return '<!DOCTYPE html><html><head><meta charset="UTF-8"></head>' +
  '<body style="margin:0;padding:0;background:#FAFAF7;font-family:Arial,sans-serif;">' +
  '<table width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF7;padding:32px 16px;"><tr><td align="center">' +
  '<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden;border:1px solid #D4D0C2;max-width:560px;">' +
  '<tr><td style="background:#2C2C2A;padding:20px 28px;">' +
  '<p style="margin:0;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#E8740A;">Adicot, Inc.</p>' +
  '<p style="margin:6px 0 0;font-size:16px;font-weight:500;color:#fff;">Your proposal is ready, ' + firstName + '.</p>' +
  '</td></tr>' +
  '<tr><td style="padding:24px 28px 0;">' +
  '<table width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF7;border:1px solid #EDE8E1;border-radius:8px;">' +
  '<tr><td style="padding:14px 18px;border-bottom:1px solid #EDE8E1;">' +
  '<p style="margin:0;font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:#888;">Project</p>' +
  '<p style="margin:4px 0 0;font-size:14px;font-weight:500;color:#2C2C2A;">' + projectName + '</p>' +
  (address ? '<p style="margin:2px 0 0;font-size:12px;color:#888;">' + address + '</p>' : '') +
  '</td></tr>' +
  '<tr><td style="padding:14px 18px;border-bottom:1px solid #EDE8E1;">' +
  '<p style="margin:0;font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:#888;">Service</p>' +
  '<p style="margin:4px 0 0;font-size:14px;color:#444441;">' + service + '</p>' +
  '</td></tr>' +
  '<tr><td style="padding:14px 18px;">' +
  '<p style="margin:0;font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:#888;">Fee</p>' +
  '<p style="margin:4px 0 0;font-size:20px;font-weight:500;color:#2C2C2A;">' + fee + '</p>' +
  '</td></tr></table></td></tr>' +
  '<tr><td style="padding:22px 28px 0;">' +
  '<p style="margin:0 0 10px;font-size:14px;color:#444441;line-height:1.6;">Please review the full proposal and work order in the portal, complete any remaining fields, and sign to get started.</p>' +
  qNote + '</td></tr>' +
  '<tr><td style="padding:20px 28px 28px;">' +
  '<a href="' + portalLink + '" style="display:inline-block;background:#E8740A;color:#fff;text-decoration:none;font-size:14px;font-weight:500;padding:12px 28px;border-radius:8px;">Review &amp; Sign &#8594;</a>' +
  '</td></tr>' +
  '<tr><td style="padding:16px 28px;border-top:1px solid #EDE8E1;background:#FAFAF7;">' +
  '<p style="margin:0;font-size:11px;color:#aaa;line-height:1.6;">Adrienne Gould-Choquette, PE &nbsp;&middot;&nbsp; Adicot, Inc.<br>Questions? Reply to this email.</p>' +
  '</td></tr>' +
  '</table></td></tr></table></body></html>';
}

function _updateSheetStatus(jobNo, newStatus) {
  if (!jobNo) return;
  try {
    var sheet = _getSheet();
    var data  = sheet.getDataRange().getValues();
    for (var i = 1; i < data.length; i++) {
      if (String(data[i][COL.JOB_NO - 1]) === String(jobNo)) {
        sheet.getRange(i + 1, COL.STATUS).setValue(newStatus);
        SpreadsheetApp.flush();
        break;
      }
    }
  } catch (err) { _logToSheet('_updateSheetStatus ERROR: ' + err.message); }
}


// ── CREATE QUESTIONS (NEED-MORE-INFO) EMAIL DRAFT ─────────────────────────────

function createQuestionsEmailDraft(projectData, adminFields, clientFieldKeys) {
  try {
    var p = Object.assign({}, projectData, adminFields || {});
    if (!p.clientEmail) return { status: 'error', message: 'No client email in project data.' };
    var firstName = (p.clientName || p.clientFirst || '').split(/\s+/)[0] || 'there';
    var jobNum    = p.jobNo || p.jobNumber || '';
    var projName  = p.title || p.projectName || jobNum;
    var subject   = jobNum + ' · ' + projName + ' — a few quick questions';
    var pageLink  = _buildClientPageLink(p, false); // questions mode (no quote)
    var html      = _buildClientSummaryEmail(p, firstName, jobNum, projName, pageLink, false, clientFieldKeys || []);
    var plain     = _clientSummaryPlain(firstName, projName, pageLink, false);
    GmailApp.createDraft(p.clientEmail, subject, plain, {
      htmlBody: html,
      name:     'Adrienne Gould-Choquette, PE',
      replyTo:  Session.getActiveUser().getEmail(),
    });
    _updateSheetStatus(jobNum, 'Questions Draft Created');
    _logToSheet('createQuestionsEmailDraft (static): draft created for ' + p.clientEmail);
    return { status: 'ok' };
  } catch (err) {
    _logToSheet('createQuestionsEmailDraft ERROR: ' + err.message);
    return { status: 'error', message: err.toString() };
  }
}

// ── STATIC CLIENT EMAIL (summary + button to the client page) ────────────────
// Used for BOTH the need-more-info (hasQuote=false) and proposal (hasQuote=true)
// emails. Styled to match the admin notification. No AMP.

function _clientSummaryPlain(firstName, projName, pageLink, hasQuote) {
  var lines = ['Hi ' + firstName + ',', ''];
  if (hasQuote) {
    lines.push('Your proposal for ' + projName + ' is ready to review and sign.');
  } else {
    lines.push('We pulled most of what we need from your drawings for ' + projName + '.');
    lines.push('A few items still need your input before we can finalize your quote.');
  }
  lines.push('');
  lines.push('Open your project page to review the details, make any corrections, and ' + (hasQuote ? 'sign:' : 'answer the open questions:'));
  lines.push(pageLink);
  lines.push('');
  lines.push('—');
  lines.push('Adrienne Gould-Choquette, PE');
  lines.push('Adicot, Inc.');
  lines.push('agc@adicot.com');
  return lines.join('\n');
}

function _buildClientSummaryEmail(p, firstName, jobNum, projName, pageLink, hasQuote, clientFieldKeys) {
  var e = function(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); };

  // FULL field set — every building parameter the page shows. Each field that is
  // filled goes to the Confirmed section; each blank field goes to Needs-your-input.
  // (Glass U & SHGC are combined into one row.)
  var ALL_DEFS = [
    ['projectAddress','Project address'],
    ['buildingStatus','Building status'],
    ['sf','Approx. area'],
    ['occupants','Occupants'],
    ['orientation','Orientation'],
    ['roofRValue','Roof R-value'],
    ['roofColor','Roof color'],
    ['deckType','Roof deck type'],
    ['roofCover','Roof covering'],
    ['suspCeiling','Suspended ceiling'],
    ['atticCond','Attic / plenum'],
    ['wallConstruction','Wall construction'],
    ['wallHeight','Exterior wall height'],
    ['glassU','Glass U / SHGC'],
    ['heatGenEquipment','Heat-gen equipment'],
    ['acNewExisting','AC new / existing'],
    ['acMounting','AC mounting'],
    ['hvacType','System type'],
    ['heatType','Heat type'],
    ['lightingWattsPerSF','Lighting W/SF'],
    ['ceilingHeight','Ceiling height']
  ];
  function has(k){
    if(k==='glassU') return (p.glassU!==undefined&&p.glassU!==null&&p.glassU!==''&&p.glassU!==0)&&(p.glassSHGC!==undefined&&p.glassSHGC!==null&&p.glassSHGC!==''&&p.glassSHGC!==0);
    var v=p[k]; return v!==undefined && v!==null && v!=='' && v!==0;
  }
  var confirmedRows = ALL_DEFS.filter(function(d){return has(d[0]);});
  var missingRows   = ALL_DEFS.filter(function(d){return !has(d[0]);});

  function rowHtml(label, val, kind){
    var dot = kind==='ok' ? '#2D6A2D' : '#E8740A';
    var valTxt = kind==='ok' ? e(val) : 'Needs your input';
    var valColor = kind==='ok' ? '#2C2C2A' : '#C05C0A';
    return '<tr><td style="padding:9px 16px;border-bottom:1px solid #EDE8E1;font-size:12px;color:#444441;">' +
      '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:'+dot+';margin-right:8px;"></span>' +
      e(label) + '</td>' +
      '<td style="padding:9px 16px;border-bottom:1px solid #EDE8E1;font-size:12px;color:'+valColor+';text-align:right;font-weight:500;">'+valTxt+'</td></tr>';
  }
  function valOf(k){
    if(k==='glassU') return (p.glassU&&p.glassSHGC)?('U '+p.glassU+' / SHGC '+p.glassSHGC):(p.glassU||'');
    return p[k];
  }

  var confHtml = confirmedRows.map(function(d){return rowHtml(d[1], valOf(d[0]), 'ok');}).join('');
  var missHtml = missingRows.map(function(d){return rowHtml(d[1], '', 'miss');}).join('');

  var feeBlock = '';
  if (hasQuote && p.totalCost) {
    feeBlock =
      '<table width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF7;border:1px solid #EDE8E1;border-radius:8px;margin-bottom:18px;">' +
      '<tr><td style="padding:13px 16px;border-bottom:1px solid #EDE8E1;"><span style="font-size:11px;color:#9A9A9A;text-transform:uppercase;letter-spacing:.06em;">Service</span><br><span style="font-size:13px;color:#2C2C2A;font-weight:500;">'+e(p.productService||'Engineering services')+'</span></td></tr>' +
      '<tr><td style="padding:13px 16px;"><span style="font-size:11px;color:#9A9A9A;text-transform:uppercase;letter-spacing:.06em;">Fee</span><br><span style="font-size:22px;color:#2C2C2A;font-weight:600;">$'+Number(p.totalCost).toLocaleString()+'</span>'+(p.engagementDays?'<span style="font-size:12px;color:#9A9A9A;"> &nbsp;&middot;&nbsp; ~'+e(p.engagementDays)+' days</span>':'')+'</td></tr>' +
      '</table>';
  }

  var intro = hasQuote
    ? 'Your proposal is ready. Review the scope and fee below, make any corrections on your project page, then accept and sign.'
    : 'We pulled most of what we need from your drawings. A few items still need your input before we can finalize your quote.';
  var btnLabel = hasQuote ? 'Review &amp; Sign &rarr;' : 'Review &amp; Answer &rarr;';
  var headerTag = hasQuote ? 'Your proposal' : 'A few quick questions';

  var missingSection = missingRows.length
    ? '<p style="margin:18px 0 8px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#C05C0A;">Needs your input</p>' +
      '<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #EDE8E1;border-radius:8px;overflow:hidden;">'+missHtml+'</table>'
    : '';
  var confirmedSection = confirmedRows.length
    ? '<p style="margin:18px 0 8px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#2D6A2D;">Confirmed from your drawings</p>' +
      '<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #EDE8E1;border-radius:8px;overflow:hidden;">'+confHtml+'</table>'
    : '';

  return '<!DOCTYPE html><html><head><meta charset="UTF-8"></head>' +
  '<body style="margin:0;padding:0;background:#FAFAF7;font-family:Arial,Helvetica,sans-serif;">' +
  '<table width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF7;padding:28px 12px;"><tr><td align="center">' +
  '<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;max-width:560px;border:1px solid #D4D0C2;">' +

  '<tr><td style="background:#2C2C2A;padding:20px 24px;">' +
  '<p style="margin:0;font-size:10px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:#E8740A;">Adicot, Inc. &mdash; '+e(headerTag)+'</p>' +
  '<p style="margin:6px 0 0;font-size:16px;color:#fff;font-weight:400;">Hi '+e(firstName)+'.</p>' +
  '<p style="margin:3px 0 0;font-size:12px;color:#D4D0C2;">'+e(jobNum)+' &middot; '+e(projName)+'</p>' +
  '</td></tr>' +

  '<tr><td style="padding:22px 24px 0;">' +
  '<p style="margin:0 0 16px;font-size:14px;color:#444441;line-height:1.6;">'+intro+'</p>' +
  feeBlock +
  missingSection +
  confirmedSection +
  '</td></tr>' +

  '<tr><td style="padding:22px 24px 24px;">' +
  '<a href="'+pageLink+'" style="display:inline-block;background:#E8740A;color:#fff;text-decoration:none;font-size:14px;font-weight:600;padding:13px 28px;border-radius:8px;">'+btnLabel+'</a>' +
  '<p style="margin:12px 0 0;font-size:11px;color:#9A9A9A;line-height:1.6;">You can review everything, see the source snippets from your drawings, and make corrections right on the page.</p>' +
  '</td></tr>' +

  '<tr><td style="padding:16px 24px;border-top:1px solid #EDE8E1;background:#FAFAF7;">' +
  '<p style="margin:0;font-size:11px;color:#9A9A9A;line-height:1.6;">Adrienne Gould-Choquette, PE &middot; Adicot, Inc.<br>Questions? Reply to this email.</p>' +
  '</td></tr>' +

  '</table></td></tr></table></body></html>';
}

function _esc(s)     { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function _escAttr(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }


// ── APPEND A NEW PROJECT ROW ──────────────────────────────────────────────────

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

var _clientCodesCache = null;

function _getClientCodes() {
  if (_clientCodesCache) return _clientCodesCache;
  try {
    var resp = UrlFetchApp.fetch(
      'https://www.adicotengineeringinc.com/_functions/clientCodes',
      { muteHttpExceptions: true }
    );
    _clientCodesCache = JSON.parse(resp.getContentText()) || [];
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
    var resp = UrlFetchApp.fetch(
      'https://www.adicotengineeringinc.com/_functions/addClientCode',
      {
        method: 'post', contentType: 'application/json',
        payload: JSON.stringify({ clientCode: clientCode, clientName: clientName, aliases: aliases }),
        muteHttpExceptions: true,
      }
    );
    var r = JSON.parse(resp.getContentText());
    _clientCodesCache = null;
    return r.status === 'added';
  } catch (err) {
    _logToSheet('_addClientCode ERROR: ' + err.message);
    return false;
  }
}


// ── NOTIFY WIX ────────────────────────────────────────────────────────────────

function notifyWix(data, sheetRowIndex) {
  try {
    var response = UrlFetchApp.fetch(
      "https://www.adicotengineeringinc.com/_functions/createProjectAndMember",
      {
        method: "post", contentType: "application/json",
        payload: JSON.stringify({
          clientEmail:      data.clientEmail    || "",
          clientFirstName:  data.clientFirst    || "",
          clientLastName:   data.clientLast     || "",
          clientPhone:      data.clientPhone    || "",
          clientCompany:    data.clientCompany  || "",
          projectName:      data.projectName    || "",
          projectFolder:    data.projectFolder  || "",
          clientCode:       data.clientCode     || "",
          subClient:        data.subClient      || "",
          locationDisambig: data.locationDisambig || "",
          community:        data.community      || "",
          subdivision:      data.subdivision    || "",
          repeatClient:     data.repeatClient   || false,
          lpdSpaceType:     data.lpdSpaceType   || "",
          projectAddress:   data.projectAddress || "",
          propertyOwner:    data.propertyOwner  || "",
          jobNo:            data.jobNo          || "",
          totalCost:        data.totalCost      || 0,
          sf:               data.sf             || 0,
          productService:   data.productService || "",
          status:           "Pending Review",
          description:      data.description   || "",
          sheetRowIndex:    sheetRowIndex,
          buildingStatus:   data.buildingStatus    || "",
          occupancyType:    data.occupancyType     || "",
          orientation:      data.orientation       || "",
          occupants:        data.occupants         || 0,
          roofRValue:       data.roofRValue        || "",
          roofColor:        data.roofColor         || "",
          roofCover:        data.roofCover         || "",
          deckType:         data.deckType          || "",
          insulPosition:    data.insulPosition     || "",
          suspCeiling:      data.suspCeiling       || "",
          atticCond:        data.atticCond         || "",
          wallConstruction: data.wallConstruction  || "",
          wallFinish:       data.wallFinish        || "",
          wallRValue:       data.wallRValue        || "",
          wallHeight:       data.wallHeight        || "",
          glassU:           data.glassU            || 0,
          glassSHGC:        data.glassSHGC         || 0,
          doorType:         data.doorType          || "",
          ceilingHeight:    data.ceilingHeight     || "",
          lightingWattsPerSF: data.lightingWattsPerSF || 0,
          heatGenEquipment: data.heatGenEquipment  || "",
          driveFolderId:    data.driveFolderId     || "",
          driveFolderUrl:   data.driveFolderUrl    || "",
          snippetMap:              data.snippetMap              || "",
          snippetRoofRValue:       data.snippetRoofRValue       || "",
          snippetWallConstruction: data.snippetWallConstruction || "",
          snippetGlassValues:      data.snippetGlassValues      || "",
          snippetCeilingHeight:    data.snippetCeilingHeight     || "",
          snippetLightingWsf:      data.snippetLightingWsf      || "",
          snippetProjectAddress:   data.snippetProjectAddress    || "",
        }),
        muteHttpExceptions: true,
      }
    );
    var result = JSON.parse(response.getContentText());
    if (result.projectId) _logToSheet("Wix project created: " + result.projectId);
    return result;
  } catch (err) { _logToSheet("notifyWix ERROR: " + err.message); return null; }
}


// ── saveAndApprove (called by the admin review page's Velo wrapper) ──────────
// Mirrors edited fields to the Sheet (only fields that HAVE a column; the CMS
// is written by the Velo wrapper and holds the complete record), then creates
// the Gmail draft (questions | proposal). Nothing sends automatically.

function _handleSaveAndApprove(payload) {
  try {
    var jobNo = payload.jobNo || '';
    var pid   = payload.pid   || payload.projectId || '';
    var mode  = (payload.mode || 'questions').toLowerCase();

    var num = function(v) { var n = parseFloat(String(v == null ? '' : v).replace(/[$,]/g, '')); return isNaN(n) ? null : n; };

    var d = {
      jobNo:            jobNo,
      _id:              pid,
      // BUG 3 FIX: fall back to title or jobNo so the project name is never blank
      // in the email subject or sheet mirror when projectFolder isn't sent.
      projectName:      payload.projectFolder || payload.title || payload.jobNo || '',
      projectFolder:    payload.projectFolder || payload.title || payload.jobNo || '',
      projectAddress:   payload.projectAddress || '',
      clientName:       payload.clientName  || '',
      clientEmail:      payload.clientEmail || '',
      sf:               num(payload.sf),
      occupants:        num(payload.occupants),
      occupancyType:    payload.occupancyType   || '',
      buildingStatus:   payload.buildingStatus  || '',
      orientation:      payload.orientation     || '',
      roofRValue:       payload.roofRValue      || '',
      roofColor:        payload.roofColor       || '',
      roofCover:        payload.roofCover       || '',
      deckType:         payload.deckType        || '',
      insulPosition:    payload.insulPosition   || '',
      suspCeiling:      payload.suspCeiling     || '',
      atticCond:        payload.atticCond       || '',
      wallConstruction: payload.wallConstruction|| '',
      wallHeight:       payload.wallHeight      || '',
      glassU:           num(payload.glassU),
      glassSHGC:        num(payload.glassSHGC),
      ceilingHeight:    payload.ceilingHeight   || '',
      lightingWattsPerSF: payload.lightingWattsPerSF || '',
      heatGenEquipment: payload.heatGenEquipment || '',
      acNewExisting:    payload.acNewExisting   || '',
      acMounting:       payload.acMounting      || '',
      systemType:       payload.systemType      || '',
      heatType:         payload.heatType        || '',
      doorType:         payload.doorType        || '',
      productService:   payload.productService  || '',
      totalCost:        num(payload.totalCost),
      // for the questions email (insulPosition/atticCond feed the confirmed block)
      glassValues:      (payload.glassU && payload.glassSHGC) ? ('U = ' + payload.glassU + ' · SHGC = ' + payload.glassSHGC) : '',
    };

    // ── Mirror to the Sheet (only columns that exist) ──
    try {
      var sheet = _getSheet();
      var rows  = sheet.getDataRange().getValues();
      for (var i = 1; i < rows.length; i++) {
        if (String(rows[i][COL.JOB_NO-1]).trim() === jobNo.trim()) {
          var r = i + 1;
          var put = function(col, v) { if (v !== null && v !== undefined && v !== '') sheet.getRange(r, col).setValue(v); };

          if (d.projectFolder)    put(COL.PROJECT_NAME,      d.projectFolder);
          put(COL.PROJECT_ADDRESS, d.projectAddress);
          put(COL.SF,              d.sf);
          put(COL.OCCUPANCY,       d.occupancyType);
          put(COL.OCCUPANTS,       d.occupants);
          put(COL.BUILDING_STATUS, d.buildingStatus);
          put(COL.ORIENTATION,     d.orientation);
          put(COL.ROOF_R_VALUE,    d.roofRValue);
          put(COL.ROOF_COLOR,      d.roofColor);       // ONLY the true roof COLOR — never roofCover
          put(COL.ROOF_DECK_TYPE,  d.deckType);
          put(COL.ROOF_INSUL_POS,  d.insulPosition);
          put(COL.ROOF_SUSP_CEIL,  d.suspCeiling);
          put(COL.WALL_CONSTRUCTION, d.wallConstruction);
          put(COL.WALL_HEIGHT,     d.wallHeight);
          put(COL.GLASS_FIXED_U,   d.glassU);
          put(COL.GLASS_FIXED_SHGC,d.glassSHGC);
          put(COL.CEIL_HEIGHT,     d.ceilingHeight);
          put(COL.LIGHTING_WPF,    d.lightingWattsPerSF);
          put(COL.HEAT_GEN_EQUIP,  d.heatGenEquipment);
          put(COL.AC_NEW_EXISTING, d.acNewExisting);
          put(COL.AC_MOUNTING,     d.acMounting);
          put(COL.DOOR_TYPE,       d.doorType);
          put(COL.PRODUCT_SERVICE, d.productService);
          put(COL.TOTAL_COST,      d.totalCost);
          // roofCover, atticCond, engagementDays have NO Sheet column — CMS only.
          sheet.getRange(r, COL.STATUS).setValue('Pending Client Approval');
          SpreadsheetApp.flush();
          break;
        }
      }
    } catch (sheetErr) {
      _logToSheet('saveAndApprove sheet-mirror error: ' + sheetErr.message);
    }

    // ── Resolve client email from CMS if missing ──
    // Always look up by jobNo since pid is the jobNo string, not a Wix _id.
    if (!d.clientEmail || !d._id || d._id === jobNo) {
      try {
        var lookupParam = 'jobNo=' + encodeURIComponent(jobNo);
        var wixResp = UrlFetchApp.fetch(
          'https://www.adicotengineeringinc.com/_functions/getProject?' + lookupParam,
          { muteHttpExceptions: true, followRedirects: true }
        );
        _logToSheet('getProject status: ' + wixResp.getResponseCode());
        _logToSheet('getProject body: ' + wixResp.getContentText().substring(0, 200));
        var wixJson = JSON.parse(wixResp.getContentText());
        d.clientEmail = wixJson.clientEmail || '';
        d.clientName  = wixJson.clientName  || d.clientName;
        d._id         = wixJson._id         || d._id;
        _logToSheet('resolved clientEmail from CMS: ' + (d.clientEmail || 'still missing'));
      } catch(fetchErr) {
        _logToSheet('clientEmail CMS lookup failed: ' + fetchErr.message);
      }
    }

    // ── Create the Gmail draft ──
    var result;
    if (mode === 'proposal') {
      result = createClientDraft(d, {}, []);
    } else {
      var missingFields = ['deckType','roofCover','insulPosition','suspCeiling','atticCond','doorType'];
      var clientFields = missingFields.filter(function(f) { return !d[f]; });
      result = createQuestionsEmailDraft(d, {}, clientFields);
    }

    if (result.status !== 'ok') {
      return { status: 'error', message: result.message || 'Draft creation failed' };
    }
    _logToSheet('saveAndApprove: draft created for ' + jobNo + ' mode=' + mode);
    return { status: 'ok', message: (mode === 'proposal' ? 'Proposal' : 'Questions email') + ' draft created — check Gmail.' };

  } catch (err) {
    _logToSheet('_handleSaveAndApprove ERROR: ' + err.message);
    return { status: 'error', message: err.message };
  }
}


// ── HANDLE CLIENT SIGNED ──────────────────────────────────────────────────────

function handleClientSigned(payload) {
  try {
    var d = payload, jobNo = d.projectId || '', sheet = _getSheet(), row = _findRowByProjectName(sheet, jobNo);
    if (row !== -1) {
      if (d.approxArea)        sheet.getRange(row, COL.SF).setValue(d.approxArea);
      if (d.occupants)         sheet.getRange(row, COL.OCCUPANTS).setValue(d.occupants);
      if (d.orientation)       sheet.getRange(row, COL.ORIENTATION).setValue(d.orientation);
      if (d.buildingStatus)    sheet.getRange(row, COL.BUILDING_STATUS).setValue(d.buildingStatus);
      if (d.roofDeckType)      sheet.getRange(row, COL.ROOF_DECK_TYPE).setValue(d.roofDeckType);
      if (d.roofInsulPosition) sheet.getRange(row, COL.ROOF_INSUL_POS).setValue(d.roofInsulPosition);
      if (d.roofSuspCeil)      sheet.getRange(row, COL.ROOF_SUSP_CEIL).setValue(d.roofSuspCeil);
      if (d.roofColor)         sheet.getRange(row, COL.ROOF_COLOR).setValue(d.roofColor);
      if (d.roofRValue)        sheet.getRange(row, COL.ROOF_R_VALUE).setValue(d.roofRValue);
      if (d.ceilingHeight)     sheet.getRange(row, COL.CEIL_HEIGHT).setValue(d.ceilingHeight);
      if (d.wallFinish)        sheet.getRange(row, COL.WALL_FINISH).setValue(d.wallFinish);
      if (d.wallConstruction)  sheet.getRange(row, COL.WALL_CONSTRUCTION).setValue(d.wallConstruction);
      if (d.wallColor)         sheet.getRange(row, COL.WALL_COLOR).setValue(d.wallColor);
      if (d.wallRValue)        sheet.getRange(row, COL.WALL_R_VALUE).setValue(d.wallRValue);
      if (d.wallHeight)        sheet.getRange(row, COL.WALL_HEIGHT).setValue(d.wallHeight);
      if (d.glassFixedU)       sheet.getRange(row, COL.GLASS_FIXED_U).setValue(d.glassFixedU);
      if (d.glassFixedSHGC)    sheet.getRange(row, COL.GLASS_FIXED_SHGC).setValue(d.glassFixedSHGC);
      if (d.glassOperU)        sheet.getRange(row, COL.GLASS_OPER_U).setValue(d.glassOperU);
      if (d.glassOperSHGC)     sheet.getRange(row, COL.GLASS_OPER_SHGC).setValue(d.glassOperSHGC);
      if (d.doorType)          sheet.getRange(row, COL.DOOR_TYPE).setValue(d.doorType);
      if (d.lightingWpf)       sheet.getRange(row, COL.LIGHTING_WPF).setValue(d.lightingWpf);
      if (d.heatGenEquip)      sheet.getRange(row, COL.HEAT_GEN_EQUIP).setValue(d.heatGenEquip);
      if (d.acNewExisting)     sheet.getRange(row, COL.AC_NEW_EXISTING).setValue(d.acNewExisting);
      if (d.acMounting)        sheet.getRange(row, COL.AC_MOUNTING).setValue(d.acMounting);
      if (d.projectNotes)      sheet.getRange(row, COL.PROJECT_NOTES).setValue(d.projectNotes);
      sheet.getRange(row, COL.STATUS).setValue('Current Work');
      SpreadsheetApp.flush();
    }
    postToSlack(null, [
      { type: 'header', text: { type: 'plain_text', text: 'Client signed — ' + jobNo } },
      { type: 'section', fields: [
        { type: 'mrkdwn', text: '*Services:*\n' + (d.services || '—') },
        { type: 'mrkdwn', text: '*Area:*\n' + (d.approxArea || '—') + ' SF' },
        { type: 'mrkdwn', text: '*Occupancy:*\n' + (d.occupancyType || '—') },
        { type: 'mrkdwn', text: '*Signed:*\n' + (d.signedDate || new Date().toISOString()) },
      ]},
    ]);
    GmailApp.sendEmail(ADMIN_EMAIL, 'Client signed — ' + jobNo, 'Work order signed.\n\nProject: ' + jobNo + '\nServices: ' + (d.services||'—') + '\nArea: ' + (d.approxArea||'—') + ' SF\nSigned: ' + (d.signedDate||'—'));
    _logToSheet('handleClientSigned: ' + jobNo);
    return { status: 'ok' };
  } catch (err) { _logToSheet('handleClientSigned ERROR: ' + err.message); return { status: 'error', message: err.toString() }; }
}


// ── HANDLE CLIENT ANSWERS ─────────────────────────────────────────────────────

function handleClientAnswers(data) {
  try {
    var sheet = _getSheet(), jobNo = data.project || '', row = _findRowByProjectName(sheet, jobNo);
    if (row === -1) { _logToSheet('clientAnswers — no row: ' + jobNo); return { status: 'error', message: 'Project not found' }; }
    if (data.totalSF)        sheet.getRange(row, COL.SF).setValue(data.totalSF);
    if (data.occupants)      sheet.getRange(row, COL.OCCUPANTS).setValue(data.occupants);
    if (data.ceilExceptions) sheet.getRange(row, COL.CEIL_HEIGHT).setValue(data.ceilExceptions);
    if (data.deckType)       sheet.getRange(row, COL.ROOF_DECK_TYPE).setValue(data.deckType === 'other' ? data.deckOther : data.deckType);
    if (data.hvacIntent)     sheet.getRange(row, COL.AC_MOUNTING).setValue(data.hvacIntent === 'other' ? data.hvacOther : data.hvacIntent);
    if (data.notes)          sheet.getRange(row, COL.DESCRIPTION).setValue(data.notes);
    sheet.getRange(row, COL.STATUS).setValue('Client Answered');
    SpreadsheetApp.flush();
    postToSlack(null, [
      { type: 'header', text: { type: 'plain_text', text: 'Client answered — ready to quote' } },
      { type: 'section', fields: [
        { type: 'mrkdwn', text: '*Project:*\n' + jobNo },
        { type: 'mrkdwn', text: '*SF:*\n' + (data.totalSF||'—') },
        { type: 'mrkdwn', text: '*Deck:*\n' + (data.deckType||'—') },
        { type: 'mrkdwn', text: '*HVAC:*\n' + (data.hvacIntent||'—') },
      ]},
    ]);
    GmailApp.sendEmail(ADMIN_EMAIL, 'Client answered — ' + jobNo, 'SF: '+(data.totalSF||'—')+'\nDeck: '+(data.deckType||'—')+'\nHVAC: '+(data.hvacIntent||'—')+'\nNotes: '+(data.notes||'—'));
    return { status: 'ok' };
  } catch (err) { _logToSheet('handleClientAnswers ERROR: ' + err.message); return { status: 'error', message: err.toString() }; }
}


// ── HELPERS ───────────────────────────────────────────────────────────────────

function _getSheet() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  return ss.getSheetByName(TAB_NAME) || ss.getSheets()[0];
}

function _findRowByProjectName(sheet, name) {
  if (!name) return -1;
  var data = sheet.getDataRange().getValues();
  var nl   = name.toLowerCase().trim();
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][COL.PROJECT_NAME-1]).toLowerCase().trim() === nl) return i+1;
    if (String(data[i][COL.JOB_NO-1]).toLowerCase().trim() === nl) return i+1;
  }
  return -1;
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
