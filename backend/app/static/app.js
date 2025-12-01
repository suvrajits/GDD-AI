// /static/app.js (REPLACE your current file with this)

/* ---------- State ---------- */
let ws = null;
let audioContext = null;
let workletNode = null;

let currentAiDiv = null;   // AI streaming block
let micActive = false;

/* ---------- Message helpers ---------- */

function appendMessage(text, role, opts = {}) {
    const div = document.createElement("div");
    div.className = "msg " + role + (opts.streaming ? " streaming" : "");
    
    if (role === "ai") {
        const content = document.createElement("div");
        content.className = "content";
        content.textContent = text || "";
        div.appendChild(content);
    } else {
        div.textContent = text;
    }

    const messages = document.getElementById("messages");
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;

    return div;
}

function appendToAI(text) {
    // ensure we always access the messages container safely
    const messages = document.getElementById("messages");

    if (!currentAiDiv) {
        currentAiDiv = appendMessage("", "ai", { streaming: true });
    }

    const contentEl = currentAiDiv.querySelector(".content");
    if (contentEl) {
        contentEl.textContent += text;
    } else {
        // fallback: append as plain text
        currentAiDiv.textContent += text;
    }

    // scroll safely
    messages.scrollTop = messages.scrollHeight;
}

function finalizeAI() {
    if (currentAiDiv) {
        currentAiDiv.classList.remove("streaming");
        currentAiDiv = null;
    }
}

/* ---------- WebSocket Handling ---------- */

function connectWS() {
    // prevent duplicate WS
    if (ws && ws.readyState === WebSocket.OPEN) {
        console.log("WS already open");
        return;
    }

    ws = new WebSocket("ws://localhost:8000/ws/stream");
    ws.binaryType = "arraybuffer";

    ws.onmessage = (msg) => {
        try {
            const d = JSON.parse(msg.data);

            if (d.type === "partial") {
                // optional: show partial STT somewhere
            }

            if (d.type === "final") {
                appendMessage(d.text, "user");
                finalizeAI();
            }

            if (d.type === "llm_stream") {
                appendToAI(d.token);
            }

            if (d.type === "llm_done") {
                finalizeAI();
            }

            if (d.type === "error") {
                appendMessage(`[ERROR] ${d.msg}`, "ai");
                finalizeAI();
            }
        } catch (e) {
            console.error("Failed to handle ws message:", e, msg.data);
        }
    };

    ws.onopen = () => {
        console.log("WS connected");
        // start mic streaming once websocket is ready
        startMicStreaming().catch(err => {
            console.error("startMicStreaming error:", err);
        });
    };

    ws.onclose = (ev) => {
        console.log("WS closed", ev);
        // cleanup audio if still active
        stopMic(false); // don't close WS again
    };

    ws.onerror = (err) => {
        console.error("WS error:", err);
    };
}

/* ---------- Microphone + PCM Worklet ---------- */

async function startMicStreaming() {
    // prevent starting twice
    if (micActive) return;
    micActive = true;

    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

        audioContext = new AudioContext({ sampleRate: 16000 });
        await audioContext.audioWorklet.addModule("/static/pcm-worklet.js");

        const source = audioContext.createMediaStreamSource(stream);
        workletNode = new AudioWorkletNode(audioContext, "pcm-processor");

        workletNode.port.onmessage = (event) => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                try {
                    ws.send(event.data);
                } catch (e) {
                    console.error("Failed to send PCM frame:", e);
                }
            }
        };

        source.connect(workletNode);

        console.log("Mic streaming started");
    } catch (e) {
        micActive = false;
        console.error("Error accessing microphone / starting worklet:", e);
        appendMessage("[Microphone error] " + e.message, "ai");
    }
}

function stopMic(closeWs = true) {
    // stop audio nodes
    micActive = false;
    try { if (workletNode) workletNode.disconnect(); } catch (e) {}
    try { if (audioContext) audioContext.close(); } catch (e) {}

    workletNode = null;
    audioContext = null;

    // optionally close WS; default true for stop button, false when ws.onclose triggers
    if (closeWs && ws && ws.readyState === WebSocket.OPEN) {
        try { ws.close(); } catch (e) {}
    }
}

/* ---------- UI Buttons ---------- */

document.getElementById("btnStartMic").onclick = () => {
    // toggle mic + ws
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        connectWS();
    } else if (!micActive) {
        startMicStreaming();
    } else {
        // already active — pause mic
        stopMic(false);
    }
};

document.getElementById("btnStopMic").onclick = () => {
    stopMic(true);
};

/* ---------- Text Sending ---------- */

const textInput = document.getElementById("textInput");
const btnSend = document.getElementById("btnSend");

btnSend.onclick = () => {
    sendTextMessage();
};

textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendTextMessage();
    }
});

function sendTextMessage() {
    const text = textInput.value.trim();
    if (!text) return;

    appendMessage(text, "user");
    textInput.value = "";

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: "text",
            text: text
        }));
    } else {
        appendMessage("[offline] Cannot send — WS not connected", "ai");
    }
}
