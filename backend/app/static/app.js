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
   NEW — Guided GDD Mode (Server-driven)
-------------------------------------------------- */
let gddWizardActive = false;
let gddSessionId = null;
let currentGDDMarkdown = "";

/* REMOVE old local arrays:
   let gddMode = false;
   let gddQuestions = [...]
   let gddAnswers = {};
   let gddIndex = 0;
*/

/* --------------------------------------------------
   NEW — GDD Wizard API Calls
-------------------------------------------------- */

function sendBot(text) {
    appendMessage(text, "ai");
}

async function startGDDWizard() {
    gddWizardActive = true;

    const res = await fetch("/gdd/start", { method: "POST" });
    const data = await res.json();

    gddSessionId = data.session_id;

    sendBot("🎮 **GDD Wizard Activated!**");
    sendBot(`${data.question}\n(${data.index + 1} / ${data.total})`);
}

async function answerGDD(userText) {
    const res = await fetch("/gdd/answer", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            session_id: gddSessionId,
            answer: userText
        })
    });

    const data = await res.json();

    if (data.status === "done") {
        sendBot("🎉 All questions collected! Type **finish gdd** to generate the full GDD.");
        return;
    }

    sendBot(`${data.question}\n(${data.index + 1} / ${data.total})`);
}

async function finishGDD() {
    sendBot("🧠 Generating your Game Design Document…");

    const res = await fetch("/gdd/finish", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ session_id: gddSessionId })
    });

    const data = await res.json();

    if (data.status !== "ok") {
        sendBot("❌ Error generating GDD.");
        return;
    }

    currentGDDMarkdown = data.markdown;

    sendBot("📘 **Your GDD is ready!**");
    sendBot(data.markdown);

    gddWizardActive = false;
    gddSessionId = null;
}


/* --------------------------------------------------
   stopAllPlayback()
-------------------------------------------------- */
function stopAllPlayback() {
    try {
        if (ttsAudioContext && typeof ttsAudioContext.close === "function") {
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
   playPcmChunk()
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

    // Add GDD hint only when wizard is inactive
    if (role === "ai" && !gddWizardActive) {
        const tip = document.createElement("div");
        tip.className = "ai-tip";
        tip.textContent = "💡 Tip: Say or type “Activate GDD Wizard” to begin building a full Game Design Document.";
        div.appendChild(tip);
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
            const txt = d.text?.trim();
            if (!txt) {
                finalizeAI();
                return;
            }

            // --------------------------------------------
            // 🔥 Voice-triggered GDD Wizard activation
            // --------------------------------------------
            if (!gddWizardActive && txt.toLowerCase().includes("activate gdd wizard")) {
                appendMessage(txt, "user");   // show transcript
                startGDDWizard();             // launch wizard
                currentSessionIsVoice = false;
                finalizeAI();
                return;
            }

            // --------------------------------------------
            // 🔥 SHIELD #3 — Prevent duplicate text in text-mode
            // --------------------------------------------
            if (!micActive && !currentSessionIsVoice) {
                // text mode → ignore transcript completely
                finalizeAI();
                return;
            }

            // --------------------------------------------
            // Voice mode → show transcript normally
            // --------------------------------------------
            appendMessage(txt, "user");

            currentSessionIsVoice = false;
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
            return;
        }
    };

    return wsReady;
}

/* --------------------------------------------------
   Microphone streaming (untouched)
-------------------------------------------------- */
async function startMicStreaming() {
    if (micActive) return;
    micActive = true;
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
    document.getElementById("btnStartMic").classList.remove("active");

    try { workletNode?.disconnect(); } catch {}
    try { audioContext?.close(); } catch {}

    workletNode = null;
    audioContext = null;

    if (closeWs && ws?.readyState === WebSocket.OPEN) ws.close();
}

document.getElementById("btnStartMic").onclick = async () => {
    try {
        await connectWS();
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
        return;
    }

    if (!micActive) {
        startMicStreaming();
    } else {
        stopMic(false);
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

    stopAllPlayback();
};

/* --------------------------------------------------
   SEND TEXT — UPDATED GDD LOGIC
-------------------------------------------------- */
async function sendText() {
    const msg = textInput.value.trim();
    if (!msg) return;
    textInput.value = "";

    // 🔥 Force text-mode state reset (fixes duplicate messages)
    currentSessionIsVoice = false;
    micActive = false;

    // 🔥 Disable mic button until user clicks again
    const micBtn = document.getElementById("btnStartMic");
    const stopMicBtn = document.getElementById("btnStopMic");

    micBtn.classList.remove("active");  // remove glow
    micBtn.disabled = false;            // re-enable press
    stopMicBtn.disabled = true;         // can't stop something not running

    try { workletNode?.disconnect(); } catch {}
    try { audioContext?.close(); } catch {}
    workletNode = null;
    audioContext = null;

    const lower = msg.toLowerCase();

    // 1) Already inside guided wizard
    if (gddWizardActive) {
        appendMessage(msg, "user");
        await answerGDD(msg);
        return;
    }

    // 2) Start wizard
    const triggers = [
    "create gdd",
    "start gdd",
    "gdd wizard",
    "design document",
    "activate gdd wizard"
    ];
    if (triggers.some(t => lower.includes(t))) {
        appendMessage(msg, "user");
        startGDDWizard();
        return;
    }

    // 3) Finish wizard (generate output)
    if (lower === "finish gdd" || lower === "generate gdd") {
        appendMessage(msg, "user");
        await finishGDD();
        return;
    }

    // 4) Normal chat mode
    appendMessage(msg, "user");

    try {
        await connectWS();
        currentSessionIsVoice = false;
        stopAllPlayback();
        finalizeAI();

        ws.send(JSON.stringify({ type: "text", text: msg }));
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
    }
}


/* --------------------------------------------------
   Buttons & Keybinds (unchanged)
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

/* --------------------------------------------------
   Sidebar + Workspace Toggles (unchanged)
-------------------------------------------------- */
const sidebar = document.getElementById("sidebar");
const workspace = document.getElementById("workspace");

document.getElementById("toggleLeft").onclick = () => {
    sidebar.classList.toggle("collapsed");
    toggleLeft.textContent = sidebar.classList.contains("collapsed") ? "➡️" : "⬅️";
};

document.getElementById("toggleRight").onclick = () => {
    workspace.classList.toggle("collapsed");
    toggleRight.textContent = workspace.classList.contains("collapsed") ? "⬅️" : "➡️";
};

/* --------------------------------------------------
   RAG Upload + Embedding (untouched)
-------------------------------------------------- */
const uploadBtn = document.getElementById("btnUploadEmbed");
const fileInput = document.getElementById("ragFileInput");
const statusBox = document.getElementById("uploadStatus");
const kbList = document.getElementById("kbList");

// Chat upload
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

    let res = await fetch("/rag/upload", { method: "POST", body: form });
    if (!res.ok) {
        statusBox.textContent = "Upload failed ❌";
        return;
    }

    statusBox.textContent = "Embedding…";

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

        let label = document.createElement("span");
        label.textContent = f;

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

/* --------------------------------------------------
   DOCX Export
-------------------------------------------------- */
async function downloadDocx(markdown) {
    const res = await fetch("/gdd/export-docx", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ markdown })
    });

    if (!res.ok) {
        alert("DOCX export failed");
        return;
    }

    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = "GDD.docx";
    a.click();
    window.URL.revokeObjectURL(url);
}

document.getElementById("btnExportDocx").onclick = () => {
    downloadDocx(currentGDDMarkdown);
};

window.addEventListener("DOMContentLoaded", () => {
    refreshKBList();
});
