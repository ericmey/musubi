const INCIDENT_MARKER = "<!-- scheduled-evals-incident -->";
const INCIDENT_TITLE = "Scheduled Evals live-quality gate is failing";

async function reconcileScheduledEvals({ github, context, result, runUrl }) {
  const listIncidents = async () => {
    const issues = await github.paginate(github.rest.issues.listForRepo, {
      ...context.repo,
      state: "all",
      per_page: 100,
    });
    return issues
      .filter((issue) => !issue.pull_request && issue.body?.includes(INCIDENT_MARKER))
      .sort((left, right) => left.number - right.number);
  };

  let incidents = await listIncidents();
  const evidence = `Scheduled Evals result: **${result}** — ${runUrl}`;

  if (result === "success" && incidents.length === 0) {
    return;
  }

  if (incidents.length === 0) {
    await github.rest.issues.create({
      ...context.repo,
      title: INCIDENT_TITLE,
      assignees: [context.repo.owner],
      labels: ["bug", "infrastructure", "tests", "status:in-progress"],
      body:
        `${INCIDENT_MARKER}\n` +
        "This Issue is maintained by the scheduled Evals workflow. " +
        "It remains open until a later scheduled or manually dispatched run succeeds.",
    });
    // Multiple reporters may observe an empty set concurrently. Re-read after creation,
    // elect the lowest Issue number, and retire every losing candidate. This preserves
    // every reporter run without relying on Actions concurrency, whose pending slot drops
    // older events when a third run arrives.
    incidents = await listIncidents();
    if (incidents.length === 0) {
      throw new Error("created scheduled Evals incident was not discoverable");
    }
  }

  const [incident, ...duplicates] = incidents;
  for (const duplicate of duplicates) {
    await github.rest.issues.update({
      ...context.repo,
      issue_number: duplicate.number,
      state: "closed",
      state_reason: "not_planned",
      body: `Superseded duplicate of #${incident.number} after concurrent reconciliation.`,
    });
  }

  if (result === "success") {
    if (incident.state === "open") {
      await github.rest.issues.createComment({
        ...context.repo,
        issue_number: incident.number,
        body: `Recovery observed. ${evidence}`,
      });
      await github.rest.issues.update({
        ...context.repo,
        issue_number: incident.number,
        state: "closed",
        state_reason: "completed",
      });
    }
    return;
  }

  if (incident.state !== "open") {
    await github.rest.issues.update({
      ...context.repo,
      issue_number: incident.number,
      state: "open",
    });
  }
  await github.rest.issues.createComment({
    ...context.repo,
    issue_number: incident.number,
    body: evidence,
  });
}

module.exports = { reconcileScheduledEvals };
