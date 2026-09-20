const INCIDENT_MARKER = "<!-- scheduled-evals-incident -->";
const RUN_MARKER = /<!-- scheduled-evals-run:(\d+):(\d+):([^ ]+) -->/;
const ALLOWED_RESULTS = new Set(["success", "failure", "cancelled", "skipped"]);

function parseRunMarker(comment) {
  if (comment.user?.login !== "github-actions[bot]" || comment.user?.type !== "Bot") {
    return null;
  }
  const match = comment.body?.match(RUN_MARKER);
  if (!match || !ALLOWED_RESULTS.has(match[3])) return null;
  return {
    runId: BigInt(match[1]),
    runAttempt: BigInt(match[2]),
    result: match[3],
  };
}

function compareRuns(left, right) {
  if (left.runId !== right.runId) return left.runId < right.runId ? -1 : 1;
  if (left.runAttempt === right.runAttempt) return 0;
  return left.runAttempt < right.runAttempt ? -1 : 1;
}

async function reconcileScheduledEvals({
  github,
  context,
  incidentNumber,
  result,
  runAttempt,
  runId,
  runUrl,
}) {
  if (!Number.isInteger(incidentNumber) || incidentNumber <= 0) {
    throw new Error("scheduled Evals incident number must be a positive integer");
  }
  if (!/^\d+$/.test(runId) || !/^\d+$/.test(runAttempt)) {
    throw new Error("scheduled Evals run identity must be numeric");
  }

  const incidentResponse = await github.rest.issues.get({
    ...context.repo,
    issue_number: incidentNumber,
  });
  const incident = incidentResponse.data;
  if (incident.pull_request || !incident.body?.includes(INCIDENT_MARKER)) {
    throw new Error(`Issue #${incidentNumber} is not the scheduled Evals incident`);
  }

  const evidence = `Scheduled Evals result: **${result}** — ${runUrl}`;
  const prefix = result === "success" ? "Recovery observed. " : "";
  await github.rest.issues.createComment({
    ...context.repo,
    issue_number: incidentNumber,
    body: `<!-- scheduled-evals-run:${runId}:${runAttempt}:${result} -->\n${prefix}${evidence}`,
  });

  const latestReportedRun = async () => {
    const comments = await github.paginate(github.rest.issues.listComments, {
      ...context.repo,
      issue_number: incidentNumber,
      per_page: 100,
    });
    return comments
      .map((comment) => parseRunMarker(comment))
      .filter((run) => run !== null)
      .sort(compareRuns)
      .at(-1);
  };

  const applyResult = async (run) => {
    const update = {
      ...context.repo,
      issue_number: incidentNumber,
      state: run.result === "success" ? "closed" : "open",
    };
    if (run.result === "success") {
      update.state_reason = "completed";
    } else {
      update.assignees = [context.repo.owner];
    }
    await github.rest.issues.update(update);
  };

  let applied = await latestReportedRun();
  if (!applied) {
    throw new Error("scheduled Evals run evidence was not discoverable");
  }
  await applyResult(applied);

  // A newer reporter can append evidence between our list and update. Re-read after
  // applying so the last finisher converges the Issue to the newest durable run record.
  const confirmed = await latestReportedRun();
  if (!confirmed) {
    throw new Error("scheduled Evals run evidence disappeared during reconciliation");
  }
  if (compareRuns(applied, confirmed) !== 0) {
    applied = confirmed;
    await applyResult(applied);
  }
}

module.exports = { reconcileScheduledEvals };
