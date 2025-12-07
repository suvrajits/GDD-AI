// gdd.js
/* --------------------------------------------------
   INTERNAL STATE (not importing UI)
-------------------------------------------------- */
let _gddWizardActive = false;

export function isGddWizardActive() {
    return _gddWizardActive;
}
export function setGddWizardActive(v) {
    _gddWizardActive = !!v;
}

export let gddSessionId = null;
export let currentGDDMarkdown = "";

/* --------------------------------------------------
   GDD API — these functions DO NOT call appendMessage()
   They return structured objects the UI will render.
-------------------------------------------------- */

export async function startGDDWizard() {
    // mark active immediately so callers can update UI
    setGddWizardActive(true);

    const res = await fetch("/gdd/start", { method: "POST" });
    const data = await res.json();
    gddSessionId = data.session_id;

    return {
        status: "ok",
        event: "wizard_activated",
        text: "🎮 **GDD Wizard Activated!** Say **Go Next** anytime to proceed.",
        session_id: gddSessionId,
        raw: data
    };
}

/* =========================
   UPDATED: answerGDD()
   - maps backend shape -> frontend shape
   - returns event: "next_question" when a question is returned
   - returns status "stay" / "done" consistent with backend
   ========================= */
export async function answerGDD(userText) {
    const res = await fetch("/gdd/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: gddSessionId, answer: userText })
    });

    const data = await res.json();

    // backend possibilities:
    // - status: "ok" and returns { question, index, total }  (next question)
    // - status: "stay" and returns { question, index, total } (same question / brainstorming)
    // - status: "done" when all done
    // Map them into consistent frontend events:
    if (data.status === "done") {
        return {
            status: "done",
            event: "done",
            raw: data
        };
    }

    // If backend returned a question (either ok or stay), normalize to next_question event
    if (data.question !== undefined && data.question !== null) {
        // Keep backend 'status' for UI behavior if needed
        return {
            status: data.status ?? "ok",
            event: "next_question",
            question: data.question,
            index: data.index ?? null,
            total: data.total ?? null,
            raw: data
        };
    }

    // Fallback: return raw response as-is
    return {
        status: data.status ?? "ok",
        event: "answer_recorded",
        raw: data
    };
}

export async function nextGDD() {
    const res = await fetch("/gdd/next", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: gddSessionId })
    });

    const data = await res.json();
    return {
        status: data.status ?? "ok",
        event: "next_question",
        question: data.question ?? null,
        index: data.index ?? null,
        total: data.total ?? null,
        raw: data
    };
}

export async function finishGDD() {
    const res = await fetch("/gdd/finish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: gddSessionId })
    });

    const data = await res.json();

    if (data.status !== "ok") {
        // keep the wizard active so user can retry
        return { status: "error", event: "finish_failed", raw: data };
    }

    currentGDDMarkdown = data.markdown;
    setGddWizardActive(false);

    return {
        status: "ok",
        event: "finished",
        markdown: data.markdown,
        export_available: !!data.export_available,
        raw: data
    };
}

export async function downloadGDD() {
    if (!gddSessionId) return { status: "error", reason: "no_session" };

    const res = await fetch("/gdd/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: gddSessionId })
    });

    if (!res.ok) return { status: "error", reason: "export_failed", code: res.status };

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `GDD_${gddSessionId}.docx`;
    a.click();
    URL.revokeObjectURL(url);

    return { status: "ok" };
}

export function setGDDSessionId(id) {
    gddSessionId = id;
}
