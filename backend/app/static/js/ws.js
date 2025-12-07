// ws.js  (Unified + Cleaned for New Wizard System)
// -----------------------------------------------

import { appendMessage, appendToAI, finalizeAI } from "./ui.js";
import { downloadGDD, setGDDSessionId } from "./gdd.js";

export let ws = null;
export let wsReady = null;

let ttsAudioContext = null;
let aiStreaming = false;

/* --------------------------------------------------
   Stop all audio playback
-------------------------------------------------- */
export function stopAllPlayback() {
    try {
        if (ttsAudioContext && typeof ttsAudioContext.close === "function") {
            ttsAudioContext.close();
        }
    } catch (err) {
        console.warn("stopAllPlayback failed:", err);
    }
    ttsAudioContext = null;
}

/* --------------------------------------------------
   PCM Playback (16kHz mono)
-------------------------------------------------- */
export function playPcmChunk(buffer) {
    try {
        if (!ttsAudioContext) {
            ttsAudioContext = new AudioContext();
        }

        const pcm = new Int16Array(buffer);
        const f32 = new Float32Array(pcm.length);

        for (let i = 0; i < pcm.length; i++) {
            f32[i] = pcm[i] / 32768;
        }

        const audioBuffer = ttsAudioContext.createBuffer(1, f32.length, 16000);
        audioBuffer.getChannelData(0).set(f32);

        const src = ttsAudioContext.createBufferSource();
        src.buffer = audioBuffer;
        src.connect(ttsAudioContext.destination);
        src.start();
    }
    catch (err) {
        console.error("PCM playback error:", err);
    }
}

/* --------------------------------------------------
   connectWS()
-------------------------------------------------- */
export function connectWS() {
    // Already connected or connecting
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return wsReady || Promise.resolve();
    }

    ws = new WebSocket(
        (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host + "/ws/stream"
    );
    ws.binaryType = "arraybuffer";

    wsReady = new Promise((resolve, reject) => {
        ws.addEventListener("open", () => {
            document.getElementById("btnStartMic").disabled = false;
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
        ws = null;
        wsReady = null;
    };

    ws.onmessage = onWSMessage;

    return wsReady;
}

/* --------------------------------------------------
   WebSocket Router
-------------------------------------------------- */
async function onWSMessage(msg) {

    // PCM audio packets
    if (msg.data instanceof ArrayBuffer) {
        playPcmChunk(msg.data);
        return;
    }

    let d;
    try {
        d = JSON.parse(msg.data);
    } catch {
        return;
    }

    // ================================
    //  WIZARD EVENTS (Unified)
    // ================================

    // Backend → Wizard session ID
    if (d.type === "gdd_session_id") {
        setGDDSessionId(d.session_id);
        return;
    }

    // Wizard → Recognized user answer (voice)
    if (d.type === "wizard_answer") {
        appendMessage(d.text, "user");
        return;
    }

    // Wizard → Next question
    if (d.type === "gdd_next") {
        const idx = (d.index !== undefined && d.index !== null)
            ? d.index + 1
            : "?";
        const total = d.total ?? "?";

        appendMessage(`Q(${idx}/${total}): ${d.question}`, "ai");
        return;
    }

    // Wizard → Finished all questions
    if (d.type === "gdd_done") {
        appendMessage("🎉 All questions answered! Say **Finish GDD**.", "ai");
        return;
    }

    // Wizard → Ask UI to download the GDD file
    if (d.type === "gdd_export_ready") {
        downloadGDD();
        return;
    }

    // ================================
    //  STT Final
    // ================================
    if (d.type === "final") {
        appendMessage(d.text, "user");
        return;
    }

    // ================================
    //  LLM Streaming
    // ================================
    if (d.type === "llm_stream") {
        if (!aiStreaming) {
            appendToAI(""); // Create bubble
            aiStreaming = true;
        }
        appendToAI(d.token);
        return;
    }

    if (d.type === "llm_done") {
        finalizeAI();
        aiStreaming = false;
        return;
    }

    // ================================
    //  TTS Synth Streaming
    // ================================
    if (d.type === "sentence_start") {
        if (!aiStreaming) {
            appendToAI("");
            aiStreaming = true;
        }
        appendToAI(d.text + " ");
        return;
    }

    if (d.type === "voice_done") {
        finalizeAI();
        aiStreaming = false;
        return;
    }

    // ================================
    //  Stop All
    // ================================
    if (d.type === "stop_all") {
        stopAllPlayback();
        finalizeAI();
        aiStreaming = false;
        return;
    }
}
