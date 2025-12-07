// mic.js
import { ws } from "./ws.js";

export let micActive = false;
let audioContext = null;
let workletNode = null;

export async function startMicStreaming() {
    if (micActive) return;
    micActive = true;

    document.getElementById("btnStartMic").classList.add("active");

    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioContext = new AudioContext();

    await audioContext.audioWorklet.addModule("/static/pcm-worklet.js");

    const src = audioContext.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(audioContext, "pcm-processor");

    workletNode.port.onmessage = (e) => {
        if (ws?.readyState === WebSocket.OPEN) ws.send(e.data);
    };

    src.connect(workletNode);
}

export function stopMic(closeWs = true) {
    micActive = false;
    document.getElementById("btnStartMic").classList.remove("active");

    try { workletNode?.disconnect(); } catch {}
    try { audioContext?.close(); } catch {}

    workletNode = null;
    audioContext = null;

    if (closeWs && ws?.readyState === WebSocket.OPEN) ws.close();
}
