// static/app.js

/* --------------------------------------------------
   State
-------------------------------------------------- */
let ws = null;
let audioContext = null;
let workletNode = null;

let currentAiDiv = null;
let micActive = false;

/* --------------------------------------------------
   Chat UI helpers
-------------------------------------------------- */
function appendMessage(text, role, opts = {}) {
    const div = document.createElement("div");
    div.className = "msg " + role + (opts.streaming ? " streaming" : "");

    if (role === "ai") {
        const wrap = document.createElement("div");
        wrap.className = "content";
        wrap.textContent = text;
        div.appendChild(wrap);
    } else {
        div.textContent = text;
    }

    document.getElementById("messages").appendChild(div);
    messages.scrollTop = messages.scrollHeight;
    return div;
}

function appendToAI(text) {
    if (!currentAiDiv) {
        currentAiDiv = appendMessage("", "ai", { streaming: true });
    }
    currentAiDiv.querySelector(".content").textContent += text;

    messages.scrollTop = messages.scrollHeight;
}

function finalizeAI() {
    if (currentAiDiv) {
        currentAiDiv.classList.remove("streaming");
        currentAiDiv = null;
    }
}

/* --------------------------------------------------
   WebSocket
-------------------------------------------------- */
function connectWS() {
    if (ws && ws.readyState === WebSocket.OPEN) return;

    ws = new WebSocket("ws://localhost:8000/ws/stream");
    ws.binaryType = "arraybuffer";

    ws.onmessage = (msg) => {
        const d = JSON.parse(msg.data);

        if (d.type === "final") {
            appendMessage(d.text, "user");
            finalizeAI();
        }

        if (d.type === "llm_stream") {
            // auto-stop mic so it doesn't record during LLM response
            if (micActive) stopMic(false);

            appendToAI(d.token);
        }

        if (d.type === "llm_done") {
            finalizeAI();
        }
    };

    ws.onopen = () => {
        console.log("WS connected");
        // begin STT stream
        startMicStreaming();
    };

    ws.onclose = () => stopMic(false);
}

/* --------------------------------------------------
   Microphone
-------------------------------------------------- */
async function startMicStreaming() {
    if (micActive) return;
    micActive = true;

    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioContext = new AudioContext({ sampleRate: 16000 });

    await audioContext.audioWorklet.addModule("/static/pcm-worklet.js");

    const source = audioContext.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(audioContext, "pcm-processor");

    workletNode.port.onmessage = (event) => {
        if (ws?.readyState === WebSocket.OPEN) ws.send(event.data);
    };

    source.connect(workletNode);
}

function stopMic(closeWs = true) {
    micActive = false;

    try { workletNode?.disconnect(); } catch {}
    try { audioContext?.close(); } catch {}

    workletNode = null;
    audioContext = null;

    if (closeWs && ws?.readyState === WebSocket.OPEN) ws.close();
}

/* --------------------------------------------------
   UI buttons
-------------------------------------------------- */
document.getElementById("btnStartMic").onclick = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) connectWS();
    else if (!micActive) startMicStreaming();
    else stopMic(false);
};

document.getElementById("btnStopMic").onclick = () => stopMic(true);

/* --------------------------------------------------
   Text chat
-------------------------------------------------- */
const textInput = document.getElementById("textInput");
const btnSend = document.getElementById("btnSend");

btnSend.onclick = () => sendText();
textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendText();
    }
});

function sendText() {
    const msg = textInput.value.trim();
    if (!msg) return;

    // DO NOT append locally — wait for server "final"
    textInput.value = "";

    if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: "text",
            text: msg
        }));
    } else {
        appendMessage("[offline] WebSocket not connected", "ai");
    }
}

