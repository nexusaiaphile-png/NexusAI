const API_BASE_URL = window.location.origin;
const EDGE_AGENT_URL = "http://127.0.0.1:8787";

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
  if (!$("loginScreen") || !$("dashboardScreen")) throw new Error("NexusAI portal markup is incomplete.");
  if ($("siteCode")) $("siteCode").textContent = makeSiteCode();
  bind();
  renderDashboard();
  checkBackend();
  if (safeStorageGet("nexusai_logged_in") === "true") showDashboard(); else showLogin();
}
function bind() {
  $("loginForm").addEventListener("submit", e => { e.preventDefault(); safeStorageSet("nexusai_logged_in","true"); showDashboard(); });
  $("logoutBtn").onclick = () => { safeStorageSet("nexusai_logged_in",""); showLogin(); };
  $("startInstallBtn").onclick = startInstall;
  $("checkAgentBtn").onclick = checkEdgeAgent;
  $("scanBtn").onclick = scanNetwork;
  $("verifyDeviceBtn").onclick = verifyDevice;
  $("protectBtn").onclick = protectSelected;
  $("copySiteCode").onclick = async () => {
    try { await navigator.clipboard.writeText($("siteCode").textContent); $("copySiteCode").textContent="COPIED"; setTimeout(()=>$("copySiteCode").textContent="COPY",1200); }
    catch (_) { $("copySiteCode").textContent="SELECT & COPY"; }
  };
  $("windowsInstallBtn").onclick = () => downloadInstructions("Windows");
  $("macInstallBtn").onclick = () => downloadInstructions("macOS");
}
function showLogin(){ $("loginScreen").classList.add("active"); $("dashboardScreen").classList.remove("active"); }
function showDashboard(){ $("loginScreen").classList.remove("active"); $("dashboardScreen").classList.add("active"); renderDashboard(); checkEdgeAgent(); }
function startInstall(){ show($("installPanel")); $("installPanel").scrollIntoView({behavior:"smooth",block:"center"}); updateSteps(1); }
function downloadInstructions(os) {
  const url = os === "Windows"
    ? "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/install_windows.ps1"
    : "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/install_mac.sh";
  window.open(url, "_blank", "noopener");
  $("setupTitle").textContent = os + " Edge Agent installer";
  $("scanText").textContent = "Run the installer on a computer connected to the same local network as your Hikvision system. Then return here and check the connection.";
}
async function checkBackend() {
  try { const r=await fetch(API_BASE_URL+"/health",{cache:"no-store"}); if(!r.ok) throw new Error("Cloud returned HTTP "+r.status); }
  catch(e) { console.warn("NexusAI cloud unavailable",e); }
}
async function pairEdgeAgent() {
  try {
    const r=await fetch(EDGE_AGENT_URL+"/configure",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({site_id:SITE_ID})});
    return r.ok;
  } catch(e) { return false; }
}
async function checkEdgeAgent() {
  try {
    const paired = await pairEdgeAgent();
    const r=await fetch(EDGE_AGENT_URL+"/health",{cache:"no-store"});
    edgeOnline=r.ok;
    updateEdgeUI();
    if(edgeOnline) { show($("discoveryPanel")); updateSteps(2); }
    if(!paired && !edgeOnline) setInstallState("Edge Agent not detected","Install and start the NexusAI Edge Agent on a computer at this site, then check the connection again.");
  } catch(e) {
    edgeOnline=false;
    updateEdgeUI();
  }
}
function setInstallState(title,text) {
  if ($("setupTitle")) $("setupTitle").textContent=title;
  if ($("scanText")) $("scanText").textContent=text;
}
function updateEdgeUI() {
  if ($("edgeStatus")) $("edgeStatus").innerHTML=edgeOnline ? "<i></i> EDGE AGENT ONLINE" : "<i></i> EDGE AGENT WAITING";
  if ($("edgeStat")) $("edgeStat").textContent=edgeOnline ? "ONLINE" : "WAITING";
  if(edgeOnline) {
    $("scanStatus").textContent="READY";
    $("scanTitle").textContent="Edge Agent connected";
    $("scanText").textContent="Your local NexusAI Edge Agent is connected. You can now discover your Hikvision system.";
  }
}
async function scanNetwork() {
  if(!edgeOnline) { $("scanStatus").textContent="EDGE AGENT REQUIRED"; $("scanTitle").textContent="Connect the Edge Agent first"; $("scanText").textContent="Install and start the NexusAI Edge Agent on a computer at this site."; return; }
  $("scanBtn").disabled=true; $("scanBtn").textContent="SCANNING…"; $("scanStatus").textContent="SCANNING"; $("scanTitle").textContent="Searching local network…";
  try {
    const r=await fetch(EDGE_AGENT_URL+"/discover",{cache:"no-store"});
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
    const r=await fetch(EDGE_AGENT_URL+"/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:SITE_ID+"-"+(selectedDevice.ip||Date.now()),camera_name:selectedDevice.name||"Hikvision device",camera_ip:selectedDevice.ip,camera_port:selectedDevice.port||80,username,password,location:"Client site"})});
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
    const r=await fetch(EDGE_AGENT_URL+"/activate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:SITE_ID+"-"+(selectedDevice.ip||Date.now()),camera_name:selectedDevice.name||"Hikvision NVR",camera_ip:selectedDevice.ip,camera_port:selectedDevice.port||80,username:$("hikUsername").value.trim(),password:$("hikPassword").value,location:"Client site"})});
    const d=await r.json();
    if(!r.ok || !d.verified) throw new Error(d.error||"NexusAI activation failed.");
    chosen.forEach(c=>protectedCameras.push({id:SITE_ID+"-"+(c.channel_id||Date.now()),name:c.channel_name||c.name||"Camera",location:c.location||"Client site",status:"ONLINE",protection:"NEXUSAI PROTECTED",addedAt:new Date().toISOString()}));
    safeStorageSet("nexusai_cameras",JSON.stringify(protectedCameras));
    $("protectedSummary").textContent=chosen.length+" camera"+(chosen.length===1?" is":"s are")+" now connected to your NexusAI protection dashboard.";
    hide($("cameraPanel")); show($("commandPanel")); updateSteps(5); renderDashboard(); $("commandPanel").scrollIntoView({behavior:"smooth",block:"center"});
  } catch(e) { alert(e.message||"NexusAI activation failed."); }
  finally { $("protectBtn").disabled=false; $("protectBtn").textContent="PROTECT SELECTED CAMERAS"; }
}
async function refreshCloudStatus() {
  try {
    const r=await fetch(API_BASE_URL+"/api/portal/status?site_id="+encodeURIComponent(SITE_ID),{cache:"no-store"});
    if(!r.ok)return;
    const d=await r.json();
    if(d.edge_agent==="ONLINE") { edgeOnline=true; updateEdgeUI(); }
  } catch(e) {}
}
function renderDashboard() {
  $("activeCameras").textContent=protectedCameras.length;
  $("eventCount").textContent=events.length;
  $("cameraList").innerHTML=protectedCameras.length?protectedCameras.map(c=>`<div class="camera-card"><div class="camera-top"><div class="camera-icon">◉</div><span class="online-badge">● ONLINE</span></div><h3>${escapeHTML(c.name)}</h3><p>${escapeHTML(c.location||"Site camera")}</p><div class="protected">✓ NEXUSAI PROTECTED</div></div>`).join(""):'<div class="empty-state">Complete self-installation to see your protected cameras.</div>';
  $("eventList").innerHTML=events.length?events.slice(0,20).map(e=>`<div class="event-row"><span>◉</span><div><strong>${escapeHTML(e.type||"Security event")}</strong><small>${escapeHTML(e.camera||"NexusAI")}</small></div><time>${new Date(e.timestamp||Date.now()).toLocaleString()}</time></div>`).join(""):'<div class="empty-state">No security events yet.</div>';
}
function escapeHTML(v){return String(v??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));}
document.addEventListener("DOMContentLoaded",()=>{ try { init(); setInterval(refreshCloudStatus,10000); } catch(e) { console.error("NexusAI portal initialization failed",e); document.body.innerHTML='<div style="min-height:100vh;background:#04080d;color:#f2f8fb;font-family:Arial,sans-serif;display:grid;place-items:center;padding:30px;text-align:center"><div><h1>NexusAI Portal</h1><p style="color:#9aabb8;margin-top:10px">The portal could not initialize. Refresh the page and try again.</p><p style="color:#ff7b88;margin-top:10px">Please do not enter your Hikvision password until the portal is working.</p></div></div>'; } });