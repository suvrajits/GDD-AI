// ui.js

/* --------------------------------------------------
   UI STATE (shared)
-------------------------------------------------- */
export let currentAiDiv = null;
export let currentSessionIsVoice = false;

import { isGddWizardActive } from "./gdd.js";

/* --------------------------------------------------
   appendMessage()
-------------------------------------------------- */
export function appendMessage(text, role, opts = {}) {
    const div = document.createElement("div");
    div.className = "msg " + role + (opts.streaming ? " streaming" : "");

    const container = document.getElementById("messages");

    if (role === "ai") {
        const wrap = document.createElement("div");
        wrap.className = "content";
        wrap.textContent = text;
        div.appendChild(wrap);

        container.appendChild(div);

        // Remove old tooltip
        document.querySelectorAll(".ai-tip").forEach(t => t.remove());

        // Create new tooltip
        const tip = document.createElement("div");
        tip.className = "ai-tip";

        // NEW: dynamic, instant tooltip logic
        if (isGddWizardActive()) {
            tip.textContent = "💡 Say “Go Next” or “Finish GDD”";
        } else {
            tip.textContent = "💡 Say “Activate GDD Wizard” to begin creating the GDD";
        }

        container.appendChild(tip);

        container.scrollTop = container.scrollHeight;
        return div;
    }

    // USER message
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
}

/* --------------------------------------------------
   appendToAI()
-------------------------------------------------- */
export function appendToAI(text) {
    if (!currentAiDiv) {
        currentAiDiv = appendMessage("", "ai", { streaming: true });
    }
    currentAiDiv.querySelector(".content").textContent += text;
}

/* --------------------------------------------------
   finalizeAI()
-------------------------------------------------- */
export function finalizeAI() {
    if (currentAiDiv) {
        currentAiDiv.classList.remove("streaming");
        currentAiDiv = null;
    }
}
