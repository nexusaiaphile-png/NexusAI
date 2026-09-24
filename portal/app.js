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
async function createMobilePairing(){
  const box=$("mobileQr");
  if(!box)return;
  box.textContent="CONNECTING TO EDGE AGENT…";
  try{
    const r=await fetch(EDGE_AGENT_URL+"/pair/start",{cache:"no-store",targetAddressSpace:"loopback"});
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
function startInstall(){ show($("installPanel")); checkEdgeAgent(); $("installPanel").scrollIntoView({behavior:"smooth",block:"center"}); updateSteps(1); }
const MAC_INSTALLER = "#!/bin/bash\nset -euo pipefail\necho \"NexusAI Edge Agent installer\"\n\nif ! command -v python3 >/dev/null 2>&1; then\n  echo \"Python 3.11+ is required. Install Python from python.org, then run this installer again.\"\n  exit 1\nfi\n\nDIR=\"$HOME/Library/Application Support/NexusAI/EdgeAgent\"\nmkdir -p \"$DIR\"\ncd \"$DIR\"\n\ncurl -fsSL \"https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py\" -o \"$DIR/agent.py\"\ncurl -fsSL \"https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt\" -o \"$DIR/requirements.txt\"\n\nif [ ! -x \"$DIR/.venv/bin/python\" ]; then\n  python3 -m venv \"$DIR/.venv\"\nfi\n\n\"$DIR/.venv/bin/python\" -m pip install --upgrade pip\n\"$DIR/.venv/bin/python\" -m pip install -r \"$DIR/requirements.txt\"\n\ncat > \"$DIR/.env\" <<EOF\nNEXUSAI_API_URL=https://nexusai-worker.onrender.com\nNEXUSAI_EDGE_TOKEN=\nLOCAL_AGENT_HOST=0.0.0.0\nLOCAL_AGENT_PORT=8787\nEOF\n\nchmod 700 \"$DIR\"\nchmod 600 \"$DIR/.env\"\n\npkill -f \"$DIR/agent.py\" >/dev/null 2>&1 || true\nnohup \"$DIR/.venv/bin/python\" \"$DIR/agent.py\" > \"$DIR/agent.out.log\" 2>&1 < /dev/null &\n\nsleep 2\nif curl -fsS \"http://127.0.0.1:8787/health\" >/dev/null 2>&1; then\n  echo \"NexusAI Edge Agent installed and running.\"\nelse\n  echo \"NexusAI Edge Agent started, but health check did not respond yet.\"\n  echo \"Log: $DIR/agent.out.log\"\nfi\necho \"Local health: http://127.0.0.1:8787/health\"\n";
const WINDOWS_INSTALLER = "$ErrorActionPreference = \"Stop\"\nWrite-Host \"NexusAI Edge Agent installer\"\nif (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw \"Python 3 is required. Install Python 3.11+ and run this installer again.\" }\n$dir = \"$env:LOCALAPPDATA\\NexusAI\\EdgeAgent\"\nNew-Item -ItemType Directory -Force -Path $dir | Out-Null\nInvoke-WebRequest -Uri \"https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py\" -OutFile \"$dir\\agent.py\"\nInvoke-WebRequest -Uri \"https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt\" -OutFile \"$dir\\requirements.txt\"\npython -m pip install --upgrade pip\n$env:NEXUSAI_API_URL = \"https://nexusai-worker.onrender.com\"\n$env:NEXUSAI_EDGE_TOKEN = \"\"\n[Environment]::SetEnvironmentVariable(\"NEXUSAI_API_URL\", \"https://nexusai-worker.onrender.com\", \"User\")\n[Environment]::SetEnvironmentVariable(\"NEXUSAI_EDGE_TOKEN\", \"\", \"User\")\nStart-Process python -ArgumentList \"$dir\\agent.py\" -WindowStyle Minimized\nWrite-Host \"NexusAI Edge Agent installed and started.\"\nWrite-Host \"Local health: http://127.0.0.1:8787/health\"\n";

function downloadInstructions(os) {
  if (os === "macOS") {
    const content = "#!/bin/bash\nset -euo pipefail\necho \"NexusAI Edge Agent installer\"\necho \"Starting automatic installation. Please keep this Terminal window open.\"\n\nif ! command -v python3 >/dev/null 2>&1; then\n  echo \"Python 3.11+ is required. Install Python from python.org, then run this installer again.\"\n  exit 1\nfi\n\nDIR=\"$HOME/Library/Application Support/NexusAI/EdgeAgent\"\nmkdir -p \"$DIR\"\ncd \"$DIR\"\n\ncurl -fsSL \"https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py\" -o \"$DIR/agent.py\"\ncurl -fsSL \"https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt\" -o \"$DIR/requirements.txt\"\n\nif [ ! -x \"$DIR/.venv/bin/python\" ]; then\n  python3 -m venv \"$DIR/.venv\"\nfi\n\n\"$DIR/.venv/bin/python\" -m pip install --upgrade pip\n\"$DIR/.venv/bin/python\" -m pip install -r \"$DIR/requirements.txt\"\n\ncat > \"$DIR/.env\" <<EOF\nNEXUSAI_API_URL=https://nexusai-worker.onrender.com\nNEXUSAI_EDGE_TOKEN=\nLOCAL_AGENT_HOST=0.0.0.0\nLOCAL_AGENT_PORT=8787\nEOF\n\nchmod 700 \"$DIR\"\nchmod 600 \"$DIR/.env\"\n\npkill -f \"$DIR/agent.py\" >/dev/null 2>&1 || true\nnohup \"$DIR/.venv/bin/python\" \"$DIR/agent.py\" > \"$DIR/agent.out.log\" 2>&1 < /dev/null &\n\nsleep 2\nif curl -fsS \"http://127.0.0.1:8787/health\" >/dev/null 2>&1; then\n  echo \"NexusAI Edge Agent installed and running.\"\nelse\n  echo \"NexusAI Edge Agent started, but health check did not respond yet.\"\n  echo \"Log: $DIR/agent.out.log\"\nfi\necho \"Local health: http://127.0.0.1:8787/health\"\necho\necho \"Installation complete. Return to the NexusAI portal and click CHECK EDGE AGENT CONNECTION.\"\necho \"You can close this Terminal window now.\"\nread -n 1 -s -r -p \"Press any key to close...\" || true\necho\n";
    const filename = "NexusAI-Edge-Agent-Mac.command";
    const blob = new Blob([content], { type: "application/x-sh; charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $("setupTitle").textContent = "macOS Edge Agent";
    $("scanText").textContent = "Download complete. Double-click NexusAI-Edge-Agent-Mac.command in Downloads. Terminal will open and install the Edge Agent automatically. You do not need to type commands.";
    return;
  }
  const content = WINDOWS_INSTALLER;
  const filename = "NexusAI-Edge-Agent-Windows.ps1";
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  $("setupTitle").textContent = "Windows Edge Agent installer";
  $("scanText").textContent = "Installer downloaded. Run it on a computer connected to the same local network as your Hikvision system, then return here and check the connection.";
}
async function checkBackend() {
  try { const r=await fetch(API_BASE_URL+"/health",{cache:"no-store"}); if(!r.ok) throw new Error("Cloud returned HTTP "+r.status); }
  catch(e) { console.warn("NexusAI cloud unavailable",e); }
}
async function pairEdgeAgent() {
  try {
    const r=await fetch(EDGE_AGENT_URL+"/configure",{method:"POST",targetAddressSpace:"loopback",headers:{"Content-Type":"application/json"},body:JSON.stringify({site_id:SITE_ID})});
    return r.ok;
  } catch(e) { return false; }
}
async function checkEdgeAgent() {
  try {
    const paired = await pairEdgeAgent();
    const r=await fetch(EDGE_AGENT_URL+"/health",{cache:"no-store",targetAddressSpace:"loopback"});
    edgeOnline=r.ok;
    updateEdgeUI();
    if(edgeOnline) { show($("discoveryPanel")); updateSteps(2); }
    if(!paired && !edgeOnline) setInstallState("Local service not detected","The NexusAI local security service is not currently reachable. Start the service for this site, then connect again.");
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
    const r=await fetch(EDGE_AGENT_URL+"/discover",{cache:"no-store",targetAddressSpace:"loopback"});
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
    const r=await fetch(EDGE_AGENT_URL+"/verify",{method:"POST",targetAddressSpace:"loopback",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:SITE_ID+"-"+(selectedDevice.ip||Date.now()),camera_name:selectedDevice.name||"Hikvision device",camera_ip:selectedDevice.ip,camera_port:selectedDevice.port||80,username,password,location:"Client site"})});
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
    const r=await fetch(EDGE_AGENT_URL+"/activate",{method:"POST",targetAddressSpace:"loopback",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:SITE_ID+"-"+(selectedDevice.ip||Date.now()),camera_name:selectedDevice.name||"Hikvision NVR",camera_ip:selectedDevice.ip,camera_port:selectedDevice.port||80,username:$("hikUsername").value.trim(),password:$("hikPassword").value,location:"Client site",channels:chosen})});
    const d=await r.json();
    if(!r.ok || !d.verified) throw new Error(d.error||"NexusAI activation failed.");
    chosen.forEach(c=>protectedCameras.push({id:SITE_ID+"-"+(c.channel_id||Date.now()),name:c.channel_name||c.name||"Camera",location:c.location||"Client site",status:"ONLINE",protection:"NEXUSAI PROTECTED",addedAt:new Date().toISOString()}));
    safeStorageSet("nexusai_cameras",JSON.stringify(protectedCameras));
    $("protectedSummary").textContent=chosen.length+" camera"+(chosen.length===1?" is":"s are")+" now connected to your NexusAI protection dashboard.";
    hide($("cameraPanel")); show($("commandPanel")); updateSteps(5); renderDashboard(); $("commandPanel").scrollIntoView({behavior:"smooth",block:"center"});
  } catch(e) { alert(e.message||"NexusAI activation failed."); }
  finally { $("protectBtn").disabled=false; $("protectBtn").textContent="PROTECT SELECTED CAMERAS"; }
}
async function refreshCloudEvents() {
  try {
    const r=await fetch(API_BASE_URL+"/api/portal/events?site_id="+encodeURIComponent(SITE_ID)+"&limit=50",{cache:"no-store"});
    if(!r.ok)return;
    const d=await r.json();
    if(Array.isArray(d.events)) {
      events=d.events.map(e=>({type:e.event,camera:e.camera_name,timestamp:e.timestamp,severity:e.severity,source:e.source,snapshot_available:e.snapshot_available}));
      safeStorageSet("nexusai_events",JSON.stringify(events));
      renderDashboard();
    }
  } catch(e) {}
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
document.addEventListener("DOMContentLoaded",()=>{ try { init(); setInterval(refreshCloudStatus,10000); setInterval(refreshCloudEvents,5000); refreshCloudEvents(); } catch(e) { console.error("NexusAI portal initialization failed",e); document.body.innerHTML='<div style="min-height:100vh;background:#04080d;color:#f2f8fb;font-family:Arial,sans-serif;display:grid;place-items:center;padding:30px;text-align:center"><div><h1>NexusAI Portal</h1><p style="color:#9aabb8;margin-top:10px">The portal could not initialize. Refresh the page and try again.</p><p style="color:#ff7b88;margin-top:10px">Please do not enter your Hikvision password until the portal is working.</p></div></div>'; } });