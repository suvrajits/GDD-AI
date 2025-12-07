// ws.js — FINAL, FULLY FIXED VERSION
import { 
    appendMessage, 
    appendToAI, 
    finalizeAI, 
    startNewAIBubble 
} from "./ui.js";

import { downloadGDD, setGDDSessionId } from "./gdd.js";

export let ws = null;
export let wsReady = null;

let ttsAudioContext = null;
let aiStreaming = false;
let aiBubble = null;

/* --------------------------------------------------
   AUDIO STOP
-------------------------------------------------- */
export function stopAllPlayback() {
    try {
        if (ttsAudioContext) ttsAudioContext.close();
    } catch (e) { console.warn(e); }
    ttsAudioContext = null;
}

/* --------------------------------------------------
   PLAY PCM AUDIO
-------------------------------------------------- */
export function playPcmChunk(buffer) {
    try {
        if (!ttsAudioContext)
            ttsAudioContext = new AudioContext();

        const pcm = new Int16Array(buffer);
        const f32 = new Float32Array(pcm.length);
        for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;

        const audioBuffer = ttsAudioContext.createBuffer(1, f32.length, 16000);
        audioBuffer.getChannelData(0).set(f32);

        const src = ttsAudioContext.createBufferSource();
        src.buffer = audioBuffer;
        src.connect(ttsAudioContext.destination);
        src.start();
    } catch (err) {
        console.error("PCM error:", err);
    }
}

/* --------------------------------------------------
   CONNECT WS — FIXED MIC ENABLE LOGIC
-------------------------------------------------- */
export function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))
        return wsReady || Promise.resolve();

    ws = new WebSocket(
        (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host + "/ws/stream"
    );
    ws.binaryType = "arraybuffer";

    wsReady = new Promise((resolve, reject) => {
        ws.addEventListener("open", () => {
            console.log("WS connected.");
            const btn = document.getElementById("btnStartMic");
            if (btn) btn.disabled = false;      // 🔥 FIXED HERE
            resolve();
        });

        ws.addEventListener("error", (err) => {
            console.error("WS error:", err);
            reject(err);
        });
    });

    ws.onclose = () => {
        stopAllPlayback();
        aiStreaming = false;
        aiBubble = null;
        ws = null;
        wsReady = null;
    };

    ws.onmessage = onWSMessage;

    return wsReady;
}

/* --------------------------------------------------
   MAIN WS MESSAGE HANDLER
-------------------------------------------------- */
async function onWSMessage(msg) {

    // ---------- AUDIO ----------
    if (msg.data instanceof ArrayBuffer) {
        playPcmChunk(msg.data);
        return;
    }

    let d;
    try { d = JSON.parse(msg.data); } catch { return; }

    // ---------- Wizard ----------
    if (d.type === "gdd_session_id") return setGDDSessionId(d.session_id);
    if (d.type === "wizard_answer") return appendMessage(d.text, "user");
    if (d.type === "gdd_export_ready") return downloadGDD();
    // --------------------------------------------------
    // WIZARD QUESTION (with Qx / N label + tooltip)
    // --------------------------------------------------
    if (d.type === "wizard_question") {

        // compute question index and total
        const Q = window.GDD_QUESTIONS || [];
        const currentIndex = Q.indexOf(d.text);   // 0-based
        const total = Q.length;

        const label = (currentIndex >= 0)
            ? `Q(${currentIndex + 1}/${total}): ${d.text}`
            : d.text;

        finalizeAI();  // ⬅ ensure wizard question always appears as its own standalone bubble
        appendMessage(label, "ai");


        // refresh tooltip
        import("./ui.js").then(m => m.createTooltip?.());

        return;
    }

    if (d.type === "gdd_next") {
        appendMessage(`Q(${d.index + 1}/${d.total}): ${d.question}`, "ai");

        // refresh tooltip
        import("./ui.js").then(m => m.createTooltip?.());

        return;
    }

    if (d.type === "gdd_done") {
        appendMessage("🎉 All questions answered! Say Finish GDD.", "ai");
        return;
    }

    // ---------- USER FINAL ----------
    if (d.type === "final") {
        appendMessage(d.text, "user");
        return;
    }

    // ---------- IGNORE TOKEN STREAM ----------
    if (d.type === "llm_stream") return;

    // ---------- LLM SENTENCE STREAM ----------
    if (d.type === "llm_sentence") {

        // ❌ Never mix wizard questions inside a streaming LLM bubble
        if (d.source === "wizard") return;

        if (!aiStreaming) {
            aiBubble = startNewAIBubble();
            aiStreaming = true;
        }
        aiBubble.querySelector(".content").textContent += d.sentence + " ";
        return;
    }


    if (d.type === "llm_done") {
        finalizeAI();
        aiStreaming = false;
        aiBubble = null;
        return;
    }

    // ---------- TTS / SPEECH SYNC ----------
    if (d.type === "sentence_start") {

        // ❌ Do NOT render wizard TTS sentences (they already appear via wizard_question)
        if (d.source === "wizard") return;

        // Normal LLM speech streaming
        if (!aiStreaming) {
            aiBubble = startNewAIBubble();
            aiStreaming = true;
        }

        aiBubble.querySelector(".content").textContent += (d.text || "").trim() + " ";
        return;
    }


    if (d.type === "voice_done") {
        finalizeAI();
        aiStreaming = false;
        aiBubble = null;
        return;
    }

    // ---------- STOP ----------
    if (d.type === "stop_all") {
        stopAllPlayback();
        finalizeAI();
        aiStreaming = false;
        aiBubble = null;
        return;
    }
}
