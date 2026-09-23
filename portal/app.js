// ============================================================
// NEXUSAI CLIENT PORTAL
// Connected to live NexusAI backend
// ============================================================

const API_BASE_URL = window.location.origin;
const EDGE_AGENT_URL = "http://127.0.0.1:8787";
const SITE_ID = localStorage.getItem("nexusai_site_id") || "site-demo";
let edgeAgentOnline = false;

// ============================================================
// STATE
// ============================================================

function readStoredArray(key) {
    try {
        const value = JSON.parse(localStorage.getItem(key) || "[]");
        return Array.isArray(value) ? value : [];
    } catch (error) {
        console.warn("NexusAI local storage reset:", key, error);
        localStorage.removeItem(key);
        return [];
    }
}

let cameras = readStoredArray("nexusai_cameras");
let events = readStoredArray("nexusai_events");

let selectedCameraCount = 1;
let verificationPassed = false;
let verificationData = null;

// ============================================================
// DOM HELPERS
// ============================================================

const $ = (id) => document.getElementById(id);

function show(element) {
    if (!element) return;

    if (
        element.classList.contains("modal") ||
        element.classList.contains("screen")
    ) {
        element.classList.add("active");
        return;
    }

    element.style.display = "";
}

function hide(element) {
    if (!element) return;

    if (
        element.classList.contains("modal") ||
        element.classList.contains("screen")
    ) {
        element.classList.remove("active");
        return;
    }

    element.style.display = "none";
}

function escapeHTML(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// ============================================================
// LOGIN
// ============================================================

document.addEventListener("DOMContentLoaded", () => {
    initializePortal();
});

function initializePortal() {
    const loggedIn = localStorage.getItem("nexusai_logged_in");

    if (loggedIn === "true") {
        showDashboard();
    } else {
        showLogin();
    }

    bindEvents();
}

function bindEvents() {
    $("loginForm")?.addEventListener("submit", handleLogin);

    $("logoutBtn")?.addEventListener("click", handleLogout);

    $("activateBtn")?.addEventListener(
        "click",
        openActivationModal
    );

    $("emptyActivateBtn")?.addEventListener(
        "click",
        openActivationModal
    );

    $("addCameraBtn")?.addEventListener(
        "click",
        openCameraModal
    );

    $("closeModal")?.addEventListener(
        "click",
        closeActivationModal
    );

    $("closeCameraModal")?.addEventListener(
        "click",
        closeCameraModal
    );

    $("cameraForm")?.addEventListener(
        "submit",
        handleCameraVerification
    );

    $("verifyCameraBtn")?.addEventListener(
        "click",
        handleCameraVerification
    );

    $("retryVerificationBtn")?.addEventListener(
        "click",
        resetVerification
    );

    $("activateCameraBtn")?.addEventListener(
        "click",
        activateVerifiedCamera
    );

    document.addEventListener("click", (event) => {
        if (
            event.target === $("activationModal")
        ) {
            closeActivationModal();
        }

        if (
            event.target === $("cameraModal")
        ) {
            closeCameraModal();
        }
    });
}

// ============================================================
// LOGIN
// ============================================================

function showLogin() {
    show($("loginScreen"));
    hide($("dashboardScreen"));
}

function showDashboard() {
    hide($("loginScreen"));
    show($("dashboardScreen"));

    renderDashboard();
}

function handleLogin(event) {
    event.preventDefault();

    localStorage.setItem(
        "nexusai_logged_in",
        "true"
    );

    showDashboard();
}

function handleLogout() {
    localStorage.removeItem(
        "nexusai_logged_in"
    );

    showLogin();
}

// ============================================================
// ACTIVATION MODAL
// ============================================================

function openActivationModal() {
    show($("activationModal"));

    selectedCameraCount = 1;

    const countButtons =
        document.querySelectorAll(
            "[data-count]"
        );

    countButtons.forEach((button) => {
        button.classList.remove("active");

        if (
            Number(button.dataset.count) === 1
        ) {
            button.classList.add("active");
        }

        button.onclick = () => {
            selectedCameraCount = Number(button.dataset.count);

            countButtons.forEach((item) =>
                item.classList.remove("active")
            );

            button.classList.add("active");

            setTimeout(() => {
                closeActivationModal();
                openCameraModal();
            }, 180);
        };
    });
}

function closeActivationModal() {
    hide($("activationModal"));
}

// ============================================================
// CAMERA MODAL
// ============================================================

function openCameraModal() {
    closeActivationModal();

    show($("cameraModal"));

    resetVerification();

    if ($("cameraSetupTitle")) {
        $("cameraSetupTitle").textContent =
            cameras.length > 0
                ? "Add Camera"
                : "Activate NexusAI";
    }
}

function closeCameraModal() {
    hide($("cameraModal"));
}

// ============================================================
// RESET VERIFICATION
// ============================================================

function resetVerification() {
    verificationPassed = false;
    verificationData = null;

    hide($("verificationBox"));
    hide($("verificationSuccess"));
    hide($("verificationError"));

    show($("cameraForm"));

    if ($("activateCameraBtn")) {
        $("activateCameraBtn").disabled = true;
    }

    setCheck(
        "networkCheck",
        "Waiting"
    );

    setCheck(
        "cameraCheck",
        "Waiting"
    );

    setCheck(
        "credentialsCheck",
        "Waiting"
    );

    setCheck(
        "nexusCheck",
        "Waiting"
    );
}

// ============================================================
// REAL CAMERA VERIFICATION
// ============================================================

async function handleCameraVerification(event) {
    if (event) {
        event.preventDefault();
    }

    const cameraName =
        $("cameraName")?.value.trim();

    const cameraIp =
        $("cameraIp")?.value.trim();

    const username =
        $("cameraUsername")?.value.trim();

    const password =
        $("cameraPassword")?.value;

    const location =
        $("cameraLocation")?.value.trim();

    if (
        !cameraName ||
        !cameraIp ||
        !username ||
        !password ||
        !location
    ) {
        showVerificationError(
            "Please complete all camera details before testing."
        );

        return;
    }

    verificationPassed = false;

    hide($("verificationSuccess"));
    hide($("verificationError"));
    show($("verificationBox"));

    if ($("verificationTitle")) {
        $("verificationTitle").textContent =
            "NexusAI Verification";
    }

    setCheck(
        "networkCheck",
        "Checking..."
    );

    setCheck(
        "cameraCheck",
        "Waiting..."
    );

    setCheck(
        "credentialsCheck",
        "Waiting..."
    );

    setCheck(
        "nexusCheck",
        "Waiting..."
    );

    if ($("verifyCameraBtn")) {
        $("verifyCameraBtn").disabled = true;
        $("verifyCameraBtn").textContent =
            "VERIFYING...";
    }

    try {
        const response = await fetch(
            `${EDGE_AGENT_URL}/verify`,
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify({
                    camera_id: `camera-${Date.now()}`,
                    camera_name: cameraName,
                    camera_ip: cameraIp,
                    camera_port: 80,
                    username: username,
                    password: password,
                    location: location
                })
            }
        );

        let result;

        try {
            result = await response.json();
        } catch {
            throw new Error(
                "NexusAI returned an invalid response."
            );
        }

        updateVerificationChecks(result);

        if (
            response.ok &&
            result.verified === true
        ) {
            verificationPassed = true;
            verificationData = result;

            showVerificationSuccess();

        } else {
            verificationPassed = false;

            showVerificationError(
                result.error ||
                "NexusAI could not verify this camera. Check the camera details and Edge Agent connection."
            );
        }

    } catch (error) {
        console.error(
            "NexusAI verification error:",
            error
        );

        setCheck(
            "networkCheck",
            "API ERROR"
        );

        setCheck(
            "cameraCheck",
            "NOT CHECKED"
        );

        setCheck(
            "credentialsCheck",
            "NOT CHECKED"
        );

        setCheck(
            "nexusCheck",
            "NOT CONNECTED"
        );

        showVerificationError(
            "NexusAI Edge Agent is not running on this computer. Start the Edge Agent, then test the camera again."
        );

    } finally {
        if ($("verifyCameraBtn")) {
            $("verifyCameraBtn").disabled = false;
            $("verifyCameraBtn").textContent =
                "TEST CAMERA";
        }
    }
}

// ============================================================
// VERIFICATION UI
// ============================================================

function updateVerificationChecks(result) {
    setCheck(
        "networkCheck",
        result.network || "UNKNOWN"
    );

    setCheck(
        "cameraCheck",
        result.camera || "UNKNOWN"
    );

    setCheck(
        "credentialsCheck",
        result.credentials || "UNKNOWN"
    );

    setCheck(
        "nexusCheck",
        result.nexusai || "UNKNOWN"
    );
}

function setCheck(id, status) {
    const element = $(id);

    if (!element) return;

    const normalized =
        String(status)
            .toUpperCase();

    element.textContent = normalized;

    element.classList.remove(
        "success",
        "error",
        "warning"
    );

    if (
        [
            "CONNECTED",
            "DETECTED",
            "ACCEPTED",
            "READY"
        ].includes(normalized)
    ) {
        element.classList.add(
            "success"
        );
    } else if (
        [
            "REJECTED",
            "NOT CONNECTED",
            "NOT DETECTED",
            "UNREACHABLE",
            "ERROR",
            "API ERROR"
        ].includes(normalized)
    ) {
        element.classList.add(
            "error"
        );
    } else {
        element.classList.add(
            "warning"
        );
    }
}

function showVerificationSuccess() {
    hide($("verificationError"));
    show($("verificationSuccess"));

    if ($("verificationTitle")) {
        $("verificationTitle").textContent =
            "Camera Verified";
    }

    if ($("activateCameraBtn")) {
        $("activateCameraBtn").disabled =
            false;
    }
}

function showVerificationError(message) {
    hide($("verificationSuccess"));
    show($("verificationError"));

    if ($("verificationErrorText")) {
        $("verificationErrorText").textContent =
            message;
    }
}

// ============================================================
// ACTIVATE VERIFIED CAMERA
// ============================================================

async function activateVerifiedCamera(event) {
    if (event) event.preventDefault();
    if (!verificationPassed || !verificationData) return;

    const device = {
        name: $("cameraName").value.trim(),
        ip: $("cameraIp").value.trim(),
        username: $("cameraUsername").value.trim(),
        location: $("cameraLocation").value.trim()
    };

    const activateButton = $("activateCameraBtn");
    if (activateButton) {
        activateButton.disabled = true;
        activateButton.textContent = "ACTIVATING...";
    }

    try {
        const response = await fetch(`${EDGE_AGENT_URL}/activate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                camera_id: "device-" + Date.now(),
                camera_name: device.name,
                camera_ip: device.ip,
                camera_port: 80,
                username: device.username,
                password: $("cameraPassword").value,
                location: device.location
            })
        });

        const activation = await response.json();
        if (!response.ok || activation.verified !== true) {
            throw new Error(
                activation.error ||
                "NexusAI Edge Agent is required before this camera can be activated."
            );
        }

        const channels = Array.isArray(activation.channels) ? activation.channels : [];
        const isNvr = activation.device_type === "NVR" || channels.length > 1;

        if (isNvr && channels.length) {
            channels.forEach((channel) => {
                cameras.push({
                    id: device.ip + "-" + channel.channel_id,
                    name: channel.channel_name || ("Channel " + channel.channel_id),
                    ip: device.ip,
                    username: device.username,
                    location: device.location,
                    channelId: channel.channel_id,
                    deviceType: "NVR_CHANNEL",
                    status: "ACTIVE",
                    protection: "NEXUSAI PROTECTED",
                    addedAt: new Date().toISOString()
                });
            });

            addLocalEvent({
                type: "NVR PROTECTION ACTIVATED",
                camera: channels.length + " channels discovered",
                location: device.location,
                timestamp: new Date().toISOString()
            });
        } else {
            cameras.push({
                id: Date.now().toString(),
                name: device.name,
                ip: device.ip,
                username: device.username,
                location: device.location,
                status: "ACTIVE",
                protection: "NEXUSAI PROTECTED",
                addedAt: new Date().toISOString()
            });

            addLocalEvent({
                type: "NEXUSAI PROTECTION ACTIVATED",
                camera: device.name,
                location: device.location,
                timestamp: new Date().toISOString()
            });
        }

        localStorage.setItem("nexusai_cameras", JSON.stringify(cameras));
        closeCameraModal();
        clearCameraForm();
        renderDashboard();
    } catch (error) {
        console.error("NexusAI activation error:", error);
        showVerificationError(error.message || "Activation failed. Make sure the NexusAI Edge Agent is running.");
    } finally {
        if (activateButton) {
            activateButton.disabled = false;
            activateButton.textContent = "ACTIVATE CAMERA";
        }
    }
}

// ============================================================
// CLEAR CAMERA FORM
// ============================================================

function clearCameraForm() {
    [
        "cameraName",
        "cameraIp",
        "cameraUsername",
        "cameraPassword",
        "cameraLocation"
    ].forEach((id) => {
        if ($(id)) {
            $(id).value = "";
        }
    });

    resetVerification();
}

// ============================================================
// DASHBOARD
// ============================================================

function renderDashboard() {
    updateStats();
    renderCameras();
    renderEvents();
    checkEdgeAgent();
    syncRemoteEvents();
}

function updateStats() {
    if ($("activeCameras")) {
        $("activeCameras").textContent =
            cameras.length;
    }

    if ($("eventCount")) {
        $("eventCount").textContent =
            events.length;
    }
}

// ============================================================
// CAMERA LIST
// ============================================================

function renderCameras() {
    const list = $("cameraList");
    const empty = $("emptyCameras");

    if (!list) return;

    if (cameras.length === 0) {
        list.innerHTML = `
            <div class="empty-state" id="emptyCameras">
                <div class="empty-icon">📹</div>
                <h3>No cameras activated</h3>
                <p>Activate your first camera to start NexusAI protection.</p>
                <button class="primary-btn" id="emptyActivateBtn">
                    ACTIVATE NEXUSAI
                </button>
            </div>
        `;

        $("emptyActivateBtn")?.addEventListener(
            "click",
            openActivationModal
        );

        return;
    }

    hide(empty);

    list.innerHTML = cameras
        .map(
            (camera) => `
                <div class="camera-card">
                    <div class="camera-card-header">
                        <strong>
                            ${escapeHTML(camera.name)}
                        </strong>

                        <span class="status-badge success">
                            ● ACTIVE
                        </span>
                    </div>

                    <div class="camera-details">
                        <div>
                            <span>Location</span>
                            <strong>
                                ${escapeHTML(camera.location)}
                            </strong>
                        </div>

                        <div>
                            <span>Protection</span>
                            <strong>
                                NEXUSAI PROTECTED
                            </strong>
                        </div>

                        <div>
                            <span>Camera</span>
                            <strong>
                                ONLINE
                            </strong>
                        </div>
                    </div>
                </div>
            `
        )
        .join("");
}

// ============================================================
// SECURITY EVENTS
// ============================================================

function addLocalEvent(event) {
    events.unshift(event);

    events = events.slice(0, 50);

    localStorage.setItem(
        "nexusai_events",
        JSON.stringify(events)
    );
}

function renderEvents() {
    const list = $("eventList");

    if (!list) return;

    if (events.length === 0) {
        list.innerHTML = `
            <div class="empty-state">
                <p>No security events yet.</p>
            </div>
        `;

        return;
    }

    list.innerHTML = events
        .map((event) => {
            const date =
                new Date(event.timestamp);

            return `
                <div class="event-row">
                    <div class="event-icon">
                        🚨
                    </div>

                    <div class="event-information">
                        <strong>
                            ${escapeHTML(event.type)}
                        </strong>

                        <span>
                            ${escapeHTML(event.camera)}
                            •
                            ${escapeHTML(event.location)}
                        </span>
                    </div>

                    <time>
                        ${date.toLocaleString()}
                    </time>
                </div>
            `;
        })
        .join("");
}

// ============================================================
// BACKEND HEALTH CHECK
// ============================================================

async function checkNexusAIBackend() {
    try {
        const response = await fetch(
            `${API_BASE_URL}/health`,
            {
                method: "GET",
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                "Backend unavailable"
            );
        }

        const result =
            await response.json();

        console.log(
            "NexusAI backend:",
            result
        );

        return true;

    } catch (error) {
        console.error(
            "NexusAI backend health check failed:",
            error
        );

        return false;
    }
}

// Run health check when portal loads.
checkNexusAIBackend();
async function checkEdgeAgent() {
    try {
        const response = await fetch(EDGE_AGENT_URL + "/health", {
            method: "GET",
            cache: "no-store"
        });
        edgeAgentOnline = response.ok;
    } catch {
        edgeAgentOnline = false;
    }
    updateEdgeStatus();
}

async function syncRemoteEvents() {
    try {
        const response = await fetch(
            API_BASE_URL + "/api/portal/events?site_id=" +
            encodeURIComponent(SITE_ID) + "&limit=50",
            { cache: "no-store" }
        );
        if (!response.ok) return;

        const payload = await response.json();
        const remoteEvents = Array.isArray(payload.events) ? payload.events : [];

        const normalized = remoteEvents.map((event) => ({
            type: event.event,
            camera: event.camera_name,
            location: event.location,
            severity: event.severity,
            timestamp: event.timestamp
        }));

        const localOnly = events.filter(
            (local) => !normalized.some(
                (remote) =>
                    remote.timestamp === local.timestamp &&
                    remote.camera === local.camera
            )
        );

        events = [...normalized, ...localOnly].slice(0, 50);
        localStorage.setItem("nexusai_events", JSON.stringify(events));
        renderEvents();
        updateStats();
    } catch (error) {
        console.warn("NexusAI remote event sync unavailable:", error);
    }
}

function updateEdgeStatus() {
    const status = document.querySelector(".system-status");
    if (!status) return;

    status.innerHTML = edgeAgentOnline
        ? "<span></span> EDGE AGENT ONLINE"
        : "<span></span> CLOUD ONLINE • EDGE AGENT OFFLINE";
}

setInterval(() => {
    if (localStorage.getItem("nexusai_logged_in") === "true") {
        checkEdgeAgent();
        syncRemoteEvents();
    }
}, 15000);
