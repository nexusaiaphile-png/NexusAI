(() => {
  "use strict";
  const qs = new URLSearchParams(location.search);
  const qr = qs.get("qr") || "";
  const $ = id => document.getElementById(id);
  let siteId = "";
  let installToken = "";
  let cameras = [];
  let refreshTimer = null;
  let localDiscoveryTimer = null;
  let automaticProtectionStarted = false;
  let localAgentOnline = false;
  let localCredentialsSaved = false;
  const LOCAL_AGENT = "http://127.0.0.1:8787";

  function show(id) {
    ["loading","activate","error","main"].forEach(x => $(x).classList.toggle("hidden", x !== id));
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

  async function localPermissionState() {
    if (!navigator.permissions?.query) return "prompt";
    try {
      const result = await navigator.permissions.query({name:"loopback-network"});
      return result.state || "prompt";
    } catch (_) {
      return "prompt";
    }
  }

  async function localFetch(path, options = {}) {
    const response = await fetch(LOCAL_AGENT + path, Object.assign({
      mode: "cors",
      targetAddressSpace: "loopback",
      cache: "no-store",
      headers: {"Cache-Control":"no-cache"}
    }, options));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "NexusAI Security Box request failed.");
    return data;
  }

  function showCctvSetup() {
    const card = $("cctvSetup");
    if (card) card.classList.remove("hidden");
  }

  function setLocalStatus(message) {
    if ($("localAgentStatus")) $("localAgentStatus").textContent = message;
  }

  async function checkLocalAgent(discover = false) {
    showCctvSetup();
    const permission = await localPermissionState();
    if (permission === "denied") {
      setLocalStatus("Browser access to the NexusAI Security Box is blocked. Allow Apps on device / loopback access for getnexusai.co.za, then check again.");
      return false;
    }
    try {
      const status = await localFetch("/setup/status");
      localAgentOnline = true;
      localCredentialsSaved = !!status.hikvision_credentials_saved;
      if (localCredentialsSaved) {
        setLocalStatus("Security Box is online. NexusAI is searching the CCTV network automatically…");
        if ($("cctvForm")) $("cctvForm").classList.add("hidden");
      } else {
        setLocalStatus("Security Box is online. Enter the Hikvision login below to start automatic discovery.");
        if ($("cctvForm")) $("cctvForm").classList.remove("hidden");
      }
      if (discover) {
        const devices = await localFetch("/discover");
        if (devices.devices?.length) {
          setLocalStatus(devices.devices.length + " Hikvision device(s) found. NexusAI is verifying the CCTV system…");
        }
      }
      return true;
    } catch (error) {
      localAgentOnline = false;
      setLocalStatus("Security Box is not connected yet. Install it on this computer, then click CHECK SECURITY BOX.");
      return false;
    }
  }

  async function connectCctv(event) {
    event.preventDefault();
    if (!localAgentOnline) {
      await checkLocalAgent(true);
      if (!localAgentOnline) return;
    }
    const button = $("connectCctvBtn");
    const status = $("cctvStatus");
    button.disabled = true;
    button.textContent = "CONNECTING…";
    status.textContent = "Saving the Hikvision login securely on this computer…";
    try {
      const payload = {
        username: $("hikUsername").value.trim(),
        password: $("hikPassword").value,
        location: "Security Site",
        nvr_ip: $("nvrIp").value.trim(),
        nvr_port: Number($("nvrPort").value || 80)
      };
      await localFetch("/credentials", {
        method:"POST",
        headers:{"Content-Type":"application/json","Cache-Control":"no-cache"},
        body:JSON.stringify(payload)
      });
      localCredentialsSaved = true;
      $("cctvForm").classList.add("hidden");
      status.textContent = "✓ Hikvision login saved locally. NexusAI is discovering and verifying the CCTV system…";
      setLocalStatus("Security Box is online. NexusAI is discovering the Hikvision system automatically…");
      await checkLocalAgent(true);
      await refresh();
    } catch (error) {
      status.textContent = error.message || "NexusAI could not connect the CCTV system.";
      button.disabled = false;
      button.textContent = "CONNECT CCTV AUTOMATICALLY";
    }
  }

  async function loadSession() {
    if (!qr) return fail("This NexusAI QR code is missing. Please scan the QR code on the protected site.");
    const response = await fetch("/api/protect/session?qr=" + encodeURIComponent(qr) + "&_=" + Date.now(), { cache: "no-store", headers: {"Cache-Control":"no-cache"} });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "The NexusAI site could not be found.");
    if (!data.activated) {
      show("activate");
      return;
    }
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
    if (data.cameras && data.cameras.length) {
      $("protectBtn").classList.add("hidden");
      if (!automaticProtectionStarted) {
        automaticProtectionStarted = true;
        await protectAll();
      }
    } else {
      $("protectBtn").classList.add("hidden");
    }
    await enableAlertsIfAlreadyGranted();
  }

  function renderCameras(list) {
    cameras = list;
    const box = $("cameras");
    box.innerHTML = "";
    if (!list.length) {
      $("cameraState").textContent = "NexusAI is connecting to the security system…";
      return;
    }
    const protectedCount = list.filter(c => c.status === "PROTECTED" || c.verified).length;
    $("cameraState").textContent = protectedCount
      ? protectedCount + " cameras protected."
      : list.length + " cameras found — NexusAI is protecting them automatically.";
    list.forEach((camera, i) => {
      const label = document.createElement("div");
      label.className = "camera";
      label.innerHTML = '<div><div class="name">' + escapeHtml(camera.camera_name || ("Camera " + (i+1))) + '</div><div class="loc">' + escapeHtml(camera.location || "Security camera") + '</div></div>';
      box.appendChild(label);
    });
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, ch => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[ch]));
  }

  async function activateSite() {
    const button = $("activateSiteBtn");
    const status = $("activateStatus");
    if (!button) return;
    button.disabled = true;
    button.textContent = "ACTIVATING…";
    status.textContent = "Creating your secure NexusAI site…";
    try {
      const response = await fetch("/api/protect/activate-qr", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({qr})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "This QR code could not be activated.");
      status.textContent = "✓ Site activated. Connecting your security system…";
      await loadSession();
    } catch (error) {
      status.textContent = error.message || "Activation could not be completed.";
      button.disabled = false;
      button.textContent = "ACTIVATE NEXUSAI";
    }
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
    const selected = cameras.map(camera => camera.camera_id);
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
      const response = await fetch("/api/protect/session?qr=" + encodeURIComponent(qr) + "&_=" + Date.now(), {cache:"no-store", headers: {"Cache-Control":"no-cache"}});
      const data = await response.json();
      if (!response.ok) return;
      installToken = data.install_token;
      if (data.cameras) {
        renderCameras(data.cameras);
        if (data.cameras.length) {
          $("protectBtn").classList.add("hidden");
          if (!automaticProtectionStarted) {
            automaticProtectionStarted = true;
            await protectAll();
          }
        }
      }
      setConnection(data.edge_agent === "ONLINE" ? "SECURITY SYSTEM ONLINE" : "CONNECTING TO SECURITY SYSTEM", data.edge_agent === "ONLINE");
    } catch (_) {}
  }

  $("activateSiteBtn").addEventListener("click", activateSite);
  $("alertsBtn").addEventListener("click", enableAlerts);
  $("protectBtn").addEventListener("click", protectAll);
  $("connectCctvBtn").addEventListener("click", connectCctv);
  $("cctvForm").addEventListener("submit", connectCctv);
  $("checkBoxBtn").addEventListener("click", () => checkLocalAgent(true));
  $("installBoxBtn").addEventListener("click", () => setTimeout(() => checkLocalAgent(false), 2500));

  loadSession().then(() => {
    localPermissionState().then(state => {
      if (state === "granted") checkLocalAgent(false);
    }).catch(() => {});
  }).catch(error => fail(error.message || "Please scan the NexusAI QR code again."));
  refreshTimer = setInterval(refresh, 5000);
  localDiscoveryTimer = setInterval(() => {
    if (localAgentOnline && localCredentialsSaved) checkLocalAgent(false);
  }, 10000);
})();
