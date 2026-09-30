const CACHE="nexusai-v3";
const ASSETS=["/app/","/app/index.html","/app/app.js","/app/manifest.json","/app/icon.svg"];
self.addEventListener("install",event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting()))});
self.addEventListener("activate",event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener("fetch",event=>{
  if(event.request.method!=="GET")return;
  event.respondWith(fetch(event.request).then(response=>{if(response.ok&&new URL(event.request.url).origin===self.location.origin){const copy=response.clone();caches.open(CACHE).then(c=>c.put(event.request,copy)).catch(()=>{})}return response}).catch(()=>caches.match(event.request).then(r=>r||caches.match("/app/"))));
});
self.addEventListener("push",event=>{
  let data={};
  try{data=event.data?event.data.json():{}}catch(_){data={body:event.data?event.data.text():"NexusAI security event detected."}}
  const title=data.title||"NexusAI Security Alert";
  const severity=(data.severity||"HIGH").toUpperCase();
  const options={
    body:data.body||"A security event was detected.",
    icon:"/app/icon.svg",
    badge:"/app/icon.svg",
    tag:data.tag||("nexusai-"+Date.now()),
    renotify:true,
    requireInteraction:severity==="CRITICAL",
    vibrate:[200,100,200],
    timestamp:Date.now(),
    data:{url:data.url||"/app/"}
  };
  event.waitUntil(self.registration.showNotification(title,options));
});
self.addEventListener("notificationclick",event=>{
  event.notification.close();
  const url=event.notification.data?.url||"/app/";
  event.waitUntil(clients.matchAll({type:"window",includeUncontrolled:true}).then(list=>{
    for(const client of list){if("focus" in client){client.navigate(url);return client.focus()}}
    return clients.openWindow?clients.openWindow(url):undefined;
  }));
});
self.addEventListener("pushsubscriptionchange",event=>{
  // The page re-registers the current subscription whenever the user opens
  // NexusAI. The event is intentionally kept lightweight and never blocks push.
});