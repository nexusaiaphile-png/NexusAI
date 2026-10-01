const params=new URLSearchParams(location.search);
let siteId=params.get("site_id")||localStorage.getItem("nexusai_site_id")||"";
let installToken=params.get("install_token")||localStorage.getItem("nexusai_install_token")||"";
const API=location.origin;
let deferredInstall=null;
const $=id=>document.getElementById(id);
function setStatus(title,text,error=false){$("statusTitle").textContent=title;$("statusText").textContent=text;$("statusText").className="install-help"+(error?" error":"");$("dot").className="dot"+(error?" off":"")}
function b64ToBytes(value){const pad="=".repeat((4-value.length%4)%4);const raw=atob((value+pad).replace(/-/g,"+").replace(/_/g,"/"));const out=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)out[i]=raw.charCodeAt(i);return out}
function showInstallHelp(){const el=$("installHelp");if(!el)return;if(deferredInstall){el.textContent="Your phone supports direct installation. Tap INSTALL NEXUSAI APP and confirm the install prompt."}else if(/iphone|ipad|ipod/i.test(navigator.userAgent)){el.textContent="On iPhone/iPad: tap Share in Safari → Add to Home Screen → Add. Then open the NexusAI icon and enable alerts."}else{el.textContent="If your browser does not show an install prompt, use its menu and choose Install app or Add to Home screen."}}
async function activatePhone(){
  const code=($("activationCode")?.value||"").trim().toUpperCase();
  if(!code){setStatus("ACTIVATION CODE REQUIRED","Enter the site activation code shown on the NexusAI client portal.",true);return}
  $("activateBtn").disabled=true;$("activateBtn").textContent="ACTIVATING…";
  try{
    const r=await fetch(API+"/api/push/activate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({activation_code:code})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||"Activation code not found.");
    siteId=d.site_id;installToken=d.install_token;
    localStorage.setItem("nexusai_site_id",siteId);localStorage.setItem("nexusai_install_token",installToken);
    $("activationCode").value=code;
    $("enableBtn").disabled=false;
    setStatus("PHONE ACTIVATED","This phone is now linked to the NexusAI site. Enable Security Alerts.");
  }catch(e){setStatus("ACTIVATION FAILED",e.message||"Could not activate this phone.",true)}
  finally{$("activateBtn").disabled=false;$("activateBtn").textContent="ACTIVATE THIS PHONE"}
}
async function registerPush(){
  if(!siteId||!installToken)throw new Error("Open NexusAI from the client portal installation button.");
  if(!window.isSecureContext)throw new Error("NexusAI mobile alerts require a secure HTTPS connection.");
  if(!("serviceWorker" in navigator)||!("PushManager" in window)||!("Notification" in window))throw new Error("This phone/browser does not support NexusAI push notifications.");
  const cfg=await fetch(API+"/api/push/config",{cache:"no-store"}).then(r=>r.json());
  if(!cfg.configured||!cfg.public_key)throw new Error("NexusAI push service is waiting for its secure server keys.");
  const reg=await navigator.serviceWorker.register("/app/service-worker.js",{scope:"/app/",updateViaCache:"none"});
  await reg.update();
  const permission=await Notification.requestPermission();
  if(permission!=="granted")throw new Error("Notification permission was not granted. Enable notifications for NexusAI in your phone settings.");
  let sub=await reg.pushManager.getSubscription();
  if(!sub)sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64ToBytes(cfg.public_key)});
  const response=await fetch(API+"/api/push/subscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({site_id:siteId,install_token:installToken,subscription:sub.toJSON()})});
  const data=await response.json();
  if(!response.ok)throw new Error(data.detail||"NexusAI could not register this phone.");
  localStorage.setItem("nexusai_site_id",siteId);localStorage.setItem("nexusai_install_token",installToken);
  $("enableBtn").disabled=true;$("enableBtn").textContent="SECURITY ALERTS ENABLED";$("testBtn").disabled=false;
  setStatus("NEXUSAI ALERTS ENABLED","This phone is registered for real-time NexusAI security notifications.");
}
async function sendTest(){
  $("testBtn").disabled=true;$("testBtn").textContent="SENDING…";
  try{
    const r=await fetch(API+"/api/push/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({site_id:siteId,install_token:installToken})});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||"Test alert failed.");
    $("testBtn").textContent="TEST ALERT SENT";
    setTimeout(()=>{$("testBtn").disabled=false;$("testBtn").textContent="SEND TEST ALERT"},2500);
  }catch(e){$("testBtn").disabled=false;$("testBtn").textContent="SEND TEST ALERT";setStatus("TEST FAILED",e.message,true)}
}
function addLocalAlert(data){
  const box=document.createElement("div");box.className="event";
  const title=document.createElement("strong");title.textContent=data.title||"NexusAI Security Alert";
  const small=document.createElement("small");small.textContent=data.body||"Security event detected.";
  box.append(title,small);$("latest").prepend(box);
}
$("installBtn").addEventListener("click",async()=>{
  if(deferredInstall){deferredInstall.prompt();try{await deferredInstall.userChoice}catch(_){}deferredInstall=null;showInstallHelp();return}
  showInstallHelp();
  if(/iphone|ipad|ipod/i.test(navigator.userAgent)) setStatus("INSTALL FROM YOUR PHONE","Use Safari Share → Add to Home Screen. Then open the NexusAI icon and enable alerts.");
  else setStatus("INSTALL FROM BROWSER MENU","Use your browser menu and choose Install app or Add to Home screen.");
});
$("enableBtn").addEventListener("click",async()=>{
  $("enableBtn").disabled=true;$("enableBtn").textContent="ENABLING…";
  try{await registerPush()}catch(e){$("enableBtn").disabled=false;$("enableBtn").textContent="ENABLE SECURITY ALERTS";setStatus("NEXUSAI ALERTS NOT ENABLED",e.message,true)}
});
$("testBtn").addEventListener("click",sendTest);
$("activateBtn").addEventListener("click",activatePhone);
window.addEventListener("beforeinstallprompt",event=>{event.preventDefault();deferredInstall=event;showInstallHelp()});
window.addEventListener("appinstalled",()=>{setStatus("NEXUSAI INSTALLED","The NexusAI app is installed on this phone. Enable security alerts now.");showInstallHelp()});
(async()=>{
  showInstallHelp();
  if(siteId&&installToken){
  try{
    const cfg=await fetch(API+"/api/push/config",{cache:"no-store"}).then(r=>r.json());
    if(!cfg.configured)setStatus("PUSH SERVICE WAITING","The NexusAI app is built, but the secure VAPID keys still need to be configured on Render.");
    else {setStatus("NEXUSAI READY","This phone is linked to site "+siteId+". Enable security alerts to register this device.");$("enableBtn").disabled=false}
  }catch(e){setStatus("NEXUSAI CLOUD UNAVAILABLE","Reconnect to the internet and open this app again.",true)}
})();