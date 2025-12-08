// ws.js — corrected, single-tooltip, no dynamic imports
import {
    appendMessage,
    appendToAI,
    finalizeAI,
    startNewAIBubble,
    createTooltip
} from "./ui.js";

import { downloadGDD, setGDDSessionId, setGddWizardActive } from "./gdd.js";


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
            if (btn) btn.disabled = false;      // 🔥 mic enable fix
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
    if (d.type === "gdd_session_id") {
        setGDDSessionId(d.session_id);
        createTooltip();
        return;
    }

    if (d.type === "wizard_answer") {
        appendMessage(d.text, "user");
        createTooltip();
        return;
    }

    if (d.type === "gdd_export_ready") {
        downloadGDD();
        createTooltip();
        return;
    }

    // NEW: wizard_notice must update tooltip immediately
    if (d.type === "wizard_notice") {
        setGddWizardActive(true);   // 🔥 REQUIRED
        appendMessage(d.text, "ai");
        createTooltip();   // 🔥 ensures tooltip switches to wizard mode instantly
        return;
    }


    // --------------------------------------------------
    // WIZARD QUESTION (with Qx / N label + tooltip)
    // --------------------------------------------------
    if (d.type === "wizard_question") {

        // compute question index and total using client's QUESTIONS if available
        const Q = window.GDD_QUESTIONS || [];
        const currentIndex = Q.indexOf(d.text);   // 0-based
        const total = Q.length;

        const label = (currentIndex >= 0)
            ? `Q(${currentIndex + 1}/${total}): ${d.text}`
            : d.text;

        // ensure wizard question is its own bubble
        finalizeAI();
        appendMessage(label, "ai");

        // single, stable tooltip refresh
        createTooltip();

        return;
    }

    if (d.type === "gdd_next") {
        appendMessage(`Q(${d.index + 1}/${d.total}): ${d.question}`, "ai");

        // single, stable tooltip refresh
        createTooltip();
        return;
    }

    if (d.type === "gdd_done") {
        setGddWizardActive(false);
        window.GDD_WIZARD_FINISHED = true;   // 🔥 REQUIRED so tooltip switches modes

        appendMessage("🎉 All questions answered! Generating your GDD...", "ai");

        createTooltip();  // tooltip now switches to finished mode

        return;
    }


    // ---------- USER FINAL ----------
    if (d.type === "final") {

        let markdown = null;

        // Case 1: plain markdown string
        if (typeof d.text === "string" && d.text.trim().startsWith("#")) {
            markdown = d.text;
        }

        // Case 2: backend returned JSON string containing { markdown: "..." }
        if (!markdown) {
            try {
                const parsed = JSON.parse(d.text);
                if (parsed && parsed.markdown) {
                    markdown = parsed.markdown;
                }
            } catch (e) {
                // ignore — not JSON
            }
        }

        // Detect final GDD output
        if (markdown) {
            window.GDD_WIZARD_FINISHED = true;
            setGddWizardActive(false);

            appendMessage("📘 Your GDD is ready!", "ai");
            appendMessage(markdown, "ai");

            createTooltip();  // NOW tooltip changes to download instructions
            return;
        }

        // Normal user message
        appendMessage(d.text, "user");
        createTooltip();
        return;
    }


    // ---------- IGNORE TOKEN STREAM ----------
    if (d.type === "llm_stream") return;

    // ---------- LLM SENTENCE STREAM ----------
    if (d.type === "llm_sentence") {
        // If server marks source 'wizard', avoid mixing it into LLM streaming bubble
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
        createTooltip();
        return;
    }

    // ---------- TTS / SPEECH SYNC ----------
    if (d.type === "sentence_start") {
        // Server may include a source field; if it's wizard we already displayed question text
        if (d.source === "wizard") return;

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
        createTooltip();
        return;
    }

    // ---------- STOP ----------
    if (d.type === "stop_all") {
        stopAllPlayback();
        finalizeAI();
        aiStreaming = false;
        aiBubble = null;
        createTooltip();
        return;
    }
}
