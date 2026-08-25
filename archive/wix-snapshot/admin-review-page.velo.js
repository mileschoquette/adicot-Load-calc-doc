// =============================================================================
// ADMIN REVIEW PAGE — Velo page code (adicot.com)
// HTML component id: #html1  (the admin-review.html embed)
//
// Mirrors the portal-p3 wrapper pattern:
//   • fetch the Projects record (by _id / jobNo / title) + pricing/states CMS
//   • postMessage the record in, then postMessage the pricing payload
//   • on ADMIN_APPROVED from the component: write the CMS record, then call
//     Apps Script saveAndApprove to create the Gmail draft (no auto-send),
//     and post the result back to the component.
//
// Apps Script calls go through backend/appsScript.web.js (callAppsScript) so the
// request runs server-side — the browser cannot POST to the Google /exec URL
// directly (CORS: Google sends no Access-Control-Allow-Origin header).
//
// Collections: Projects, Import1 (ServicePricing), Import3 (State_with_PE_License)
// =============================================================================

import wixLocation from 'wix-location';
import wixData from 'wix-data';
import { callAppsScript } from 'backend/appsScript.web.js';

// Map the page's edited field keys (_values) → Wix CMS field keys.
function _mapClientFields(ans) {
  const u = {};
  const setStr = (k, v) => { if (v !== undefined && v !== null && v !== '') u[k] = v; };
  const setNum = (k, v) => { const n = parseFloat(String(v).replace(/[$,]/g,'')); if (!isNaN(n)) u[k] = n; };
  setStr('projectAddress',    ans.projectAddress);
  setStr('buildingStatus',    ans.buildingStatus);
  setNum('sf',                ans.sf);
  setNum('occupants',         ans.occupants);
  setStr('orientation',       ans.orientation);
  setStr('roofRValue',        ans.roofRValue);
  setStr('roofColor',         ans.roofColor);
  setStr('deckType',          ans.deckType);
  setStr('roofCover',         ans.roofCover);
  setStr('insulPosition',     ans.insulPosition);
  setStr('suspCeiling',       ans.suspCeiling);
  setStr('atticCond',         ans.atticCond);
  setStr('wallConstruction',  ans.wallConstruction);
  setStr('wallHeight',        ans.wallHeight);
  setNum('glassU',            ans.glassU);
  setNum('glassSHGC',         ans.glassSHGC);
  setStr('heatGenEquipment',  ans.heatGenEquipment);
  setStr('acNewExisting',     ans.acNewExisting);
  setStr('acMounting',        ans.acMounting);
  setStr('systemType',        ans.systemType);
  setStr('coolingEff',        ans.coolingEff);
  setStr('heatingEff',        ans.heatingEff);
  setStr('lightingWattsPerSF',ans.lightingWattsPerSF);
  setStr('equipWattsPerSF',   ans.equipWattsPerSF);
  setStr('ceilingHeight',     ans.ceilingHeight);
  setStr('wallFinish',        ans.wallFinish);
  setStr('wallColor',         ans.wallColor);
  setStr('wallRValue',        ans.wallRValue);
  setStr('partConstruction',  ans.partConstruction);
  setStr('partRValue',        ans.partRValue);
  setStr('floorType',         ans.floorType);
  setStr('floorRValue',       ans.floorRValue);
  setStr('doorType',          ans.doorType);
  setStr('infiltration',      ans.infiltration);
  setStr('hwType',            ans.hwType);
  setStr('hwEfficiency',      ans.hwEfficiency);
  setStr('hwCapacityGal',     ans.hwCapacityGal);
  setStr('indoorTemp',        ans.indoorTemp);
  setStr('indoorRH',          ans.indoorRH);
  setStr('changeRate',        ans.changeRate);
  setStr('liabilityCap',      ans.liabilityCap);
  setStr('condoCap',          ans.condoCap);
  setStr('deliverablesOverride', ans.deliverablesOverride);
  return u;
}

// Helper: find a project record by _id or jobNo with suppressAuth.
async function _resolveProject(id, jobNo) {
  let proj = null;
  if (id) {
    try { proj = await wixData.get('Projects', id, { suppressAuth: true }); } catch (_) {}
  }
  if (!proj && jobNo) {
    try {
      const r = await wixData.query('Projects').eq('jobNo', jobNo).find({ suppressAuth: true });
      if (r.items.length) proj = r.items[0];
    } catch (_) {}
  }
  return proj;
}

$w.onReady(async () => {

  const q = wixLocation.query;
  // The client email link uses ?id=...  — accept that as well as _id/projectId.
  const recordId   = q['_id'] || q['id'] || q['projectId'] || '';
  const projectKey = recordId || q['jobNo'] || q['title'] || '';

  // ── 1. Load the project record ────────────────────────────────────────────
  let projectData = {};
  if (projectKey) {
    try {
      if (recordId) {
        const res = await wixData.get('Projects', recordId, { suppressAuth: true });
        if (res) projectData = res;
      }
      if (!Object.keys(projectData).length && (q['jobNo'] || q['title'])) {
        const key = q['jobNo'] || q['title'];
        const res = await wixData.query('Projects')
          .eq('jobNo', key)
          .or(wixData.query('Projects').eq('title', key))
          .find({ suppressAuth: true });
        if (res.items.length > 0) projectData = res.items[0];
      }
    } catch (e) {
      console.error('Admin review — project fetch failed:', e);
    }
  }
  if (!Object.keys(projectData).length) projectData = { ...q };

  // ── 2. Load pricing + licensed states ─────────────────────────────────────
  let servicePricing = [];
  let licensedStates = [];
  try {
    const [pricingRes, statesRes] = await Promise.all([
      wixData.query('Import1').find(),
      wixData.query('Import3').find(),
    ]);
    servicePricing = pricingRes.items;
    licensedStates = statesRes.items;
  } catch (e) {
    console.error('Admin review — pricing/states query failed:', e);
  }

  // ── 3. Feed the component (record first, pricing second) ──────────────────
  let dataSent = false;
  function sendData() {
    if (dataSent) return;
    dataSent = true;

    const payload = {};
    Object.entries(projectData).forEach(([k, v]) => {
      if (v !== null && v !== undefined) payload[k] = v;
    });
    if (q['totalCost']) payload.totalCost = q['totalCost'];
    payload._mode = ((q['mode'] || '').toLowerCase() === 'client') ? 'client' : 'admin';
    if (q['quote'] === '1' || q['quote'] === 'true') payload._quote = true;
    $w('#html1').postMessage(payload);

    setTimeout(() => {
      $w('#html1').postMessage({
        _pricingData:    true,
        _servicePricing: JSON.stringify(servicePricing),
        _licensedStates: JSON.stringify(licensedStates),
      });
    }, 300);
  }

  // ── 4. Component → wrapper messages ───────────────────────────────────────
  $w('#html1').onMessage(async (event) => {
    const d = event.data;
    if (!d) return;

    // Height handshake — resize iframe + push data once we know the height
    if (d.type === 'IFRAME_HEIGHT') {
      $w('#html1').height = d.height;
      sendData();
      return;
    }

    // Approve: write CMS, then create the Gmail draft via Apps Script
    if (d.type === 'ADMIN_APPROVED') {
      const mode   = d.mode === 'proposal' ? 'proposal' : 'questions';
      const fields = d.fields || {};
      const id     = d.projectId || projectData._id || '';
      const jobNo  = d.jobNo || projectData.jobNo || '';

      let resultMsg = { type: 'ADMIN_APPROVE_RESULT', mode, status: 'ok' };

      try {
        // 4a. Resolve the live record — suppressAuth on all lookups
        const proj = await _resolveProject(id, jobNo);
        console.log('proj found:', proj ? proj._id : 'NULL');
        console.log('fields.sf:', fields.sf);
        console.log('fields.glassU:', fields.glassU);
        console.log('fields.partConstruction:', fields.partConstruction);
        console.log('fields.floorType:', fields.floorType);
        // 4b. Map edited fields → CMS keys (only write provided values)
        if (proj) {
          const u = { reviewComplete: true, status: 'Pending Client Approval' };
          const setStr = (k, v) => { if (v !== undefined && v !== null && v !== '') u[k] = v; };
          const setNum = (k, v) => { const n = parseFloat(String(v).replace(/[$,]/g,'')); if (!isNaN(n)) u[k] = n; };

          setStr('jobNo',             fields.jobNo);
          setStr('title',             fields.title);
          setStr('clientName',        fields.clientName);
          setStr('clientPhone',       fields.clientPhone);
          setStr('clientEmail',       fields.clientEmail);
          setStr('projectAddress',    fields.projectAddress);
          setStr('buildingStatus',    fields.buildingStatus);
          setNum('sf',                fields.sf);
          setNum('occupants',         fields.occupants);
          setStr('orientation',       fields.orientation);
          setStr('roofRValue',        fields.roofRValue);
          setStr('roofColor',         fields.roofColor);
          setStr('deckType',          fields.deckType);
          setStr('roofCover',         fields.roofCover);
          setStr('insulPosition',     fields.insulPosition);
          setStr('suspCeiling',       fields.suspCeiling);
          setStr('atticCond',         fields.atticCond);
          setStr('wallConstruction',  fields.wallConstruction);
          setStr('wallHeight',        fields.wallHeight);
          setNum('glassU',            fields.glassU);
          setNum('glassSHGC',         fields.glassSHGC);
          setStr('heatGenEquipment',  fields.heatGenEquipment);
          setStr('acNewExisting',     fields.acNewExisting);
          setStr('acMounting',        fields.acMounting);
          setStr('systemType',        fields.systemType);
          setStr('hvacType',          fields.hvacType);
          setStr('heatType',          fields.heatType);
          setStr('coolingEff',        fields.coolingEff);
          setStr('heatingEff',        fields.heatingEff);
          setStr('lightingWattsPerSF',fields.lightingWattsPerSF);
          setStr('equipWattsPerSF',   fields.equipWattsPerSF);
          setStr('ceilingHeight',     fields.ceilingHeight);
          setStr('wallFinish',        fields.wallFinish);
          setStr('wallColor',         fields.wallColor);
          setStr('wallRValue',        fields.wallRValue);
          setStr('partConstruction',  fields.partConstruction);
          setStr('partRValue',        fields.partRValue);
          setStr('floorType',         fields.floorType);
          setStr('floorRValue',       fields.floorRValue);
          setStr('doorType',          fields.doorType);
          setStr('infiltration',      fields.infiltration);
          setStr('hwType',            fields.hwType);
          setStr('hwEfficiency',      fields.hwEfficiency);
          setStr('hwCapacityGal',     fields.hwCapacityGal);
          setStr('indoorTemp',        fields.indoorTemp);
          setStr('indoorRH',          fields.indoorRH);
          setStr('occupancyType',     fields.occupancyType);
          setStr('productService',    fields.productService);
          setNum('totalCost',         fields.totalCost);
          setNum('engagementDays',    fields.engagementDays);
          setStr('changeRate',        fields.changeRate);
          setStr('liabilityCap',      fields.liabilityCap);
          setStr('condoCap',          fields.condoCap);
          setStr('deliverablesOverride', fields.deliverablesOverride);

          console.log('update called, u keys:', JSON.stringify(u));

          await wixData.update('Projects', { ...proj, ...u }, { suppressAuth: true });
        }

        // 4c. Create the Gmail draft (no auto-send) via Apps Script — server-side
        const out = await callAppsScript({
          action: 'saveAndApprove',
          mode,
          pid:   id,
          jobNo,
          ...fields,
        });
        if (!out || out.status !== 'ok') {
          resultMsg = {
            type: 'ADMIN_APPROVE_RESULT', mode, status: 'error',
            message: (out && out.message) ? out.message : 'No confirmation from Apps Script — draft may not have been created.'
          };
        }
      } catch (err) {
        console.error('ADMIN_APPROVED handler error:', err);
        resultMsg = { type: 'ADMIN_APPROVE_RESULT', mode, status: 'error', message: String(err.message || err) };
      }

      $w('#html1').postMessage(resultMsg);
      return;
    }

    // Client signed the proposal (client mode, quote) → write CMS, status Active
    if (d.type === 'PORTAL_SIGNED') {
      const ans = d.answers || {};
      const sig = d.signature || {};
      const id  = d.projectId || projectData._id || '';
      const jobNo = d.jobNo || projectData.jobNo || '';
      let resultMsg = { type: 'PORTAL_SIGN_RESULT', status: 'ok' };
      try {
        const proj = await _resolveProject(id, jobNo);
        if (proj) {
          const u = _mapClientFields(ans);
          u.status            = 'Current Work';
          u.proposalSigned    = true;
          u.workOrderComplete = true;
          u.signedDate        = new Date();
          u.signedBy          = sig.name || '';
          u.signedTitle       = sig.title || '';
          u.gcAccepted        = !!sig.acceptedGC;
          await wixData.update('Projects', { ...proj, ...u }, { suppressAuth: true });
        }
        try {
          await callAppsScript({
            action: 'clientSigned', projectId: jobNo || id,
            services: ans.productService || '', approxArea: ans.sf || '', occupancyType: ans.occupancyType || '',
            signedDate: (sig.signedAt || new Date().toISOString()),
          });
        } catch (_) {}
      } catch (err) {
        console.error('PORTAL_SIGNED handler error:', err);
        resultMsg = { type: 'PORTAL_SIGN_RESULT', status: 'error', message: String(err.message || err) };
      }
      $w('#html1').postMessage(resultMsg);
      return;
    }

    // Client answered the open questions (client mode, no quote) → loop back to admin
    if (d.type === 'CLIENT_ANSWERS') {
      const ans = d.answers || {};
      const id  = d.projectId || projectData._id || '';
      const jobNo = d.jobNo || projectData.jobNo || '';
      let resultMsg = { type: 'CLIENT_ANSWERS_RESULT', status: 'ok' };
      try {
        const proj = await _resolveProject(id, jobNo);
        if (proj) {
          const u = _mapClientFields(ans);
          u.status = 'Client Answered';
          await wixData.update('Projects', { ...proj, ...u }, { suppressAuth: true });
        }
        // BUG 4 FIX: send jobNo only, never fall back to _id
        try {
          await callAppsScript({
            action: 'clientAnswers', project: jobNo,
            totalSF: ans.sf || '', occupants: ans.occupants || '', deckType: ans.deckType || '',
            hvacIntent: ans.acMounting || '', notes: ans.description || '',
          });
        } catch (_) {}
      } catch (err) {
        console.error('CLIENT_ANSWERS handler error:', err);
        resultMsg = { type: 'CLIENT_ANSWERS_RESULT', status: 'error', message: String(err.message || err) };
      }
      $w('#html1').postMessage(resultMsg);
      return;
    }
  });

  // Fallback: if the height handshake never fires, push data anyway
  setTimeout(sendData, 2000);
});
