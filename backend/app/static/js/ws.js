// ws.js  (FULLY PATCHED FOR NEW GDD WIZARD)
// ------------------------------------------

import { appendMessage, appendToAI, finalizeAI } from "./ui.js";
import { downloadGDD, setGDDSessionId } from "./gdd.js";

export let ws = null;
export let wsReady = null;

let ttsAudioContext = null;
let aiStreaming = false;

/* --------------------------------------------------
   Stop playback
-------------------------------------------------- */
export function stopAllPlayback() {
    try { if (ttsAudioContext?.close) ttsAudioContext.close(); }
    catch {}
    ttsAudioContext = null;
}

/* --------------------------------------------------
   PCM playback
-------------------------------------------------- */
export function playPcmChunk(buffer) {
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
}

/* --------------------------------------------------
   connectWS()
-------------------------------------------------- */
export function connectWS() {
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) {
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
        ws.addEventListener("error", reject);
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
   WebSocket Message Router
-------------------------------------------------- */
async function onWSMessage(msg) {

    if (msg.data instanceof ArrayBuffer) {
        playPcmChunk(msg.data);
        return;
    }

    let d = {};
    try { d = JSON.parse(msg.data); }
    catch { return; }

    // ============================================================
    // 🟩 NEW WIZARD EVENTS (voice-driven)
    // ============================================================

    // Backend tells frontend which session ID belongs to wizard
    if (d.type === "gdd_session_id") {
        setGDDSessionId(d.session_id);
        return;
    }

    // Backend sends next question (voice)
    if (d.type === "gdd_next") {
        const idx = (d.index !== undefined && d.index !== null)
            ? (d.index + 1)
            : "?";
        const total = d.total ?? "?";

        appendMessage(`Q(${idx}/${total}): ${d.question}`, "ai");
        return;
    }

    // Backend sends wizard_answer (voice transcription)
    if (d.type === "wizard_answer") {
        appendMessage(d.text, "user");
        return;
    }

    // Backend indicates wizard is done
    if (d.type === "gdd_done") {
        appendMessage("🎉 All questions answered! Say **Finish GDD**.", "ai");
        return;
    }

    // Backend says export is ready
    if (d.type === "gdd_export_ready") {
        downloadGDD();
        return;
    }

    // ============================================================
    // 🟦 NORMAL FINAL STT
    // ============================================================
    if (d.type === "final") {
        appendMessage(d.text, "user");
        return;
    }

    // ============================================================
    // 🟪 LLM STREAMING (same as before)
    // ============================================================
    if (d.type === "llm_stream") {
        if (!aiStreaming) {
            appendToAI(""); // create bubble
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

    // ============================================================
    // 🟧 TTS STREAMING
    // ============================================================
    if (d.type === "sentence_start") {
        if (!aiStreaming) {
            appendToAI(""); // create bubble
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

    if (d.type === "stop_all") {
        stopAllPlayback();
        finalizeAI();
        aiStreaming = false;
        return;
    }
}
