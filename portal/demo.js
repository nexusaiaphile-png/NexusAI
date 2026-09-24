/* NexusAI Portal Demo Mode
 * Enable with: /portal/?demo=1
 * This never runs in normal production mode.
 */
(function () {
  const params = new URLSearchParams(window.location.search);
  if (params.get("demo") !== "1") return;

  const originalFetch = window.fetch.bind(window);
  const demoDevice = {
    name: "NexusAI Demo Hikvision NVR",
    ip: "192.168.1.200",
    port: 80,
    type: "Hikvision NVR",
    discovery: "DEMO_SIMULATION"
  };

  const demoChannels = [
    { channel_id: "101", channel_name: "Main Entrance", enabled: true },
    { channel_id: "201", channel_name: "Sales Floor", enabled: true },
    { channel_id: "301", channel_name: "Stock Room", enabled: true },
    { channel_id: "401", channel_name: "Back Door", enabled: true }
  ];

  function jsonResponse(payload, status = 200) {
    return new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" }
    });
  }

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    const path = new URL(url, window.location.href).pathname;

    if (path === "/health") {
      return jsonResponse({
        service: "NexusAI Edge Agent",
        status: "ONLINE",
        version: "DEMO-1.0",
        site_id: localStorage.getItem("nexusai_site_id") || "demo-site"
      });
    }

    if (path === "/configure") {
      let body = {};
      try { body = JSON.parse(init?.body || "{}"); } catch (_) {}
      return jsonResponse({ configured: true, site_id: body.site_id || "demo-site" });
    }

    if (path === "/discover") {
      await new Promise(r => setTimeout(r, 900));
      return jsonResponse({
        service: "NexusAI Edge Agent",
        status: "ONLINE",
        network: "DEMO_SIMULATION",
        devices: [demoDevice]
      });
    }

    if (path === "/verify") {
      await new Promise(r => setTimeout(r, 700));
      return jsonResponse({
        camera_id: "demo-nvr-192-168-1-200",
        camera_name: demoDevice.name,
        location: "Client site",
        network: "CONNECTED",
        camera: "DETECTED",
        credentials: "ACCEPTED",
        nexusai: "CONNECTED",
        verified: true,
        device_type: "NVR",
        channels: demoChannels
      });
    }

    if (path === "/activate") {
      await new Promise(r => setTimeout(r, 700));
      return jsonResponse({
        camera_id: "demo-nvr-192-168-1-200",
        camera_name: demoDevice.name,
        location: "Client site",
        network: "CONNECTED",
        camera: "DETECTED",
        credentials: "ACCEPTED",
        nexusai: "CONNECTED",
        verified: true,
        device_type: "NVR",
        channels: demoChannels,
        monitoring: "DEMO_MONITORING_STARTED",
        monitoring_started: true
      });
    }

    if (path === "/api/portal/status") {
      return jsonResponse({
        edge_agent: "ONLINE",
        status: "ONLINE",
        agent_version: "DEMO-1.0",
        cameras: []
      });
    }

    return originalFetch(input, init);
  };

  document.addEventListener("DOMContentLoaded", function () {
    const banner = document.createElement("div");
    banner.textContent = "NEXUSAI DEMO MODE • SIMULATED HIKVISION DEVICE • NO REAL CAMERA CONNECTION";
    Object.assign(banner.style, {
      position: "fixed",
      top: "0",
      left: "0",
      right: "0",
      zIndex: "99999",
      padding: "9px 14px",
      textAlign: "center",
      font: "700 12px/1.2 Arial,sans-serif",
      letterSpacing: "1px",
      background: "#0b1720",
      color: "#55f2b0",
      borderBottom: "1px solid #55f2b0"
    });
    document.body.prepend(banner);
  });
})();
