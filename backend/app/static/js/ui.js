// ui.js — FINAL, STABLE, FULLY COMPATIBLE WITH ws.js

export let currentAiDiv = null;
export let currentSessionIsVoice = false;

import { isGddWizardActive, GDD_WIZARD_FINISHED } from "./gdd.js";

/* --------------------------------------------------
   createTooltip()
-------------------------------------------------- */
export function createTooltip() {
    const container = document.getElementById("messages");

    // Remove old tooltips
    document.querySelectorAll(".ai-tip").forEach(t => t.remove());

    const tip = document.createElement("div");
    tip.className = "ai-tip";

   if (GDD_WIZARD_FINISHED) {
        tip.textContent = "📘 GDD Generated! Say “Download GDD” or “Export GDD” to download your document.";
    }
    else if (isGddWizardActive()) {
        tip.textContent = "💡 Say “Go Next” for the next question — or “Finish GDD” to compose the full document.";
    }
    else {
        tip.textContent = "💡 Say “Activate GDD Wizard” to start creating your GDD anytime.";
    }

    container.appendChild(tip);
    container.scrollTop = container.scrollHeight;
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
        createTooltip(); // refresh tooltip after AI output
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

// --------------------------------------------------
// GDD Glow Helpers (global UI utilities)
// --------------------------------------------------
export function clearGddGlow() {
    document.querySelectorAll(".gdd-glow").forEach(el => el.classList.remove("gdd-glow"));
}

export function applyGddGlow(div) {
    if (!div) return;
    div.classList.add("gdd-glow");
}
