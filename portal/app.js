/* =========================================================
   NEXUSAI CLIENT PORTAL
   Frontend application logic
   ========================================================= */

const loginScreen = document.getElementById("loginScreen");
const dashboardScreen = document.getElementById("dashboardScreen");

const loginForm = document.getElementById("loginForm");
const logoutBtn = document.getElementById("logoutBtn");

const activationModal = document.getElementById("activationModal");
const cameraModal = document.getElementById("cameraModal");

const activateBtn = document.getElementById("activateBtn");
const emptyActivateBtn = document.getElementById("emptyActivateBtn");
const addCameraBtn = document.getElementById("addCameraBtn");

const closeModal = document.getElementById("closeModal");
const closeCameraModal = document.getElementById("closeCameraModal");

const cameraForm = document.getElementById("cameraForm");
const verifyCameraBtn = document.getElementById("verifyCameraBtn");

const verificationBox = document.getElementById("verificationBox");
const verificationSuccess =
    document.getElementById("verificationSuccess");
const verificationError =
    document.getElementById("verificationError");

const activateCameraBtn =
    document.getElementById("activateCameraBtn");

const retryVerificationBtn =
    document.getElementById("retryVerificationBtn");

const verificationTitle =
    document.getElementById("verificationTitle");

const verificationErrorText =
    document.getElementById("verificationErrorText");

const activeCameras =
    document.getElementById("activeCameras");

const eventCount =
    document.getElementById("eventCount");

const cameraList =
    document.getElementById("cameraList");

const emptyCameras =
    document.getElementById("emptyCameras");

const eventList =
    document.getElementById("eventList");

const cameraSetupTitle =
    document.getElementById("cameraSetupTitle");

const cameraName =
    document.getElementById("cameraName");

const cameraIp =
    document.getElementById("cameraIp");

const cameraUsername =
    document.getElementById("cameraUsername");

const cameraPassword =
    document.getElementById("cameraPassword");

const cameraLocation =
    document.getElementById("cameraLocation");

const networkCheck =
    document.getElementById("networkCheck");

const cameraCheck =
    document.getElementById("cameraCheck");

const credentialsCheck =
    document.getElementById("credentialsCheck");

const nexusCheck =
    document.getElementById("nexusCheck");


/* =========================================================
   APPLICATION STATE
   ========================================================= */

let cameras = [];
let securityEvents = [];
let selectedCameraNumber = 1;
let verificationPassed = false;


/* =========================================================
   INITIALIZATION
   ========================================================= */

document.addEventListener("DOMContentLoaded", () => {
    loadLocalData();
    updateDashboard();

    if (localStorage.getItem("nexusai_logged_in") === "true") {
        showDashboard();
    } else {
        showLogin();
    }
});


/* =========================================================
   LOGIN
   ========================================================= */

loginForm.addEventListener("submit", (event) => {
    event.preventDefault();

    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value.trim();

    if (!email || !password) {
        return;
    }

    /*
       DEMO AUTHENTICATION

       Real authentication will be connected to the
       NexusAI backend later.

       No password is being sent anywhere here.
    */

    localStorage.setItem("nexusai_logged_in", "true");
    localStorage.setItem("nexusai_user_email", email);

    showDashboard();
});


function showLogin() {
    loginScreen.classList.add("active");
    dashboardScreen.classList.remove("active");
}


function showDashboard() {
    loginScreen.classList.remove("active");
    dashboardScreen.classList.add("active");

    updateDashboard();
}


/* =========================================================
   LOGOUT
   ========================================================= */

logoutBtn.addEventListener("click", () => {
    localStorage.removeItem("nexusai_logged_in");
    showLogin();
});


/* =========================================================
   ACTIVATION MODAL
   ========================================================= */

function openActivationModal() {
    activationModal.classList.add("active");
}


function closeActivationModal() {
    activationModal.classList.remove("active");
}


activateBtn.addEventListener("click", openActivationModal);

emptyActivateBtn.addEventListener(
    "click",
    openActivationModal
);

addCameraBtn.addEventListener(
    "click",
    openActivationModal
);

closeModal.addEventListener(
    "click",
    closeActivationModal
);


/* =========================================================
   CAMERA COUNT
   ========================================================= */

document.querySelectorAll(".count-btn").forEach((button) => {

    button.addEventListener("click", () => {

        selectedCameraNumber =
            Number(button.dataset.count);

        /*
           For now we configure one camera at a time.
           The backend will later support complete
           multi-camera onboarding.
        */

        closeActivationModal();

        openCameraSetup(
            cameras.length + 1
        );
    });

});


/* =========================================================
   CAMERA SETUP
   ========================================================= */

function openCameraSetup(cameraNumber) {

    selectedCameraNumber = cameraNumber;

    cameraSetupTitle.textContent =
        `Camera ${cameraNumber}`;

    cameraForm.reset();

    resetVerification();

    cameraModal.classList.add("active");
}


function closeCameraSetup() {
    cameraModal.classList.remove("active");
}


closeCameraModal.addEventListener(
    "click",
    closeCameraSetup
);


/* =========================================================
   RESET VERIFICATION
   ========================================================= */

function resetVerification() {

    verificationBox.classList.add("hidden");

    verificationSuccess.classList.add("hidden");

    verificationError.classList.add("hidden");

    verificationPassed = false;

    networkCheck.textContent = "...";
    cameraCheck.textContent = "...";
    credentialsCheck.textContent = "...";
    nexusCheck.textContent = "...";
}


/* =========================================================
   CAMERA VERIFICATION
   ========================================================= */

verifyCameraBtn.addEventListener(
    "click",
    verifyCamera
);


async function verifyCamera() {

    const name =
        cameraName.value.trim();

    const ip =
        cameraIp.value.trim();

    const username =
        cameraUsername.value.trim();

    const password =
        cameraPassword.value.trim();

    const location =
        cameraLocation.value.trim();


    if (
        !name ||
        !ip ||
        !username ||
        !password ||
        !location
    ) {
        alert(
            "Please complete all camera details before testing."
        );

        return;
    }


    verificationBox.classList.remove(
        "hidden"
    );

    verificationSuccess.classList.add(
        "hidden"
    );

    verificationError.classList.add(
        "hidden"
    );


    verificationTitle.textContent =
        "Running NexusAI Verification...";


    verifyCameraBtn.disabled = true;

    verifyCameraBtn.textContent =
        "VERIFYING...";


    /*
       DEMO VERIFICATION

       This currently simulates the verification process.

       IMPORTANT:
       The real version will send the camera information
       securely to the NexusAI backend, which will test:

       1. Network connectivity
       2. Camera availability
       3. Authentication
       4. NexusAI connection
       5. Camera readiness

       Camera credentials must NOT be sent directly from
       this public frontend to third-party services.
    */


    await verificationStep(
        networkCheck,
        "CHECKING...",
        "CONNECTED"
    );

    await verificationStep(
        cameraCheck,
        "CHECKING...",
        "DETECTED"
    );

    await verificationStep(
        credentialsCheck,
        "CHECKING...",
        "ACCEPTED"
    );

    await verificationStep(
        nexusCheck,
        "CONNECTING...",
        "CONNECTED"
    );


    verificationTitle.textContent =
        "Camera verification successful";


    verificationPassed = true;


    verificationSuccess.classList.remove(
        "hidden"
    );


    verifyCameraBtn.disabled = false;

    verifyCameraBtn.textContent =
        "TEST CAMERA";
}


function verificationStep(
    element,
    processingText,
    successText
) {

    return new Promise((resolve) => {

        element.textContent =
            processingText;

        setTimeout(() => {

            element.textContent =
                `✓ ${successText}`;

            resolve();

        }, 650);

    });

}


/* =========================================================
   ACTIVATE VERIFIED CAMERA
   ========================================================= */

activateCameraBtn.addEventListener(
    "click",
    () => {

        if (!verificationPassed) {
            return;
        }


        const camera = {

            id: Date.now(),

            name:
                cameraName.value.trim(),

            ip:
                cameraIp.value.trim(),

            username:
                cameraUsername.value.trim(),

            /*
               SECURITY NOTE:
               Password is deliberately NOT stored
               in localStorage.

               The real backend will securely store
               credentials server-side.
            */

            location:
                cameraLocation.value.trim(),

            status: "ONLINE",

            protected: true,

            activatedAt:
                new Date().toISOString()

        };


        cameras.push(camera);

        saveLocalData();

        updateDashboard();

        closeCameraSetup();

        alert(
            `${camera.name} has been activated and is now protected by NexusAI.`
        );

    }
);


/* =========================================================
   RETRY VERIFICATION
   ========================================================= */

retryVerificationBtn.addEventListener(
    "click",
    () => {

        verificationError.classList.add(
            "hidden"
        );

        verificationSuccess.classList.add(
            "hidden"
        );

        verificationPassed = false;

    }
);


/* =========================================================
   DASHBOARD UPDATE
   ========================================================= */

function updateDashboard() {

    activeCameras.textContent =
        `${cameras.length} / ${cameras.length}`;

    eventCount.textContent =
        securityEvents.length;

    renderCameras();

    renderEvents();

}


/* =========================================================
   RENDER CAMERAS
   ========================================================= */

function renderCameras() {

    cameraList
        .querySelectorAll(".camera-card")
        .forEach((card) => card.remove());


    if (cameras.length === 0) {

        emptyCameras.classList.remove(
            "hidden"
        );

        return;
    }


    emptyCameras.classList.add(
        "hidden"
    );


    cameras.forEach((camera) => {

        const card =
            document.createElement("div");

        card.className =
            "camera-card";


        card.innerHTML = `

            <div class="camera-card-top">

                <div class="camera-icon">
                    📹
                </div>

                <span class="online-badge">
                    ● ${camera.status}
                </span>

            </div>

            <h3>
                ${escapeHtml(camera.name)}
            </h3>

            <p class="camera-location">
                ${escapeHtml(camera.location)}
            </p>

            <div class="protected">
                🛡️ NEXUSAI PROTECTED
            </div>

        `;


        cameraList.appendChild(card);

    });

}


/* =========================================================
   RENDER SECURITY EVENTS
   ========================================================= */

function renderEvents() {

    if (securityEvents.length === 0) {

        eventList.innerHTML = `
            <div class="empty-events">
                No security events recorded yet.
            </div>
        `;

        return;
    }


    eventList.innerHTML = "";


    securityEvents
        .slice()
        .reverse()
        .forEach((event) => {

            const row =
                document.createElement("div");

            row.className =
                "event-row";


            row.innerHTML = `

                <div class="event-icon">
                    🚨
                </div>

                <div>

                    <div class="event-title">
                        ${escapeHtml(event.type)}
                    </div>

                    <div class="event-meta">
                        ${escapeHtml(event.camera)}
                        •
                        ${escapeHtml(event.location)}
                    </div>

                </div>

                <div class="event-time">
                    ${escapeHtml(event.time)}
                </div>

            `;


            eventList.appendChild(row);

        });

}


/* =========================================================
   LOCAL STORAGE
   ========================================================= */

function saveLocalData() {

    /*
       Demo only.

       Do NOT use localStorage for real client
       credentials or production security data.
    */

    localStorage.setItem(
        "nexusai_cameras",
        JSON.stringify(cameras)
    );

    localStorage.setItem(
        "nexusai_events",
        JSON.stringify(securityEvents)
    );

}


function loadLocalData() {

    try {

        cameras =
            JSON.parse(
                localStorage.getItem(
                    "nexusai_cameras"
                )
            ) || [];


        securityEvents =
            JSON.parse(
                localStorage.getItem(
                    "nexusai_events"
                )
            ) || [];

    } catch (error) {

        console.error(
            "Failed to load NexusAI data:",
            error
        );

        cameras = [];
        securityEvents = [];

    }

}


/* =========================================================
   SECURITY HELPER
   ========================================================= */

function escapeHtml(value) {

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");

}


/* =========================================================
   MODAL BACKDROP
   ========================================================= */

window.addEventListener(
    "click",
    (event) => {

        if (
            event.target === activationModal
        ) {
            closeActivationModal();
        }

        if (
            event.target === cameraModal
        ) {
            closeCameraSetup();
        }

    }
);
