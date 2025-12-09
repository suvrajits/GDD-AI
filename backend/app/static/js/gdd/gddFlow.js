// static/js/gdd/gddFlow.js
import { callOrchestrator } from './orchestratorClient.js';

(function () {
  // QUESTIONS — short, incremental. Keys used to build final context.
  const QUESTIONS = [
    { key: "title", q: "What's the one-line name / tagline for this game?" },
    { key: "genre", q: "Game genre (e.g., 2v2 auto-battler)? Keep short." },
    { key: "core_loop", q: "Describe the core loop in one sentence." },
    { key: "target_audience", q: "Who is the target player? (casual, competitive, age range)" },
    { key: "platforms", q: "Primary platforms (iOS / Android / PC)?" },
    { key: "unique", q: "What is the single unique hook (hero fusion etc.)?" },
    { key: "monetization", q: "Preferred monetization model? (cosmetics, battle pass, IAP)" },
    { key: "kpi", q: "Desired KPI targets (D1, D7, D30) or leave blank." },
    { key: "constraints", q: "Key constraints (team size, budget, timeline)?" },
    { key: "style", q: "Art & audio style — 2-3 words." }
  ];

  // UI references (assumes IDs from index.html)
  const messagesEl = document.getElementById("messages");
  const textInput = document.getElementById("textInput");
  const btnSend = document.getElementById("btnSend");

  // small state
  let idx = -1;
  let answers = {}; // key -> text
  const apiBase = window.location.origin; // adapt if backend differs
  const orchestratorBase = apiBase; // or something like http://localhost:8001

  // helper to append a chat message bubble
  function appendMsg(text, cls = "ai") {
    const d = document.createElement("div");
    d.className = "msg " + cls;
    d.textContent = text;
    messagesEl.appendChild(d);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function startGDDFlow() {
    idx = 0;
    answers = {};
    appendMsg("Starting GDD guided session. I'll ask short questions. Reply in the input box.", "ai");
    askCurrent();
  }

  function askCurrent() {
    if (idx < 0 || idx >= QUESTIONS.length) {
      appendMsg("All questions asked. Type 'generate gdd' to create the full GDD, or 'review' to list answers.", "ai");
      return;
    }
    const q = QUESTIONS[idx].q;
    appendMsg(q, "ai");
  }

  // process user answer
  async function processUserText(text) {
    const t = text.trim();
    if (!t) return;

    // special commands
    if (t.toLowerCase() === "/startgdd" || t.toLowerCase() === "start gdd") {
      startGDDFlow();
      return;
    }

    if (t.toLowerCase() === "review") {
      // show answers so far
      const keys = Object.keys(answers);
      if (!keys.length) {
        appendMsg("No answers yet.", "ai");
      } else {
        appendMsg("Pinned answers:\n" + JSON.stringify(answers, null, 2), "ai");
      }
      return;
    }

    if (t.toLowerCase() === "generate gdd" || t.toLowerCase() === "/generate") {
      appendMsg("Generating the GDD. Orchestrating personas — this may take a few seconds...", "ai");
      try {
        const res = await callOrchestrator(orchestratorBase, answers.title || "Unnamed Concept", answers);
        if (res.integration_markdown) {
          appendMsg("=== FINAL GDD (MARKDOWN) ===", "ai");
          appendMsg(res.integration_markdown, "ai");
        } else if (res.ok && res.raw_integration) {
          appendMsg("Integration output returned. Showing raw markdown if present.", "ai");
          appendMsg(JSON.stringify(res.raw_integration, null, 2), "ai");
        } else {
          appendMsg("Orchestrator returned unexpected response: " + JSON.stringify(res).slice(0, 400), "ai");
        }
      } catch (err) {
        appendMsg("Orchestrator call failed: " + err.message, "ai");
      }
      return;
    }

    // If we're in question mode (idx in range) store answer and advance
    if (idx >= 0 && idx < QUESTIONS.length) {
      const key = QUESTIONS[idx].key;
      answers[key] = t;
      // pin to localStorage so the user can resume if needed
      localStorage.setItem("gdd_answers", JSON.stringify(answers));
      appendMsg("Saved.", "ai");
      idx++;
      if (idx < QUESTIONS.length) {
        askCurrent();
      } else {
        appendMsg("All done — type 'generate gdd' to run the orchestrator, or 'review' to check saved answers.", "ai");
      }
      return;
    }

    // fallback normal chat: just echo user message and keep existing flow
    appendMsg("I didn't understand that. Type '/startgdd' to begin the guided GDD flow.", "ai");
  }

  // wire send button and Enter
  btnSend.addEventListener("click", () => {
    const txt = textInput.value || "";
    if (!txt.trim()) return;
    // append user bubble
    const u = document.createElement("div");
    u.className = "msg user";
    u.textContent = txt;
    messagesEl.appendChild(u);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    textInput.value = "";
    processUserText(txt);
  });

  textInput.addEventListener("keyup", (e) => {
    if (e.key === "Enter") {
      btnSend.click();
    }
  });

  // Hook 'New Concept' sidebar button if present (non-invasive)
  const sideButtons = document.querySelectorAll(".side-btn");
  if (sideButtons && sideButtons.length) {
    // First side-btn is New Concept per index.html ordering
    const newConceptBtn = sideButtons[0];
    if (newConceptBtn) {
      newConceptBtn.addEventListener("click", (ev) => {
        startGDDFlow();
      });
    }
  }

  // On page load, restore any pinned answers
  const saved = localStorage.getItem("gdd_answers");
  if (saved) {
    try {
      answers = JSON.parse(saved);
      appendMsg("Restored pinned answers. Type 'review' to inspect or '/startgdd' to continue questions.", "ai");
    } catch (e) {}
  }

})();
