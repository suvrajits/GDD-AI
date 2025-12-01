let ws = null;
let audioContext = null;
let workletNode = null;

let currentAiDiv = null;   // AI streaming block

/* ---------- Message helpers ---------- */

function appendMessage(text, role, opts = {}) {
    const div = document.createElement("div");
    div.className = "msg " + role + (opts.streaming ? " streaming" : "");
    
    if (role === "ai") {
        const content = document.createElement("div");
        content.className = "content";
        content.textContent = text || "";
        div.appendChild(content);
    } else {
        div.textContent = text;
    }

    const messages = document.getElementById("messages");
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;

    return div;
}

function appendToAI(text) {
    if (!currentAiDiv) {
        currentAiDiv = appendMessage("", "ai", { streaming: true });
    }
    currentAiDiv.querySelector(".content").textContent += text;
    document.getElementById("messages").scrollTop = messages.scrollHeight;
}

function finalizeAI() {
    if (currentAiDiv) {
        currentAiDiv.classList.remove("streaming");
        currentAiDiv = null;
    }
}

/* ---------- WebSocket Handling ---------- */

function connectWS() {
    ws = new WebSocket("ws://localhost:8000/ws/stream");
    ws.binaryType = "arraybuffer";

    ws.onmessage = (msg) => {
        const d = JSON.parse(msg.data);

        if (d.type === "final") {
            appendMessage(d.text, "user");
            finalizeAI();
        }
        if (d.type === "llm_stream") {
            appendToAI(d.token);
        }
        if (d.type === "llm_done") {
            finalizeAI();
        }
    };

    ws.onopen = () => {
        console.log("WS connected");
        startMicStreaming();
    };
}

/* ---------- Microphone + PCM Worklet ---------- */

async function startMicStreaming() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

    audioContext = new AudioContext({ sampleRate: 16000 });
    await audioContext.audioWorklet.addModule("/static/pcm-worklet.js");

    const source = audioContext.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(audioContext, "pcm-processor");

    workletNode.port.onmessage = (event) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(event.data);
        }
    };

    source.connect(workletNode);
}

function stopMic() {
    try { workletNode.disconnect(); } catch {}
    try { audioContext.close(); } catch {}
    try { ws.close(); } catch {}
}

/* ---------- UI Buttons ---------- */

document.getElementById("btnStartMic").onclick = () => {
    connectWS();
};

document.getElementById("btnStopMic").onclick = () => {
    stopMic();
};
