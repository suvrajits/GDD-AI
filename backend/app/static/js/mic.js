// mic.js — FINAL FIXED VERSION FOR STREAMING + INTERRUPTIONS
import { connectWS, ws } from "./ws.js";
import { stopAllPlayback } from "./ws.js";

export let micActive = false;

let audioContext = null;
let workletNode = null;
let mediaStream = null;

export async function startMicStreaming() {
    try {
        // Interrupt any current TTS/LLM
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "stop_llm" }));
        }
    } catch {}

    stopAllPlayback();

    if (micActive) return;
    micActive = true;

    const btn = document.getElementById("btnStartMic");
    if (btn) btn.classList.add("active");

    // Ensure WS is connected
    try { await connectWS(); } catch (e) { console.warn("connectWS failed", e); }

    // Request microphone
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });

    audioContext = new AudioContext();
    await audioContext.resume();   // important for browsers that suspend AudioContext

    // Load the PCM processor; ensure file exists at /static/pcm-worklet.js
    await audioContext.audioWorklet.addModule("/static/pcm-worklet.js");

    const src = audioContext.createMediaStreamSource(mediaStream);
    workletNode = new AudioWorkletNode(audioContext, "pcm-processor");

    // Send raw PCM frames to backend
    workletNode.port.onmessage = (e) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(e.data);
        }
    };

    src.connect(workletNode);
}

export function stopMic(closeWs = false) {
    micActive = false;
    const btn = document.getElementById("btnStartMic");
    if (btn) btn.classList.remove("active");
    if (btn) btn.disabled = false;

    try { if (workletNode) workletNode.disconnect(); } catch {}
    try { if (audioContext) audioContext.close(); } catch {}

    workletNode = null;
    audioContext = null;

    try { if (mediaStream) mediaStream.getTracks().forEach(t => t.stop()); } catch {}
    mediaStream = null;

    if (closeWs && ws && ws.readyState === WebSocket.OPEN) {
        ws.close();
    }
}

// Button wiring — ensure these elements exist in your index.html
document.getElementById("btnStartMic").onclick = async () => {
    try { await connectWS(); } catch {}
    if (!micActive) startMicStreaming();
    else stopMic(false);
};

document.getElementById("btnStopMic").onclick = () => {
    try {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "stop_llm" }));
        }
    } catch {}

    stopAllPlayback();
    stopMic(false);
};
