// ui.js  (FINAL, CLEAN, UPDATED)

/* --------------------------------------------------
   UI STATE (shared)
-------------------------------------------------- */
export let currentAiDiv = null;
export let currentSessionIsVoice = false;

import { isGddWizardActive } from "./gdd.js";

/* --------------------------------------------------
   createTooltip() — internal helper
-------------------------------------------------- */
function createTooltip() {
    const container = document.getElementById("messages");

    // Remove existing tooltip
    document.querySelectorAll(".ai-tip").forEach(t => t.remove());

    // Create new tooltip
    const tip = document.createElement("div");
    tip.className = "ai-tip";

    if (isGddWizardActive()) {
        tip.textContent = "💡 Say “Go Next” or “Finish GDD”";
    } else {
        tip.textContent = "💡 Say “Activate GDD Wizard” to begin creating the GDD";
    }

    container.appendChild(tip);
}

/* --------------------------------------------------
   appendMessage()
-------------------------------------------------- */
export function appendMessage(text, role, opts = {}) {
    const container = document.getElementById("messages");

    if (!text && !opts.streaming) {
        return; // Avoid empty static bubbles
    }

    // Create chat bubble
    const div = document.createElement("div");
    div.className = "msg " + role + (opts.streaming ? " streaming" : "");

    // ---------------- AI MESSAGE ----------------
    if (role === "ai") {
        const wrap = document.createElement("div");
        wrap.className = "content";
        wrap.textContent = text ?? "";
        div.appendChild(wrap);

        container.appendChild(div);

        // Tooltip is always refreshed after AI replies
        createTooltip();

        container.scrollTop = container.scrollHeight;
        return div;
    }

    // ---------------- USER MESSAGE ----------------
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;

    return div;
}

/* --------------------------------------------------
   appendToAI() — Extend existing AI bubble during streaming
-------------------------------------------------- */
export function appendToAI(text) {
    if (!currentAiDiv) {
        // Create the streaming bubble if it doesn’t exist
        currentAiDiv = appendMessage("", "ai", { streaming: true });
    }

    const content = currentAiDiv.querySelector(".content");
    content.textContent += text;
    
    const container = document.getElementById("messages");
    container.scrollTop = container.scrollHeight;
}

/* --------------------------------------------------
   finalizeAI() — Close the streaming bubble
-------------------------------------------------- */
export function finalizeAI() {
    if (!currentAiDiv) return;

    currentAiDiv.classList.remove("streaming");
    currentAiDiv = null;

    const container = document.getElementById("messages");
    container.scrollTop = container.scrollHeight;
}
