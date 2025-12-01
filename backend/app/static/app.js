// static/app.js
/* --------------------------------------------------
   State
-------------------------------------------------- */
let ws = null;
let wsReady = null;
let audioContext = null;
let workletNode = null;

let currentAiDiv = null;
let micActive = false;

let ttsAudioContext = null;

// voice-mode flag (controlled by server events)
let currentSessionIsVoice = false;

/* --------------------------------------------------
   Stop all audio playback (immediate)
-------------------------------------------------- */
function stopAllPlayback() {
    try {
        if (ttsAudioContext) {
            // close will stop currently playing audio immediately
            ttsAudioContext.close();
        }
    } catch (e) {
        console.warn("stopAllPlayback error", e);
    } finally {
        ttsAudioContext = null;
    }

    // finalize current AI bubble if present
    if (currentAiDiv) {
        currentAiDiv.classList.remove("streaming");
        currentAiDiv = null;
    }
}

/* --------------------------------------------------
   PCM playback (raw PCM16)
-------------------------------------------------- */
function playPcmChunk(buffer) {
    if (!ttsAudioContext) {
        ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
    }

    const pcm16 = new Int16Array(buffer);
    const float32 = new Float32Array(pcm16.length);

    for (let i = 0; i < pcm16.length; i++) {
        float32[i] = pcm16[i] / 32768;
    }

    const audioBuffer = ttsAudioContext.createBuffer(1, float32.length, 16000);
    audioBuffer.getChannelData(0).set(float32);

    const src = ttsAudioContext.createBufferSource();
    src.buffer = audioBuffer;
    src.connect(ttsAudioContext.destination);
    src.start();
}

/* --------------------------------------------------
   UI helpers
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
   WebSocket connect
-------------------------------------------------- */
function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return wsReady || Promise.resolve();
    }

    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/stream");
    ws.binaryType = "arraybuffer";

    wsReady = new Promise((resolve, reject) => {
        const onOpen = () => {
            console.log("WS connected");
            ws.removeEventListener("open", onOpen);
            ws.removeEventListener("error", onError);
            resolve();
        };
        const onError = (e) => {
            console.error("WS error", e);
            ws.removeEventListener("open", onOpen);
            ws.removeEventListener("error", onError);
            reject(e);
        };
        ws.addEventListener("open", onOpen);
        ws.addEventListener("error", onError);
    });

    ws.onclose = () => {
        console.warn("WS closed");
        stopMic(false);
        stopAllPlayback();
        currentSessionIsVoice = false;
        ws = null;
        wsReady = null;
    };

    ws.onmessage = (msg) => {
        if (msg.data instanceof ArrayBuffer) {
            playPcmChunk(msg.data);
            return;
        }

        let d = null;
        try { d = JSON.parse(msg.data); }
        catch (err) {
            console.error("JSON parse error", err, msg.data);
            return;
        }

        // user final (typed or STT final)
        if (d.type === "final") {
            if (d.text && d.text.trim()) {
                appendMessage(d.text, "user");
            }
            finalizeAI();
            return;
        }

        // typed LLM streaming (only show when NOT voice-driven)
        if (d.type === "llm_stream") {
            if (!currentSessionIsVoice && d.token) {
                appendToAI(d.token);
            }
            return;
        }

        if (d.type === "llm_done") {
            if (!currentSessionIsVoice) finalizeAI();
            return;
        }

        // sentence_start -> reveal sentence and mark voice mode
        if (d.type === "sentence_start") {
            const clean = (d.text || "").trim();
            if (!clean) return;
            currentSessionIsVoice = true;
            if (!currentAiDiv) {
                currentAiDiv = appendMessage("", "ai", { streaming: true });
            }
            currentAiDiv.querySelector(".content").textContent += clean + " ";
            messages.scrollTop = messages.scrollHeight;
            return;
        }

        // voice_done -> reset voice-mode and finalize bubble
        if (d.type === "voice_done") {
            currentSessionIsVoice = false;
            finalizeAI();
            return;
        }

        // stop_all -> immediate playback stop (barge-in or manual stop)
        if (d.type === "stop_all") {
            stopAllPlayback();
            currentSessionIsVoice = false;
            return;
        }

        // partial STT - ignore for now
        if (d.type === "partial") {
            return;
        }
    };

    return wsReady;
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

    const src = audioContext.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(audioContext, "pcm-processor");

    workletNode.port.onmessage = (e) => {
        if (ws?.readyState === WebSocket.OPEN) ws.send(e.data);
    };

    src.connect(workletNode);
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
   UI Buttons & Text send
-------------------------------------------------- */
document.getElementById("btnStartMic").onclick = async () => {
    try {
        await connectWS();
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
        return;
    }

    if (!micActive) startMicStreaming();
    else stopMic(false);
};

document.getElementById("btnStopMic").onclick = async () => {
    // manual stop -> tell backend to cancel everything
    try {
        await connectWS();
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "stop_llm" }));
        }
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
    }
};

const textInput = document.getElementById("textInput");
const btnSend = document.getElementById("btnSend");

btnSend.onclick = () => sendText();

textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendText();
    }
});

async function sendText() {
    const msg = textInput.value.trim();
    if (!msg) return;
    textInput.value = "";

    try {
        await connectWS();
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
        return;
    }

    try {
        ws.send(JSON.stringify({ type: "text", text: msg }));
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
    }
}
