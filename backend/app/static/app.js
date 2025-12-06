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
   Guided GDD Mode (Server-driven)
-------------------------------------------------- */
let gddWizardActive = false;
let gddSessionId = null;
let currentGDDMarkdown = "";

let gddIndex = 0;
let gddTotal = 14; 

/* --------------------------------------------------
   Wizard Utility
-------------------------------------------------- */
function sendBot(text) {
    appendMessage(text, "ai");
}

/* --------------------------------------------------
   Wizard API — start / answer / next / finish
-------------------------------------------------- */
async function startGDDWizard() {
    gddWizardActive = true;

    const res = await fetch("/gdd/start", { method: "POST" });
    const data = await res.json();

    gddSessionId = data.session_id;

    sendBot("🎮 **GDD Wizard Activated!**\nSay **Go Next** anytime to proceed.");
}

async function answerGDD(userText) {
    const res = await fetch("/gdd/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            session_id: gddSessionId,
            answer: userText
        })
    });

    const data = await res.json();

    if (data.status === "done") {
        sendBot("🎉 All questions collected! Say or type **finish gdd** to generate the document.");
        return;
    }

    sendBot(`${data.question}\n(${data.index + 1} / ${data.total})`);
}

/* ⭐ PATCH: new /gdd/next request */
async function nextGDD() {
    const res = await fetch("/gdd/next", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: gddSessionId })
    });

    const data = await res.json();

    if (data.status === "done") {
        sendBot("🎉 All questions answered! Say or type **finish gdd** to generate the full GDD.");
        return;
    }

    sendBot(`${data.question}\n(${data.index + 1} / ${data.total})`);
}

async function finishGDD() {
    sendBot("🧠 Generating your Game Design Document…");

    const res = await fetch("/gdd/finish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: gddSessionId })
    });

    const data = await res.json();
    if (data.session_id) gddSessionId = data.session_id;
    if (data.status !== "ok") {
        sendBot("❌ Error generating GDD.");
        return;
    }

    currentGDDMarkdown = data.markdown;
    gddSessionId = data.session_id;   // restore session id
    sendBot("📘 **Your GDD is ready!**");
    sendBot(data.markdown);

    if (data.export_available) {
        sendBot("⬇️ **Click the Export to Word button to download your GDD.**");
    }

    gddWizardActive = false;

    // ⭐ OPTION B — DO NOT reset gddSessionId
    // gddSessionId remains valid for export.
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
   PCM Playback
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

    try { src.start(); }
    catch (e) { console.warn("Error starting audio source:", e); }
}

/* --------------------------------------------------
   UI Helpers
-------------------------------------------------- */
function appendMessage(text, role, opts = {}) {
    const div = document.createElement("div");
    div.className = "msg " + role + (opts.streaming ? " streaming" : "");

    if (role === "ai") {
        const wrap = document.createElement("div");
        wrap.className = "content";
        wrap.textContent = text;
        div.appendChild(wrap);

        const container = document.getElementById("messages");
        container.appendChild(div);

        // ⭐ FIX: Remove old tooltip before adding a new one
        const oldTips = document.querySelectorAll(".ai-tip");
        oldTips.forEach(t => t.remove());

        const tip = document.createElement("div");
        tip.className = "ai-tip";

        if (gddWizardActive) {
            tip.textContent =
                "💡 Say “Go Next” for the next question — Say “Finish GDD” to complete.";
        } else {
            tip.textContent =
                "💡 Tip: Say “Activate GDD Wizard” to start creating a full Game Design Document.";
        }

        container.appendChild(tip);
        messages.scrollTop = messages.scrollHeight;
        return div;
    }

    // USER MESSAGE
    div.textContent = text;
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
   WebSocket Logic
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
            document.getElementById("btnStartMic").disabled = false;
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
        document.getElementById("btnStartMic").disabled = false;
    };

    ws.onmessage = async (msg) => {

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

        /* --------------------------------------------------
           WIZARD EVENTS
        -------------------------------------------------- */
        if (d.type === "wizard_notice") {

            // Do NOT re-trigger wizard after final GDD ready message
            if (d.text.includes("Your GDD is ready")) {
                sendBot(d.text);
                gddWizardActive = false;
                return;
            }

            gddWizardActive = true;
            sendBot(d.text);
            return;
        }

        if (d.type === "wizard_question") {
            if (!window.gddIndexInitialized) {
                window.gddIndex = 0;
                window.gddTotal = 14;
                window.gddIndexInitialized = true;
            } else {
                window.gddIndex++;
            }

            sendBot(`${d.text}\n(${window.gddIndex + 1} / ${window.gddTotal})`);
            return;
        }

        if (d.type === "wizard_answer") {
            appendMessage(d.text, "user");
            return;
        }

        // Backend sends session ID for exporting
        if (d.type === "gdd_session_id") {
            console.log("🔗 Linked GDD session:", d.session_id);
            gddSessionId = d.session_id;
            return;
        }

        if (d.type === "gdd_export_ready") {
            console.log("📥 Export GDD triggered by voice");
            downloadGDD();
            return;
        }

        /* --------------------------------------------------
           FINAL STT TRANSCRIPT
        -------------------------------------------------- */
        if (d.type === "final") {
            const txt = d.text?.trim();
            if (!txt) { finalizeAI(); return; }

            const lower = txt.toLowerCase();  // ⭐ declared ONCE

            // ⭐ ALWAYS intercept export voice commands FIRST
            if (
                lower.includes("download gdd") ||
                lower.includes("export gdd") ||
                lower.includes("export document") ||
                lower.includes("export the gdd")
            ) {
                if (!currentGDDMarkdown || currentGDDMarkdown.trim().length < 30) {
                    sendBot("❌ The GDD is not ready yet. Say **Finish GDD** first.");
                    finalizeAI();
                    return;
                }

                console.log("📥 Voice command → Export GDD");
                downloadGDD();
                finalizeAI();
                return;
            }

            // Wizard handling
            if (gddWizardActive) {
                if (txt.startsWith("📘")) {
                    sendBot(txt);
                    return;
                }
                return;
            }

            // Activate wizard
            if (!gddWizardActive && lower.includes("activate gdd wizard")) {
                appendMessage(txt, "user");
                currentSessionIsVoice = false;
                finalizeAI();
                return;
            }

            // Secondary export pattern match
            if (
                lower.includes("export gdd") ||
                lower.includes("download gdd") ||
                lower.includes("export document") ||
                lower.includes("export g d d") ||
                lower.includes("export the gdd")
            ) {
                console.log("📥 Voice command → Export GDD");

                if (!currentGDDMarkdown || currentGDDMarkdown.trim().length < 30) {
                    sendBot("❌ The GDD is not ready yet. Say **Finish GDD** first.");
                    finalizeAI();
                    return;
                }

                downloadGDD();
                finalizeAI();
                return;
            }

            // Normal voice message
            appendMessage(txt, "user");
            currentSessionIsVoice = false;
            finalizeAI();
            return;
        }

        /* --------------------------------------------------
           LLM STREAMING
        -------------------------------------------------- */
        if (d.type === "llm_stream") {
            if (gddWizardActive) return;
            if (!currentSessionIsVoice && d.token) appendToAI(d.token);
            return;
        }

        if (d.type === "llm_done") {
            if (gddWizardActive) return;
            if (!currentSessionIsVoice) finalizeAI();
            return;
        }

        /* --------------------------------------------------
           SENTENCE START
        -------------------------------------------------- */
        if (d.type === "sentence_start") {
            if (gddWizardActive) return;
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
    };

    return wsReady;
}


/* --------------------------------------------------
   Microphone (unchanged)
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

    document.getElementById("btnStartMic").disabled = false;

    try { workletNode?.disconnect(); } catch {}
    try { audioContext?.close(); } catch {}

    workletNode = null;
    audioContext = null;

    if (closeWs && ws?.readyState === WebSocket.OPEN) ws.close();
}

document.getElementById("btnStartMic").onclick = async () => {
    try { await connectWS(); }
    catch (e) { appendMessage("[offline] WebSocket not connected", "ai"); return; }

    if (!micActive) startMicStreaming();
    else stopMic(false);
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
   SEND TEXT — Includes Wizard Patches
-------------------------------------------------- */
async function sendText() {
    const msg = textInput.value.trim();
    if (!msg) return;
    textInput.value = "";

    const lower = msg.toLowerCase();

    // Wizard answer
    if (gddWizardActive && !lower.includes("go next")) {
        appendMessage(msg, "user");
        await answerGDD(msg);
        return;
    }

    // Text "Go Next"
    if (gddWizardActive && lower.includes("go next")) {
        appendMessage(msg, "user");
        await nextGDD();
        return;
    }

    // Start wizard
    const triggers = ["create gdd", "start gdd", "gdd wizard", "design document", "activate gdd wizard"];
    if (triggers.some(t => lower.includes(t))) {
        appendMessage(msg, "user");
        startGDDWizard();
        return;
    }

    // Finish wizard
    if (lower === "finish gdd" || lower === "generate gdd") {
        appendMessage(msg, "user");
        await finishGDD();
        return;
    }

    // Export (text)
    const exportTriggers = [
        "export gdd",
        "download gdd",
        "export document",
        "export doc",
        "download document",
        "download gdd doc",
        "export"
    ];

    if (exportTriggers.some(t => lower.includes(t))) {
        appendMessage(msg, "user");
        downloadGDD();
        return;
    }

    // Normal chat
    appendMessage(msg, "user");

    try {
        await connectWS();
        currentSessionIsVoice = false;
        if (micActive) stopMic(false);
        stopAllPlayback();
        finalizeAI();
        ws.send(JSON.stringify({ type: "text", text: msg }));
    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
    }
}

/* --------------------------------------------------
   Buttons / Keybinds
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
   Sidebar + Workspace Toggles
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
   RAG Upload
-------------------------------------------------- */
const uploadBtn = document.getElementById("btnUploadEmbed");
const fileInput = document.getElementById("ragFileInput");
const statusBox = document.getElementById("uploadStatus");
const kbList = document.getElementById("kbList");

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
    if (!res.ok) { statusBox.textContent = "Upload failed ❌"; return; }

    statusBox.textContent = "Embedding…";

    let res2 = await fetch("/rag/ingest", { method: "POST" });
    await res2.json();

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

    if (!res.ok) { alert("DOCX export failed"); return; }

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

/* --------------------------------------------------
   GDD Export API
-------------------------------------------------- */
async function downloadGDD() {
    if (!gddSessionId) {
        sendBot("❌ No active GDD session to export.");
        return;
    }

    const res = await fetch("/gdd/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: gddSessionId })
    });

    if (!res.ok) {
        sendBot("❌ Export failed.");
        return;
    }

    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = `GDD_${gddSessionId}.docx`;
    a.click();

    window.URL.revokeObjectURL(url);
}
