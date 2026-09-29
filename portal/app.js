const API_BASE_URL = window.location.origin;
const EDGE_AGENT_URLS = ["http://127.0.0.1:8787", "http://localhost:8787"];

function safeStorageGet(key, fallback = null) {
  try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
}
function safeStorageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) {}
}
function getSiteId() {
  const existing = safeStorageGet("nexusai_site_id");
  if (existing) return existing;
  let id = "";
  try { id = crypto.randomUUID(); } catch (_) { id = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2,10); }
  id = "site-" + id;
  safeStorageSet("nexusai_site_id", id);
  return id;
}
const SITE_ID = getSiteId();

let edgeOnline = false;
let discoveredDevices = [];
let selectedDevice = null;
let verifiedChannels = [];
let selectedChannels = new Set();
let protectedCameras = loadArray("nexusai_cameras");
let events = loadArray("nexusai_events");
let previousAlertKeys = new Set();
let panelAlerts = loadArray("nexusai_panel_alerts");
let browserAlertsEnabled = safeStorageGet("nexusai_browser_alerts", "false") === "true";

function loadArray(key) {
  try {
    const value = JSON.parse(safeStorageGet(key, "[]"));
    return Array.isArray(value) ? value : [];
  } catch (_) {
    return [];
  }
}

const $ = id => document.getElementById(id);
const show = el => el && el.classList.remove("hidden");
const hide = el => el && el.classList.add("hidden");

function updateSteps(step) {
  document.querySelectorAll(".step").forEach(el => el.classList.toggle("active", Number(el.dataset.step) <= step));
  if ($("setupBadge")) $("setupBadge").textContent = "STEP " + step + " OF 5";
}
function makeSiteCode() {
  return "NEX-" + SITE_ID.slice(-8).replace(/-/g, "").toUpperCase();
}
function init() {
  if (!$("dashboardScreen")) throw new Error("NexusAI portal markup is incomplete.");
  if ($("siteCode")) $("siteCode").textContent = makeSiteCode();
  bind();
  renderDashboard();
  renderPanelAlerts();
  updateBrowserAlertButton();
  checkBackend();
  showDashboard();
}
async function createMobilePairing(){
  const box=$("mobileQr");
  if(!box)return;
  box.textContent="CONNECTING TO EDGE AGENT…";
  try{
    const r=await edgeFetch("/pair/start").then(x=>x.response);
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||"Edge Agent pairing is unavailable");
    const url=d.mobile_url;
    box.innerHTML="";
    const img=document.createElement("img");
    img.alt="NexusAI mobile activation QR code";
    img.width=156; img.height=156;
    img.style.background="#fff"; img.style.padding="8px"; img.style.borderRadius="10px";
    img.src=d.qr_data_url||"";
    if(!d.qr_data_url) throw new Error("Edge Agent did not return a QR image");
    box.appendChild(img);
    $("scanText").textContent="Scan the QR code with a phone connected to the same local network. The phone will pair directly with this Edge Agent.";
  }catch(e){
    box.textContent="START EDGE AGENT TO GENERATE QR";
    console.warn("NexusAI mobile pairing unavailable",e);
  }
}
function bind() {
  $("loginForm")?.addEventListener("submit", e => {
    e.preventDefault();
    const form = $("loginForm");
    if (!form || !form.checkValidity()) {
      form?.reportValidity();
      return;
    }
    safeStorageSet("nexusai_logged_in", "true");
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      button.textContent = "SIGNING IN…";
    }
    showDashboard();
    setTimeout(() => {
      if (button) {
        button.disabled = false;
        button.textContent = "SIGN IN";
      }
    }, 500);
  });
  if ($("logoutBtn")) $("logoutBtn").onclick = () => { showDashboard(); };
  $("startInstallBtn").onclick = startInstall;
  $("checkAgentBtn").onclick = checkEdgeAgent;
  $("scanBtn").onclick = scanNetwork;
  $("verifyDeviceBtn").onclick = verifyDevice;
  $("protectBtn").onclick = protectSelected;
  if ($("enableBrowserAlertsBtn")) $("enableBrowserAlertsBtn").onclick = enableBrowserAlerts;
  if ($("clearPanelAlertsBtn")) $("clearPanelAlertsBtn").onclick = clearPanelAlerts;
  if ($("installNexusAppBtn")) $("installNexusAppBtn").onclick = installNexusApp;
  $("copySiteCode").onclick = async () => {
    try { await navigator.clipboard.writeText($("siteCode").textContent); $("copySiteCode").textContent="COPIED"; setTimeout(()=>$("copySiteCode").textContent="COPY",1200); }
    catch (_) { $("copySiteCode").textContent="SELECT & COPY"; }
  };
  if ($("windowsInstallBtn")) $("windowsInstallBtn").onclick = () => downloadInstructions("Windows");
  if ($("macInstallBtn")) $("macInstallBtn").onclick = () => downloadInstructions("macOS");
}
function showDashboard(){ $("dashboardScreen").classList.add("active"); renderDashboard(); checkEdgeAgent(); }
function startInstall(){ show($("installPanel")); checkEdgeAgent(); $("installPanel").scrollIntoView({behavior:"smooth",block:"center"}); updateSteps(1); }
async function downloadInstructions(os) {
  const endpoint = os === "macOS" ? "/downloads/install_mac.sh" : "/downloads/install_windows.ps1";
  const filename = os === "macOS" ? "NexusAI-Edge-Agent-Mac.sh" : "NexusAI-Edge-Agent-Windows.ps1";
  try {
    const r = await fetch(endpoint, {cache:"no-store"});
    if (!r.ok) throw new Error("Installer download failed: HTTP " + r.status);
    const content = await r.text();
    const blob = new Blob([content], {type:"text/plain;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if ($("setupTitle")) $("setupTitle").textContent = os + " Edge Agent installer";
    if ($("scanText")) $("scanText").textContent = "Installer downloaded. Run it on a computer connected to the same local network as your Hikvision system, then return here and check the connection.";
  } catch (e) {
    if ($("setupTitle")) $("setupTitle").textContent = "Installer unavailable";
    if ($("scanText")) $("scanText").textContent = "NexusAI could not download the current Edge Agent installer. Please try again.";
    console.error("NexusAI installer download failed", e);
  }
}
async function checkBackend() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const r=await fetch(API_BASE_URL+"/health",{cache:"no-store",signal:controller.signal});
    if(!r.ok) throw new Error("Cloud returned HTTP "+r.status);
    return true;
  } catch(e) {
    console.warn("NexusAI cloud unavailable",e);
    return false;
  } finally {
    clearTimeout(timer);
  }
}
async function edgeFetch(path, options = {}) {
  let lastError = null;
  for (const base of EDGE_AGENT_URLS) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), options.timeout || 5000);
      try {
        const response = await fetch(base + path, {
          ...options,
          cache: "no-store",
          mode: "cors",
          targetAddressSpace: "loopback",
          signal: controller.signal
        });
        return { response, base };
      } finally {
        clearTimeout(timer);
      }
    } catch (error) {
      lastError = error;
      console.warn("NexusAI Edge Agent request failed:", base + path, error);
    }
  }
  throw lastError || new Error("Edge Agent is unavailable");
}

async function pairEdgeAgent() {
  try {
    const result = await edgeFetch("/configure", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({site_id:SITE_ID})
    });
    return result.response.ok;
  } catch(e) { return false; }
}

async function checkEdgeAgent() {
  const cloudOk = await checkBackend();
  let localOk = false;
  let localError = "";

  try {
    const result = await edgeFetch("/health");
    localOk = result.response.ok;
    if (localOk) {
      try {
        const health = await result.response.clone().json();
        const version = health?.version || "UNKNOWN";
        if ($("edgeVersionBadge")) $("edgeVersionBadge").textContent = version === "1.7.0" ? "UP TO DATE" : "UPDATE REQUIRED";
        if ($("edgeVersionText")) $("edgeVersionText").textContent = "Version " + version + (version === "1.7.0" ? " is installed. Notifications and the latest portal controls are available." : " is installed. Update to version 1.7.0 before testing notifications.");
      } catch (_) {}
    } else if (!localOk) {
      if ($("edgeVersionBadge")) $("edgeVersionBadge").textContent = "OFFLINE";
      if ($("edgeVersionText")) $("edgeVersionText").textContent = "The local Edge Agent could not be reached.";
      localError = "Edge Agent returned HTTP " + result.response.status + ".";
    }
  } catch(e) {
    localError = e?.name === "AbortError"
      ? "Browser timed out connecting to the local Edge Agent."
      : (e?.message || "Browser could not connect to the local Edge Agent.");
  }

  edgeOnline = Boolean(cloudOk && localOk);
  updateEdgeUI();

  if (edgeOnline) {
    show($("discoveryPanel"));
    updateSteps(2);
    setInstallState(
      "NexusAI local service connected",
      "Edge Agent is ONLINE on this computer. NexusAI can now discover compatible Hikvision equipment on this network."
    );
  } else {
    hide($("discoveryPanel"));
    updateSteps(1);

    if (!localOk && cloudOk) {
      setInstallState(
        "Edge Agent connection blocked",
        localError + " The Edge Agent is confirmed to run at 127.0.0.1:8787. Allow Local Network access for getnexusai.co.za if Chrome asks."
      );
    } else if (localOk && !cloudOk) {
      setInstallState(
        "Cloud connection unavailable",
        "The local Edge Agent is online, but NexusAI Cloud cannot be reached."
      );
    } else {
      setInstallState(
        "NexusAI connection unavailable",
        "The cloud and local security service could not both be reached."
      );
    }
  }
}
function setInstallState(title,text) {
  if ($("setupTitle")) $("setupTitle").textContent=title;
  if ($("scanText")) $("scanText").textContent=text;
}
function updateEdgeUI() {
  if ($("edgeStatus")) $("edgeStatus").innerHTML=edgeOnline ? "<i></i> LOCAL SECURITY SERVICE ONLINE" : "<i></i> CONNECTING";
  if ($("edgeStat")) $("edgeStat").textContent=edgeOnline ? "ONLINE" : "WAITING";
  if ($("connectionTitle")) $("connectionTitle").textContent=edgeOnline ? "NexusAI local service connected" : "Waiting for NexusAI local service";
  if ($("connectionText")) $("connectionText").textContent=edgeOnline ? "Your local security service is connected. NexusAI can now discover the Hikvision equipment on this network." : "The local NexusAI security service must be running on this network before the portal can discover private Hikvision equipment.";
  if(edgeOnline) {
    $("scanStatus").textContent="READY";
    $("scanTitle").textContent="Ready to discover your cameras";
    $("scanText").textContent="NexusAI will search the local network for compatible Hikvision devices.";
  }
}
async function scanNetwork() {
  if(!edgeOnline) { $("scanStatus").textContent="CONNECTING"; $("scanTitle").textContent="Local security service required"; $("scanText").textContent="NexusAI cannot safely scan a private camera network directly from the browser."; return; }
  $("scanBtn").disabled=true; $("scanBtn").textContent="SCANNING…"; $("scanStatus").textContent="SCANNING"; $("scanTitle").textContent="Searching local network…";
  try {
    const r=await edgeFetch("/discover").then(x=>x.response);
    if(!r.ok) throw new Error("Discovery HTTP "+r.status);
    const d=await r.json();
    discoveredDevices=Array.isArray(d.devices)?d.devices:[];
    renderDevices();
    $("scanTitle").textContent=discoveredDevices.length ? discoveredDevices.length+" compatible device(s) found" : "No compatible devices found";
    $("scanText").textContent=discoveredDevices.length ? "Select your Hikvision device. Discovery does not authorize or activate it." : "Make sure the Edge Agent computer and Hikvision system are connected to the same local network.";
    $("scanStatus").textContent=discoveredDevices.length ? "FOUND" : "NO DEVICES";
  } catch(e) {
    $("scanTitle").textContent="Discovery unavailable";
    $("scanText").textContent="The local Edge Agent did not return a valid discovery response.";
    $("scanStatus").textContent="ERROR";
    console.error(e);
  } finally { $("scanBtn").disabled=false; $("scanBtn").textContent="SCAN MY NETWORK"; }
}
function renderDevices() {
  $("deviceList").innerHTML=discoveredDevices.map((d,i)=>`<button class="device-card" data-device="${i}"><div class="device-icon">N</div><div><strong>${escapeHTML(d.name||"Hikvision device")}</strong><span>${escapeHTML(d.ip||"Local device")} • ${escapeHTML(d.type||"Hikvision")}</span></div><b>SELECT</b></button>`).join("");
  document.querySelectorAll(".device-card").forEach(b=>b.onclick=()=>selectDevice(Number(b.dataset.device)));
}
function selectDevice(i) {
  selectedDevice=discoveredDevices[i];
  $("selectedDeviceTitle").textContent="Verify "+(selectedDevice.name||"Hikvision device");
  $("selectedDeviceAddress").textContent=selectedDevice.ip||"Local device";
  $("selectedDeviceType").textContent=selectedDevice.type||"Hikvision device";
  show($("verificationPanel")); updateSteps(3); $("verificationPanel").scrollIntoView({behavior:"smooth",block:"center"});
}
async function verifyDevice() {
  const username=$("hikUsername").value.trim(), password=$("hikPassword").value;
  if(!selectedDevice || !password) { $("verifyResult").textContent="Select a device and enter the Hikvision password."; show($("verifyResult")); return; }
  $("verifyDeviceBtn").disabled=true; $("verifyDeviceBtn").textContent="VERIFYING…";
  try {
    const result=await edgeFetch("/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:SITE_ID+"-"+(selectedDevice.ip||Date.now()),camera_name:selectedDevice.name||"Hikvision device",camera_ip:selectedDevice.ip,camera_port:selectedDevice.port||80,username,password,location:"Client site"})});
    const d=await r.json();
    if(!r.ok || !d.verified) throw new Error(d.error||"Device verification failed.");
    verifiedChannels=Array.isArray(d.channels)?d.channels:[];
    if(!verifiedChannels.length) throw new Error("Hikvision device verified, but no camera channels were returned.");
    renderChannels(); show($("cameraPanel")); hide($("verifyResult")); updateSteps(4); $("cameraPanel").scrollIntoView({behavior:"smooth",block:"center"});
  } catch(e) { $("verifyResult").textContent=e.message; show($("verifyResult")); $("verifyResult").className="result-box error"; }
  finally { $("verifyDeviceBtn").disabled=false; $("verifyDeviceBtn").textContent="VERIFY DEVICE"; }
}
function renderChannels() {
  $("cameraSelection").innerHTML=verifiedChannels.map((c,i)=>`<label class="camera-select"><input type="checkbox" data-channel="${i}"><div><strong>${escapeHTML(c.channel_name||c.name||"Camera "+(i+1))}</strong><span>Channel ${escapeHTML(c.channel_id||String(i+1))}</span></div><b>SELECT</b></label>`).join("");
  document.querySelectorAll(".camera-select input").forEach(x=>x.onchange=()=>{const i=Number(x.dataset.channel); x.checked?selectedChannels.add(i):selectedChannels.delete(i); $("selectedCount").textContent=selectedChannels.size+" SELECTED";});
}
async function protectSelected() {
  if(!selectedChannels.size) { alert("Select at least one camera."); return; }
  if(!selectedDevice) { alert("Select and verify your Hikvision device first."); return; }
  const chosen=[...selectedChannels].map(i=>verifiedChannels[i]);
  $("protectBtn").disabled=true; $("protectBtn").textContent="ACTIVATING…";
  try {
    const result=await edgeFetch("/activate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:SITE_ID+"-"+(selectedDevice.ip||Date.now()),camera_name:selectedDevice.name||"Hikvision NVR",camera_ip:selectedDevice.ip,camera_port:selectedDevice.port||80,username:$("hikUsername").value.trim(),password:$("hikPassword").value,location:"Client site",channels:chosen})});
    const d=await r.json();
    if(!r.ok || !d.verified) throw new Error(d.error||"NexusAI activation failed.");
    chosen.forEach(c=>protectedCameras.push({id:SITE_ID+"-"+(c.channel_id||Date.now()),name:c.channel_name||c.name||"Camera",location:c.location||"Client site",status:"ONLINE",protection:"NEXUSAI PROTECTED",addedAt:new Date().toISOString()}));
    safeStorageSet("nexusai_cameras",JSON.stringify(protectedCameras));
    $("protectedSummary").textContent=chosen.length+" camera"+(chosen.length===1?" is":"s are")+" now connected to your NexusAI protection dashboard.";
    hide($("cameraPanel")); show($("commandPanel")); updateSteps(5); renderDashboard(); $("commandPanel").scrollIntoView({behavior:"smooth",block:"center"});
  } catch(e) { alert(e.message||"NexusAI activation failed."); }
  finally { $("protectBtn").disabled=false; $("protectBtn").textContent="PROTECT SELECTED CAMERAS"; }
}

function installNexusApp() {
  const url = "/app/?site_id=" + encodeURIComponent(SITE_ID);
  const instructions = $("nexusAppInstructions");
  if (instructions) instructions.innerHTML = "<strong>Open NexusAI on your phone:</strong> scan or send this portal link to the client's phone, then enable notifications. On iPhone use Share → Add to Home Screen; on Android use the browser's Install/Add to Home Screen option.";
  window.open(url, "_blank", "noopener");
}

function alertKey(e) {
  return [e.timestamp,e.camera_name||e.camera,e.event||e.type,e.severity].map(v=>String(v||"")).join("|");
}
function eventSeverityClass(severity) {
  return String(severity||"LOW").toUpperCase();
}
function renderPanelAlerts() {
  const list=$("panelAlertList");
  if(!list) return;
  list.innerHTML=panelAlerts.length ? panelAlerts.slice(0,30).map(e=>`<div class="event-row"><span>◉</span><div><strong>${escapeHTML(e.event||e.type||"SECURITY EVENT")}</strong><small>${escapeHTML(e.camera_name||e.camera||"NexusAI")} • ${escapeHTML(e.location||"Client site")} • ${escapeHTML(eventSeverityClass(e.severity))}</small></div><time>${new Date(e.timestamp||Date.now()).toLocaleString()}</time></div>`).join("") : '<div class="empty-state">No security alerts yet.</div>';
}
function updateBrowserAlertButton() {
  const button=$("enableBrowserAlertsBtn");
  if(!button) return;
  if(!("Notification" in window)) {
    button.textContent="BROWSER ALERTS UNAVAILABLE";
    button.disabled=true;
    return;
  }
  if(Notification.permission==="granted" && browserAlertsEnabled) button.textContent="BROWSER ALERTS ENABLED";
  else if(Notification.permission==="denied") button.textContent="BROWSER ALERTS BLOCKED";
  else button.textContent="ENABLE BROWSER ALERTS";
}
async function enableBrowserAlerts() {
  if(!("Notification" in window)) {
    showPanelAlertResult("This browser does not support desktop notifications.",true); return;
  }
  try {
    const permission=await Notification.requestPermission();
    if(permission!=="granted") {
      browserAlertsEnabled=false;
      safeStorageSet("nexusai_browser_alerts","false");
      showPanelAlertResult("Browser alerts were not enabled. The NexusAI panel will still receive live events.",true);
    } else {
      browserAlertsEnabled=true;
      safeStorageSet("nexusai_browser_alerts","true");
      showPanelAlertResult("Browser alerts are enabled. NexusAI will notify you when a new security event arrives.");
    }
  } catch(e) {
    showPanelAlertResult("Could not enable browser alerts.",true);
  }
  updateBrowserAlertButton();
}
function showPanelAlertResult(message,error=false) {
  const box=$("panelAlertResult");
  if(!box)return;
  box.textContent=message;
  box.className="result-box "+(error?"error":"");
  show(box);
}
function notifyNewSecurityEvent(event) {
  const title="NexusAI Security Alert";
  const body=(event.event||"Security event detected")+" • "+(event.camera_name||event.camera||"Protected camera")+" • "+(event.severity||"LOW");
  if(browserAlertsEnabled && "Notification" in window && Notification.permission==="granted") {
    try { new Notification(title,{body,tag:"nexusai-"+alertKey(event)}); } catch(_) {}
  }
  if($("panelAlertHeadline")) $("panelAlertHeadline").textContent=event.event||"Security event detected";
  if($("panelAlertSummary")) $("panelAlertSummary").textContent=(event.camera_name||event.camera||"Protected camera")+" • "+(event.location||"Client site")+" • Severity "+(event.severity||"LOW");
}
function clearPanelAlerts() {
  panelAlerts=[];
  safeStorageSet("nexusai_panel_alerts",JSON.stringify(panelAlerts));
  renderPanelAlerts();
  showPanelAlertResult("Local alert history cleared. Cloud events remain stored.");
}
async function refreshCloudEvents() {
  try {
    const r=await fetch(API_BASE_URL+"/api/portal/events?site_id="+encodeURIComponent(SITE_ID)+"&limit=50",{cache:"no-store"});
    if(!r.ok)return;
    const d=await r.json();
    if(Array.isArray(d.events)) {
      const incoming=d.events.map(e=>({type:e.event,event:e.event,camera:e.camera_name,camera_name:e.camera_name,location:e.location,timestamp:e.timestamp,severity:e.severity,source:e.source,snapshot_available:e.snapshot_available}));
      const incomingKeys=new Set(incoming.map(alertKey));
      const newEvents=incoming.filter(e=>!previousAlertKeys.has(alertKey(e)));
      if(previousAlertKeys.size) newEvents.slice(0,10).forEach(e=>{
        panelAlerts.unshift(e);
        notifyNewSecurityEvent(e);
      });
      previousAlertKeys=incomingKeys;
      panelAlerts=panelAlerts.slice(0,50);
      events=incoming;
      safeStorageSet("nexusai_events",JSON.stringify(events));
      safeStorageSet("nexusai_panel_alerts",JSON.stringify(panelAlerts));
      renderPanelAlerts();
      renderDashboard();
    }
  } catch(e) {}
}
async function refreshCloudStatus() {
  // Use the same connection test as the main CHECK CONNECTION action.
  // A background refresh must never use a different path and overwrite
  // a valid Edge Agent state with a false WAITING state.
  await checkEdgeAgent();
}
function renderDashboard() {
  $("activeCameras").textContent=protectedCameras.length;
  $("eventCount").textContent=events.length;
  $("cameraList").innerHTML=protectedCameras.length?protectedCameras.map(c=>`<div class="camera-card"><div class="camera-top"><div class="camera-icon">◉</div><span class="online-badge">● ONLINE</span></div><h3>${escapeHTML(c.name)}</h3><p>${escapeHTML(c.location||"Site camera")}</p><div class="protected">✓ NEXUSAI PROTECTED</div></div>`).join(""):'<div class="empty-state">Complete self-installation to see your protected cameras.</div>';
  $("eventList").innerHTML=events.length?events.slice(0,20).map(e=>`<div class="event-row"><span>◉</span><div><strong>${escapeHTML(e.type||"Security event")}</strong><small>${escapeHTML(e.camera||"NexusAI")}</small></div><time>${new Date(e.timestamp||Date.now()).toLocaleString()}</time></div>`).join(""):'<div class="empty-state">No security events yet.</div>';
}
function escapeHTML(v){return String(v??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));}
document.addEventListener("DOMContentLoaded",()=>{ try { init(); setInterval(refreshCloudStatus,10000); setInterval(refreshCloudEvents,5000); refreshCloudEvents(); } catch(e) { console.error("NexusAI portal initialization failed",e); document.body.innerHTML='<div style="min-height:100vh;background:#04080d;color:#f2f8fb;font-family:Arial,sans-serif;display:grid;place-items:center;padding:30px;text-align:center"><div><h1>NexusAI Portal</h1><p style="color:#9aabb8;margin-top:10px">The portal could not initialize. Refresh the page and try again.</p><p style="color:#ff7b88;margin-top:10px">Please do not enter your Hikvision password until the portal is working.</p></div></div>'; } });