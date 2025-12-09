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

// -------------------------------
// RAG: Upload + Embed
// -------------------------------
const ragFileInput = document.getElementById("ragFileInput");
const btnUploadEmbed = document.getElementById("btnUploadEmbed");
const uploadStatus = document.getElementById("uploadStatus");
const kbList = document.getElementById("kbList");

/* --------------------------------------------------
   LEFT & RIGHT PANEL TOGGLES
-------------------------------------------------- */

const sidebar = document.querySelector(".sidebar");
const workspace = document.querySelector(".workspace");

const sidebarToggle = document.getElementById("sidebarToggle");
const workspaceToggle = document.getElementById("workspaceToggle");


// Sidebar slide-in/out
if (sidebarToggle) {
    sidebarToggle.addEventListener("click", () => {
        sidebar.classList.toggle("collapsed");

        // Change arrow direction
        sidebarToggle.textContent = sidebar.classList.contains("collapsed")
            ? "❯"
            : "❮";
    });
}

// Workspace slide-in/out
if (workspaceToggle) {
    workspaceToggle.addEventListener("click", () => {
        workspace.classList.toggle("collapsed");

        // Change arrow direction
        workspaceToggle.textContent = workspace.classList.contains("collapsed")
            ? "❮"
            : "❯";
    });
}



/* Load Knowledge Base */
async function refreshKnowledgeBase() {
    const kbList = document.getElementById("kbList");
    if (!kbList) {
        console.warn("kbList element not found.");
        return;
    }

    kbList.innerHTML = `<div class="loading">Loading...</div>`;

    try {
        const res = await fetch("/rag/embedded-files");
        const json = await res.json();

        if (!json.files || json.files.length === 0) {
            kbList.innerHTML = `<div class="empty">No knowledge base files yet.</div>`;
            return;
        }

        kbList.innerHTML = "";

        json.files.forEach(filename => {
            const row = document.createElement("div");
            row.className = "kb-item";

            // File name
            const nameSpan = document.createElement("span");
            nameSpan.textContent = filename;

            // ❌ Delete button
            const delBtn = document.createElement("span");
            delBtn.className = "kb-remove";
            delBtn.textContent = "✖";

            // Delete handler
            delBtn.onclick = async () => {
                if (!confirm(`Delete '${filename}' from knowledge base?`)) return;

                try {
                    const res = await fetch(`/rag/file/${encodeURIComponent(filename)}`, {
                        method: "DELETE"
                    });

                    if (!res.ok) {
                        alert("Failed to delete file.");
                        return;
                    }

                    // Refresh list
                    await refreshKnowledgeBase();

                } catch (err) {
                    console.error(err);
                    alert("Error deleting file.");
                }
            };

            row.appendChild(nameSpan);
            row.appendChild(delBtn);
            kbList.appendChild(row);
        });

    } catch (err) {
        console.error(err);
        kbList.innerHTML = `<div class="error">Failed to load KB.</div>`;
    }
}




// Upload + embed files
async function uploadAndEmbedFiles() {
    const files = ragFileInput.files;
    if (!files || files.length === 0) {
        uploadStatus.textContent = "Please select files first.";
        uploadStatus.style.color = "red";
        return;
    }

    uploadStatus.textContent = "Uploading & embedding...";
    uploadStatus.style.color = "black";

    const form = new FormData();
    for (let f of files) form.append("files", f);

    try {
        const resp = await fetch("/rag/upload", {
            method: "POST",
            body: form
        });

        if (!resp.ok) {
            uploadStatus.textContent = "Upload failed.";
            uploadStatus.style.color = "red";
            return;
        }

        const data = await resp.json();

        if (data.success) {
            uploadStatus.textContent = "File(s) embedded successfully!";
            uploadStatus.style.color = "green";
            refreshKnowledgeBase();
        } else {
            uploadStatus.textContent = "Embedding error.";
            uploadStatus.style.color = "red";
        }

    } catch (err) {
        uploadStatus.textContent = "Upload error.";
        uploadStatus.style.color = "red";
        console.error(err);
    }
}

// Hook the upload button
btnUploadEmbed.addEventListener("click", async () => {
    uploadStatus.textContent = "Uploading...";
    const files = ragFileInput.files;

    if (!files.length) {
        uploadStatus.textContent = "Please select a file.";
        uploadStatus.style.color = "red";
        return;
    }

    try {
        // 1️⃣ Upload files
        const formData = new FormData();
        for (const f of files) formData.append("files", f);

        const uploadRes = await fetch("/rag/upload", {
            method: "POST",
            body: formData
        });

        if (!uploadRes.ok) {
            uploadStatus.textContent = "Upload failed.";
            uploadStatus.style.color = "red";
            return;
        }

        uploadStatus.textContent = "Embedding...";
        uploadStatus.style.color = "black";

        // 2️⃣ NOW ingest them
        const ingestRes = await fetch("/rag/ingest", { method: "POST" });
        const ingestJson = await ingestRes.json();

        if (!ingestRes.ok) {
            uploadStatus.textContent = ingestJson.detail || "Embedding failed.";
            uploadStatus.style.color = "red";
            return;
        }

        uploadStatus.textContent = "Embedded successfully ✔";
        uploadStatus.style.color = "green";

        await refreshKnowledgeBase();

    } catch (err) {
        console.error(err);
        uploadStatus.textContent = "Upload/Embed error.";
        uploadStatus.style.color = "red";
    }
});




// Load on startup
refreshKnowledgeBase();

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
