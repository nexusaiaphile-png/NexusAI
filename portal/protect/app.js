(() => {
  "use strict";
  const qs = new URLSearchParams(location.search);
  const qr = qs.get("qr") || "";
  const $ = id => document.getElementById(id);
  let siteId = "";
  let installToken = "";
  let cameras = [];
  let refreshTimer = null;

  function show(id) {
    ["loading","error","main"].forEach(x => $(x).classList.toggle("hidden", x !== id));
  }
  function fail(message) {
    $("errorText").textContent = message;
    show("error");
  }
  function setConnection(text, good = false) {
    $("connection").textContent = text;
    $("connection").style.color = good ? "#55d1ad" : "#e6b66b";
  }
  function b64ToBytes(value) {
    const padding = "=".repeat((4 - value.length % 4) % 4);
    const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  }

  async function loadSession() {
    if (!qr) return fail("This NexusAI QR code is missing. Please scan the QR code on the protected site.");
    const response = await fetch("/api/protect/session?qr=" + encodeURIComponent(qr), { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "The NexusAI site could not be found.");
    siteId = data.site_id;
    installToken = data.install_token;
    renderCameras(data.cameras || []);
    const installBtn = $("installBoxBtn");
    if (installBtn) {
      installBtn.href = "/protect/install?qr=" + encodeURIComponent(qr);
      installBtn.classList.toggle("hidden", data.edge_agent === "ONLINE");
      $("boxStatus").textContent = data.edge_agent === "ONLINE"
        ? "Security Box connected."
        : "The Security Box is not connected yet. Install it on a computer connected to the CCTV network.";
    }
    setConnection(data.edge_agent === "ONLINE" ? "SECURITY SYSTEM ONLINE" : "CONNECTING TO SECURITY SYSTEM", data.edge_agent === "ONLINE");
    show("main");
    if (data.cameras && data.cameras.length) $("protectBtn").classList.remove("hidden");
    else $("protectBtn").classList.add("hidden");
    await enableAlertsIfAlreadyGranted();
  }

  function renderCameras(list) {
    cameras = list;
    const box = $("cameras");
    box.innerHTML = "";
    if (!list.length) {
      $("cameraState").textContent = "NexusAI is waiting for the site's Security Engine. This page will keep checking automatically.";
      return;
    }
    const protectedCount = list.filter(c => c.status === "PROTECTED" || c.verified).length;
    $("cameraState").textContent = protectedCount ? protectedCount + " cameras already protected." : list.length + " cameras found.";
    list.forEach((camera, i) => {
      const label = document.createElement("label");
      label.className = "camera";
      label.innerHTML = '<input type="checkbox" checked data-camera-id="' + String(camera.camera_id).replace(/"/g,"&quot;") + '"><div><div class="name">' + escapeHtml(camera.camera_name || ("Camera " + (i+1))) + '</div><div class="loc">' + escapeHtml(camera.location || "Security camera") + '</div></div>';
      box.appendChild(label);
    });
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, ch => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[ch]));
  }

  async function enableAlerts() {
    const status = $("alertsStatus");
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      status.textContent = "This browser does not support NexusAI phone alerts. Open NexusAI in a supported mobile browser.";
      return;
    }
    $("alertsBtn").disabled = true;
    status.textContent = "Turning on security alerts…";
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") throw new Error("Notifications were not allowed.");
      const configRes = await fetch("/api/push/config", { cache: "no-store" });
      const config = await configRes.json();
      if (!config.public_key) throw new Error("NexusAI alerts are not configured yet.");
      const registration = await navigator.serviceWorker.register("/app/service-worker.js", { scope: "/app/" });
      await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(config.public_key) });
      }
      const response = await fetch("/api/push/subscribe", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ site_id: siteId, install_token: installToken, subscription: subscription.toJSON() })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "Could not enable alerts.");
      status.textContent = "✓ This phone will receive NexusAI security alerts.";
      $("alertsBtn").textContent = "SECURITY ALERTS ENABLED";
      $("alertsBtn").disabled = true;
    } catch (error) {
      status.textContent = error.message || "Could not enable alerts.";
      $("alertsBtn").disabled = false;
    }
  }

  async function enableAlertsIfAlreadyGranted() {
    if ("Notification" in window && Notification.permission === "granted") {
      await enableAlerts();
    }
  }

  async function protectAll() {
    const selected = [...document.querySelectorAll("#cameras input[type=checkbox]:checked")].map(x => x.dataset.cameraId);
    if (!selected.length) return;
    $("protectBtn").disabled = true;
    $("protectBtn").textContent = "PROTECTING…";
    try {
      const response = await fetch("/api/protect/cameras", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({site_id: siteId, install_token: installToken, camera_ids: selected})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "NexusAI could not protect these cameras.");
      $("protectedCount").textContent = data.protected_count;
      $("doneText").textContent = "NexusAI is now monitoring these cameras. This phone will receive security alerts.";
      $("done").classList.remove("hidden");
      $("protectBtn").classList.add("hidden");
      $("cameraState").textContent = data.protected_count + " cameras protected.";
      document.querySelectorAll("#cameras input").forEach(x => x.disabled = true);
      setConnection("NEXUSAI PROTECTED", true);
      window.scrollTo({top: document.body.scrollHeight, behavior: "smooth"});
    } catch (error) {
      $("alertsStatus").textContent = error.message || "Protection could not be completed.";
      $("protectBtn").disabled = false;
      $("protectBtn").textContent = "PROTECT ALL CAMERAS";
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/protect/session?qr=" + encodeURIComponent(qr), {cache:"no-store"});
      const data = await response.json();
      if (!response.ok) return;
      installToken = data.install_token;
      if (data.cameras) {
        renderCameras(data.cameras);
        if (data.cameras.length) $("protectBtn").classList.remove("hidden");
      }
      setConnection(data.edge_agent === "ONLINE" ? "SECURITY SYSTEM ONLINE" : "CONNECTING TO SECURITY SYSTEM", data.edge_agent === "ONLINE");
    } catch (_) {}
  }

  $("alertsBtn").addEventListener("click", enableAlerts);
  $("protectBtn").addEventListener("click", protectAll);

  loadSession().catch(error => fail(error.message || "Please scan the NexusAI QR code again."));
  refreshTimer = setInterval(refresh, 5000);
})();