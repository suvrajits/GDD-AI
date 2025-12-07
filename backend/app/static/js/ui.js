// ui.js — FINAL, STABLE, FULLY COMPATIBLE WITH ws.js

export let currentAiDiv = null;
export let currentSessionIsVoice = false;

import { isGddWizardActive } from "./gdd.js";

/* --------------------------------------------------
   createTooltip()
-------------------------------------------------- */
function createTooltip() {
    const container = document.getElementById("messages");

    // Remove all previous tooltips
    document.querySelectorAll(".ai-tip").forEach(t => t.remove());

    const tip = document.createElement("div");
    tip.className = "ai-tip";

    if (isGddWizardActive()) {
        tip.textContent = "💡 Say “Go Next” or “Finish GDD”.";
    } else {
        tip.textContent = "💡 Say “Activate GDD Wizard” to begin.";
    }

    container.appendChild(tip);
}

/* --------------------------------------------------
   startNewAIBubble() — Called by ws.js
-------------------------------------------------- */
export function startNewAIBubble() {
    const container = document.getElementById("messages");

    const div = document.createElement("div");
    div.className = "msg ai streaming";

    const content = document.createElement("div");
    content.className = "content";
    content.textContent = "";
    div.appendChild(content);

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;

    currentAiDiv = div;
    return div;
}

/* --------------------------------------------------
   appendMessage() — For static user/AI messages
-------------------------------------------------- */
export function appendMessage(text, role, opts = {}) {
    const container = document.getElementById("messages");

    if (!opts.streaming && (!text || text.trim() === "")) return;

    const div = document.createElement("div");
    div.className = "msg " + role + (opts.streaming ? " streaming" : "");

    if (role === "ai") {
        const content = document.createElement("div");
        content.className = "content";
        content.textContent = text ?? "";
        div.appendChild(content);

        container.appendChild(div);
        createTooltip();
        container.scrollTop = container.scrollHeight;

        return div;
    }

    // User bubble
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
}

/* --------------------------------------------------
   appendToAI() — Extend an existing AI bubble
-------------------------------------------------- */
export function appendToAI(text) {
    if (!currentAiDiv) {
        currentAiDiv = startNewAIBubble();
    }
    const content = currentAiDiv.querySelector(".content");
    content.textContent += text;

    const container = document.getElementById("messages");
    container.scrollTop = container.scrollHeight;
}

/* --------------------------------------------------
   finalizeAI() — End the streaming bubble
-------------------------------------------------- */
export function finalizeAI() {
    if (!currentAiDiv) return;

    currentAiDiv.classList.remove("streaming");
    currentAiDiv = null;

    const container = document.getElementById("messages");
    container.scrollTop = container.scrollHeight;
}
