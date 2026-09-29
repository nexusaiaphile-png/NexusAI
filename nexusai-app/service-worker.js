self.addEventListener("install", event => { self.skipWaiting(); });
self.addEventListener("activate", event => { event.waitUntil(self.clients.claim()); });
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = { body: event.data ? event.data.text() : "Security event detected." }; }
  const title = data.title || "NexusAI Security Alert";
  const options = { body: data.body || "A security event was detected.", icon: "/app/icon.svg", badge: "/app/icon.svg", tag: data.tag || "nexusai-security", renotify: true, requireInteraction: true, data: { url: data.url || "/app/" } };
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener("notificationclick", event => { event.notification.close(); event.waitUntil(clients.matchAll({type:"window",includeUncontrolled:true}).then(list => { const target=event.notification.data?.url || "/app/"; for (const client of list) { if ("focus" in client) { client.navigate(target); return client.focus(); } } if (clients.openWindow) return clients.openWindow(target); })); });
