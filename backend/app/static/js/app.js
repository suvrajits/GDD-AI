// static/app.js
/* --------------------------------------------------
   State
-------------------------------------------------- */
let ws = null;
let wsReady = null;

let audioContext = null;
let workletNode = null;
let micActive = false;

let ttsAudioContext = null;

let currentAiDiv = null;
let currentSessionIsVoice = false;   // voice-mode flag

/* --------------------------------------------------
   stopAllPlayback() — stops audio immediately but ensures audio context can be recreated
-------------------------------------------------- */
function stopAllPlayback() {
    try {
        if (ttsAudioContext && typeof ttsAudioContext.close === "function") {
            // close stops playback; we set to null so next play recreates new context
            ttsAudioContext.close();
        }
    } catch (e) {
        console.warn("stopAllPlayback error", e);
    } finally {
        ttsAudioContext = null;
    }

    if (currentAiDiv) {
        currentAiDiv.classList.remove("streaming");
        currentAiDiv = null;
    }
}

/* --------------------------------------------------
   playPcmChunk() — raw PCM16 playback with recreated AudioContext if needed
-------------------------------------------------- */
function playPcmChunk(buffer) {
    if (!ttsAudioContext) {
        try {
            ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
        } catch (e) {
            console.error("Could not create AudioContext:", e);
            return;
        }
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
    try {
        src.start();
    } catch (e) {
        console.warn("Error starting audio source:", e);
    }
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

        if (d.type === "final") {
            if (d.text && d.text.trim()) {
                appendMessage(d.text, "user");
            }
            finalizeAI();
            return;
        }

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

        if (d.type === "voice_done") {
            currentSessionIsVoice = false;
            finalizeAI();
            return;
        }

        if (d.type === "stop_all") {
            stopAllPlayback();
            currentSessionIsVoice = false;
            finalizeAI();
            return;
        }

        if (d.type === "partial") {
            // ignore partial transcripts in UI to avoid noise
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

    // 🔥 NEW — visual state ON
    document.getElementById("btnStartMic").classList.add("active");

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

    // 🔥 NEW — visual state OFF
    document.getElementById("btnStartMic").classList.remove("active");

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

    if (!micActive) {
        startMicStreaming();   // mic turns ON
    } else {
        stopMic(false);        // mic turns OFF, websocket remains open
    }
};


document.getElementById("btnStopMic").onclick = async () => {
    try {
        await connectWS();
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "stop_llm" }));
        }
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
    }

    // stop playback locally too
    stopAllPlayback();
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

    // ensure text-mode (no voice)
    currentSessionIsVoice = false;
    stopAllPlayback();
    finalizeAI();

    try {
        ws.send(JSON.stringify({ type: "text", text: msg }));
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
    }
}

/* --------------------------------------------------
   Sidebar + Workspace Slide Toggles
-------------------------------------------------- */

const sidebar = document.getElementById("sidebar");
const workspace = document.getElementById("workspace");

const toggleLeft = document.getElementById("toggleLeft");
const toggleRight = document.getElementById("toggleRight");

toggleLeft.onclick = () => {
    sidebar.classList.toggle("collapsed");
    toggleLeft.textContent = sidebar.classList.contains("collapsed") ? "➡️" : "⬅️";
};

toggleRight.onclick = () => {
    workspace.classList.toggle("collapsed");
    toggleRight.textContent = workspace.classList.contains("collapsed") ? "⬅️" : "➡️";
};

/* --------------------------------------------------
   RAG Upload (Workspace) — Single-button Upload+Embed
-------------------------------------------------- */
/* --------------------------------------------------
   RAG Upload (Workspace) — Single-button Upload+Embed
-------------------------------------------------- */
const uploadBtn = document.getElementById("btnUploadEmbed");
const fileInput = document.getElementById("ragFileInput");
const statusBox = document.getElementById("uploadStatus");
const kbList = document.getElementById("kbList");

// Chat-box upload button
const chatUploadBtn = document.getElementById("btnUploadChat");
const chatFileInput = document.getElementById("ragFileInputChat");

chatUploadBtn.onclick = () => chatFileInput.click();
chatFileInput.onchange = () => triggerUpload(chatFileInput.files);
fileInput.onchange = () => triggerUpload(fileInput.files);

uploadBtn.onclick = () => triggerUpload(fileInput.files);


async function triggerUpload(files) {
    if (!files || files.length === 0) return;

    statusBox.textContent = "Uploading…";

    const form = new FormData();
    for (const f of files) form.append("files", f);

    // 1) Upload
    let res = await fetch("/rag/upload", { method: "POST", body: form });   
    if (!res.ok) {
        statusBox.textContent = "Upload failed ❌";
        return;
    }
    let out = await res.json();
    console.log("UPLOAD:", out);

    statusBox.textContent = "Embedding…";

    // 2) Ingest
    let res2 = await fetch("/rag/ingest", { method: "POST" });
    let out2 = await res2.json();
    console.log("INGEST:", out2);

    statusBox.textContent = "Embedded successfully ✔️";

    refreshKBList();
}

async function refreshKBList() {
    let res = await fetch("/rag/embedded-files");
    let j = await res.json();
    kbList.innerHTML = "";

    j.files.forEach(f => {
        let div = document.createElement("div");
        div.className = "kb-item";

        // File name label
        let label = document.createElement("span");
        label.textContent = f;

        // Remove button (❌)
        let rm = document.createElement("span");
        rm.textContent = "❌";
        rm.className = "kb-remove";
        rm.onclick = async () => {
            await fetch(`/rag/file/${encodeURIComponent(f)}`, { method: "DELETE" });
            refreshKBList();
        };

        div.appendChild(label);
        div.appendChild(rm);
        kbList.appendChild(div);
    });
}


window.addEventListener("DOMContentLoaded", () => {
    refreshKBList();  
});

