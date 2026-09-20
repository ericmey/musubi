const INCIDENT_MARKER = "<!-- scheduled-evals-incident -->";
const INCIDENT_TITLE = "Scheduled Evals live-quality gate is failing";

async function reconcileScheduledEvals({ github, context, result, runUrl }) {
  const issues = await github.paginate(github.rest.issues.listForRepo, {
    ...context.repo,
    state: "all",
    per_page: 100,
  });
  const incidents = issues
    .filter((issue) => !issue.pull_request && issue.body?.includes(INCIDENT_MARKER))
    .sort((left, right) => left.number - right.number);

  if (incidents.length > 1) {
    throw new Error(
      `expected at most one scheduled Evals incident, found ${incidents.length}`,
    );
  }

  const incident = incidents[0];
  const evidence = `Scheduled Evals result: **${result}** — ${runUrl}`;

  if (result === "success") {
    if (incident?.state === "open") {
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

  if (!incident) {
    await github.rest.issues.create({
      ...context.repo,
      title: INCIDENT_TITLE,
      assignees: [context.repo.owner],
      labels: ["bug", "infrastructure", "tests", "status:in-progress"],
      body:
        `${INCIDENT_MARKER}\n${evidence}\n\n` +
        "This Issue is maintained by the scheduled Evals workflow. " +
        "It remains open until a later scheduled or manually dispatched run succeeds.",
    });
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
