export function handleDesignerFeedback(d, appendMessage, appendToAI, finalizeAI) {
    // Streaming feedback
    if (d.type === "designer_feedback") {
        if (!window.designerFeedbackDiv) {
            window.designerFeedbackDiv = appendMessage(
                "💬 **Designer Feedback**\n",
                "ai",
                { streaming: true, noTip: true }
            );
        }
        const content = window.designerFeedbackDiv.querySelector(".content");
        content.textContent += d.token;
        return true;
    }

    // End feedback
    if (d.type === "designer_feedback_done") {
        finalizeAI();
        window.designerFeedbackDiv = null;
        return true;
    }

    return false;
}
