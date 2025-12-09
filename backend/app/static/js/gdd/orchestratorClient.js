// static/js/gdd/orchestratorClient.js
export async function callOrchestrator(apiUrl, concept, answers) {
  const payload = { concept, answers };
  const resp = await fetch(apiUrl + "/api/orchestrate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error("Orchestrator error: " + txt);
  }
  return resp.json();
}
