// app.js (FULL UPDATED VERSION)

/* --------------------------------------------------
   Imports
-------------------------------------------------- */
import { connectWS, stopAllPlayback, ws } from "./ws.js";
import { appendMessage, finalizeAI, currentSessionIsVoice } from "./ui.js";

import {
    startGDDWizard,
    answerGDD,
    nextGDD,
    finishGDD,
    downloadGDD,
    isGddWizardActive,
} from "./gdd.js";

import {
    startMicStreaming,
    stopMic,
    micActive
} from "./mic.js";

/* --------------------------------------------------
   SEND TEXT — Includes FULL Wizard Logic
-------------------------------------------------- */
export async function sendText() {
    const msg = textInput.value.trim();
    if (!msg) return;
    textInput.value = "";

    const lower = msg.toLowerCase();

    /* --------------------------------------------
       1) WIZARD ACTIVE → USER ANSWERS
    -------------------------------------------- */
    if (isGddWizardActive() && !lower.includes("go next")) {
        appendMessage(msg, "user");

        try {
            const res = await answerGDD(msg);

            if (res.event === "next_question") {
                appendMessage(
                    `Q(${(res.index ?? 0) + 1}/${res.total ?? "?"}): ${res.question}`,
                    "ai"
                );
            } else if (res.event === "done") {
                appendMessage("🎉 All questions collected! Say **Finish GDD**.", "ai");
            }
        } catch (e) {
            appendMessage("❌ Failed to record answer.", "ai");
        }
        return;
    }

    /* --------------------------------------------
       2) WIZARD ACTIVE → USER SAYS "GO NEXT"
    -------------------------------------------- */
    if (isGddWizardActive() && lower.includes("go next")) {
        appendMessage(msg, "user");

        try {
            const res = await nextGDD();

            if (res.event === "next_question") {
                appendMessage(
                    `Q(${(res.index ?? 0) + 1}/${res.total ?? "?"}): ${res.question}`,
                    "ai"
                );
            } else if (res.event === "done") {
                appendMessage("🎉 All questions answered! Say **Finish GDD**.", "ai");
            }

        } catch (e) {
            appendMessage("❌ Failed to fetch next question.", "ai");
        }
        return;
    }

    /* --------------------------------------------
       3) WIZARD ACTIVATION
    -------------------------------------------- */
    const triggers = [
        "create gdd",
        "start gdd",
        "gdd wizard",
        "design document",
        "activate gdd wizard",
        "activate g d d wizard",
    ];

    if (triggers.some(t => lower.includes(t))) {
        appendMessage(msg, "user");

        try {
            await startGDDWizard();

            appendMessage("🎮 **GDD Wizard Activated!**", "ai");

            // Fetch first question
            const q = await nextGDD();
            if (q.event === "next_question") {
                appendMessage(
                    `Q(${(q.index ?? 0) + 1}/${q.total ?? "?"}): ${q.question}`,
                    "ai"
                );
            }
        } catch (e) {
            appendMessage("❌ Failed to start GDD Wizard.", "ai");
        }

        return;
    }

    /* --------------------------------------------
       4) FINISH GDD DOCUMENT
    -------------------------------------------- */
    if (lower === "finish gdd" || lower === "generate gdd") {
        appendMessage(msg, "user");

        try {
            appendMessage("🧠 Generating your GDD...", "ai");

            const res = await finishGDD();

            if (res.status === "ok") {
                appendMessage("📘 **Your GDD is ready!**", "ai");
                appendMessage(res.markdown, "ai");

                if (res.export_available) {
                    appendMessage("⬇️ Click Export to Word to download your GDD.", "ai");
                }
            } else {
                appendMessage("❌ Error generating GDD.", "ai");
            }

        } catch (e) {
            appendMessage("❌ Failed to generate GDD.", "ai");
        }
        return;
    }

    /* --------------------------------------------
       5) EXPORT GDD DOCUMENT
    -------------------------------------------- */
    const exportTriggers = [
        "export gdd", "download gdd", "export document", "export doc",
        "download document", "download gdd doc", "export"
    ];

    if (exportTriggers.some(t => lower.includes(t))) {
        appendMessage(msg, "user");
        downloadGDD();
        return;
    }

    /* --------------------------------------------
       6) NORMAL CHAT FLOW (Non-Wizard)
    -------------------------------------------- */
    appendMessage(msg, "user");

    try {
        await connectWS();

        // Ensure no voice-streaming session continues
        currentSessionIsVoice = false;

        if (micActive) stopMic(false);
        stopAllPlayback();

        ws.send(JSON.stringify({ type: "text", text: msg }));

    } catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
    }
}

/* --------------------------------------------------
   Buttons
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
   Mic Buttons
-------------------------------------------------- */
document.getElementById("btnStartMic").onclick = async () => {
    try { await connectWS(); }
    catch (e) {
        appendMessage("[offline] WebSocket not connected", "ai");
        return;
    }

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
