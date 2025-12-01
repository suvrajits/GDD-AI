// static/app.js

/* --------------------------------------------------
   State
-------------------------------------------------- */
let ws = null;
let audioContext = null;
let workletNode = null;

let currentAiDiv = null;
let micActive = false;

let ttsAudioContext = null;

/* --------------------------------------------------
   TTS PCM Playback
-------------------------------------------------- */
function playPcmChunk(buffer) {
    // Initialize audio context if not already
    if (!ttsAudioContext) {
        ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
    }

    const pcm16 = new Int16Array(buffer);
    const float32 = new Float32Array(pcm16.length);

    for (let i = 0; i < pcm16.length; i++) {
        float32[i] = pcm16[i] / 32768; // normalize to [-1, 1]
    }

    const audioBuffer = ttsAudioContext.createBuffer(
        1,                 // mono
        float32.length,
        16000              // Azure PCM sample rate
    );

    audioBuffer.getChannelData(0).set(float32);

    const source = ttsAudioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(ttsAudioContext.destination);

    source.start();
}


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

    ws.onopen = () => {
        console.log("WS connected");
        startMicStreaming();
    };

    ws.onclose = () => stopMic(false);

    ws.onmessage = (msg) => {
        // ----------------------------------------------------
        // 1) Handle binary audio FIRST (Azure TTS PCM)
        // ----------------------------------------------------
        if (msg.data instanceof ArrayBuffer) {
            playPcmChunk(msg.data);
            return;
        }

        // ----------------------------------------------------
        // 2) Then handle JSON text messages
        // ----------------------------------------------------
        let d = null;
        try {
            d = JSON.parse(msg.data);
        } catch (err) {
            console.error("JSON parse error:", err, msg.data);
            return;
        }

        if (d.type === "final") {
            appendMessage(d.text, "user");
            finalizeAI();
        }

        if (d.type === "llm_stream") {
            // stop Mic while AI is speaking
            if (micActive) stopMic(false);
            appendToAI(d.token);
        }

        if (d.type === "llm_done") {
            finalizeAI();
        }
    };
}

/* --------------------------------------------------
   Microphone streaming
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
