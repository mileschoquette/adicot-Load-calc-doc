// backend/projects.web.js
import { Permissions, webMethod } from "wix-web-module";
import wixMembersBackend from "wix-members-backend";
import { triggeredEmails } from "wix-crm-backend";
import wixData from "wix-data";
import { fetch } from 'wix-fetch';
import { mediaManager } from 'wix-media-backend';

const PROJECTS_COLLECTION = "Projects";
const ADMIN_EMAIL         = "admin@adicot.com";
const SITE_URL            = "https://www.adicotengineeringinc.com";
const PORTAL_PATH         = "/portal";

export const createProjectAndMember = webMethod(
  Permissions.Anyone,
  async (projectData) => {

    const {
      clientEmail, clientFirstName, clientLastName, clientPhone, clientCompany,
      projectName, projectAddress, propertyOwner, jobNo, totalCost, sf,
      productService, status, description, sheetRowIndex,
      buildingStatus, occupancyType, orientation, occupants,
      roofRValue, roofColor, wallConstruction, wallFinish, wallRValue,
      glassU, glassSHGC, lightingWattsPerSF, heatGenEquipment,
      snippetRoofRValue, snippetWallConstruction, snippetGlassValues,
      snippetCeilingHeight, snippetLightingWsf, snippetProjectAddress, snippetMap,
      equipWattsPerSF, engagementDays,
      deckType, roofCover, insulPosition, suspCeiling, atticCond,
      ceilingHeight, wallHeight, doorType, infiltration,
      glassFrame, glazingType, glazingTint, skylights,
      kitchenExhCFM, makeupAirCFM, indoorTemp, indoorRH,
      acNewExisting, acMounting, systemType, coolingEff, heatingEff,
      hwType, hwEfficiency, hvacType, heatType,
      projectFolder, clientCode, subClient, locationDisambig,
      lpdSpaceType, driveFolderId, driveFolderUrl,
      community, subdivision, repeatClient, insurance,
    } = projectData;

    let memberId = null;
    try {
      const existing = await wixMembersBackend.getMemberByEmail(clientEmail);
      memberId = existing._id;
    } catch {
      try {
        const reg = await wixMembersBackend.registerMember({
          email:    clientEmail,
          password: Math.random().toString(36).slice(-10),
          contactInfo: {
            firstName: clientFirstName,
            lastName:  clientLastName,
            phones:    clientPhone ? [clientPhone] : [],
          },
        });
        memberId = reg.member._id;
      } catch (_) {}
    }

    const projectRecord = {
      title:              projectName,
      jobNo:              jobNo,
      projectAddress:     projectAddress,
      propertyOwner:      propertyOwner,
      clientEmail:        clientEmail,
      clientName:         `${clientFirstName || ''} ${clientLastName || ''}`.trim(),
      clientPhone:        clientPhone,
      clientCompany:      clientCompany,
      totalCost:          parseFloat(totalCost) || 0,
      sf:                 parseInt(sf) || 0,
      productService:     productService,
      status:             status || "Pending Review",
      description:        description,
      memberId:           memberId,
      sheetRowIndex:      sheetRowIndex,
      buildingStatus:     buildingStatus,
      occupancyType:      occupancyType,
      orientation:        orientation,
      occupants:          parseInt(occupants) || 0,
      roofRValue:         roofRValue,
      roofColor:          roofColor,
      wallConstruction:   wallConstruction,
      wallFinish:         wallFinish,
      wallRValue:         wallRValue,
      glassU:             parseFloat(glassU) || 0,
      glassSHGC:          parseFloat(glassSHGC) || 0,
      lightingWattsPerSF: parseFloat(lightingWattsPerSF) || 0,
      heatGenEquipment:   heatGenEquipment,
      snippetRoofRValue:       snippetRoofRValue,
      snippetWallConstruction: snippetWallConstruction,
      snippetGlassValues:      snippetGlassValues,
      snippetCeilingHeight:    snippetCeilingHeight,
      snippetLightingWsf:      snippetLightingWsf,
      snippetProjectAddress:   snippetProjectAddress,
      snippetMap:              snippetMap || '',
      equipWattsPerSF:    equipWattsPerSF,
      engagementDays:     parseInt(engagementDays) || 0,
      deckType:           deckType,
      insurance:        insurance        || '',
      roofCover:          roofCover,
      insulPosition:      insulPosition,
      suspCeiling:        suspCeiling,
      atticCond:          atticCond,
      ceilingHeight:      ceilingHeight,
      wallHeight:         wallHeight,
      doorType:           doorType,
      infiltration:       infiltration,
      glassFrame:         glassFrame,
      glazingType:        glazingType,
      glazingTint:        glazingTint,
      skylights:          skylights,
      kitchenExhCFM:      kitchenExhCFM,
      makeupAirCFM:       makeupAirCFM,
      indoorTemp:         indoorTemp,
      indoorRH:           indoorRH,
      acNewExisting:      acNewExisting,
      acMounting:         acMounting,
      systemType:         systemType,
      coolingEff:         coolingEff,
      heatingEff:         heatingEff,
      hwType:             hwType,
      hwEfficiency:       hwEfficiency,
      projectFolder:    projectFolder    || '',
      clientCode:       clientCode       || '',
      subClient:        subClient        || '',
      locationDisambig: locationDisambig || '',
      lpdSpaceType:     lpdSpaceType     || '',
      driveFolderId:    driveFolderId    || '',
      driveFolderUrl:   driveFolderUrl   || '',
      hvacType:           hvacType           || '',
      heatType:           heatType           || '',
      community:        community        || '',
      subdivision:      subdivision      || '',
      repeatClient:     repeatClient     || false,
      workOrderComplete: false,
      proposalSigned:    false,
      reviewComplete:    false,
      createdDate:       new Date(),
    };

    const saved = await wixData.insert(PROJECTS_COLLECTION, projectRecord, { suppressAuth: true });

    let magicLinkUrl = null;
    if (memberId) {
      try {
        const ml = await wixMembersBackend.generateMagicLink(
          clientEmail,
          `${SITE_URL}${PORTAL_PATH}?project=${saved._id}`,
          3600
        );
        magicLinkUrl = ml.url;
      } catch (_) {}
    }

    return {
      success:   true,
      projectId: saved._id,
      memberId:  memberId,
      magicLink: magicLinkUrl,
    };
  }
);

export const getProjectForMember = webMethod(
  Permissions.Member,
  async (projectId) => {
    const project = await wixData.get(PROJECTS_COLLECTION, projectId);
    if (!project) throw new Error("Project not found");
    return project;
  }
);

export const getProjectForAdmin = webMethod(
  Permissions.Admin,
  async (projectId) => {
    const project = await wixData.get(PROJECTS_COLLECTION, projectId);
    if (!project) throw new Error("Project not found");
    return project;
  }
);

export const updateProjectStatus = webMethod(
  Permissions.Member,
  async (projectId, newStatus, workOrderAnswers) => {
    const project = await wixData.get(PROJECTS_COLLECTION, projectId);
    if (!project) throw new Error("Project not found");
    const updated = await wixData.update(PROJECTS_COLLECTION, {
      ...project,
      status:            newStatus,
      workOrderComplete: true,
      proposalSigned:    newStatus === "Current Work",
      workOrderAnswers:  JSON.stringify(workOrderAnswers),
      signedDate:        new Date(),
    });
    return { success: true, project: updated };
  }
);

export const sendMagicLinkEmail = webMethod(
  Permissions.Anyone,
  async (clientEmail, projectId, projectName) => {
    const magicLink = await wixMembersBackend.generateMagicLink(
      clientEmail,
      `${SITE_URL}${PORTAL_PATH}?project=${projectId}`,
      3600
    );
    await triggeredEmails.emailMember("portalAccess", clientEmail, {
      variables: { projectName, magicLink: magicLink.url, adminEmail: ADMIN_EMAIL }
    });
    return { success: true };
  }
);

export const saveReviewEdits = webMethod(
  Permissions.Admin,
  async (projectId, confirmedFields, filledFields) => {
    const project = await wixData.get(PROJECTS_COLLECTION, projectId);
    if (!project) throw new Error("Project not found");
    const updated = await wixData.update(PROJECTS_COLLECTION, {
      ...project, ...confirmedFields, ...filledFields,
      reviewComplete: true,
      status: "Pending Client Approval",
    });
    return { success: true, project: updated };
  }
);

const SCRIPT_URL = 'https://script.google.com/macros/s/AKfycbwuEYc0S4MYR2PIoX0_VjKGcl1q3ZBemmWDXL1fs7v8DLSSIBsMPsvmNNl38rF81V3XmA/exec';

export const callCreateDraft = webMethod(
  Permissions.Anyone,
  async (projectData, adminFields, clientFields, action) => {
    try {
      const payload = JSON.stringify({
        action:      action || 'createQuestionsEmail',
        projectData, adminFields, clientFields,
      });
      let response = await fetch(SCRIPT_URL, {
        method:  'post',
        headers: {
          'Content-Type': 'application/json',
          'Accept':       'application/json',
        },
        body: payload,
        redirect: 'manual',
      });
      let redirectCount = 0;
      while ((response.status === 302 || response.status === 301 || response.status === 303) && redirectCount < 5) {
        const location = response.headers.get('location');
        if (!location) break;
        response = await fetch(location, {
          method:  'get',
          headers: { 'Accept': 'application/json' },
          redirect: 'manual',
        });
        redirectCount++;
      }
      const text = await response.text();
      try {
        return JSON.parse(text);
      } catch (_) {
        return { status: 'error', message: 'Apps Script returned non-JSON (status ' + response.status + '): ' + text.slice(0, 300) };
      }
    } catch (err) {
      return { status: 'error', message: 'Fetch failed: ' + (err.message || err.toString()) };
    }
  }
);
export const uploadSnippet = webMethod(
  Permissions.Admin,
  async (fileName, base64Data, mimeType) => {
    try {
      const buffer = Buffer.from(base64Data, 'base64');
      const uploadResult = await mediaManager.upload(
        '/project-snippets',
        buffer,
        fileName || ('snippet_' + Date.now() + '.png'),
        {
          mediaOptions: {
            mimeType: mimeType || 'image/png',
            mediaType: 'image',
          },
          metadataOptions: {
            isPrivate: false,
            isVisitorUpload: false,
          },
        }
      );
      console.log('uploadResult:', JSON.stringify(uploadResult));
      const rawUrl = (uploadResult && (uploadResult.fileUrl || uploadResult.url || uploadResult.fileName)) || '';
const publicUrl = rawUrl.startsWith('wix:image://')
  ? 'https://static.wixstatic.com/media/' + rawUrl.replace('wix:image://v1/', '').split('#')[0].split('/')[0]
  : rawUrl.startsWith('http')
    ? rawUrl
    : '';
      return { status: 'ok', url: publicUrl };
    } catch (err) {
      console.error('uploadSnippet error:', err.message);
      return { status: 'error', message: err.message || err.toString() };
    }
  }
);
