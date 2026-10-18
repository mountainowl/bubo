// Present only recorded evidence; absence of an outcome never means unresolved.
export function findingTitle(finding) {
  const line = (finding.body || '').split('\n').find(line => line.trim()) || '';
  return line.replace(/^[\s#>*-]+/, '').replace(/[*`]/g, '').trim().slice(0, 180)
    || `${finding.category || 'Finding'} in ${finding.file || 'unknown file'}`;
}

const severityRank = finding => ({ blocking: 0, critical: 0, high: 1 })[(finding.severity || "").toLowerCase()] ?? 2;

export function workbench(reviews = []) {
  const details = reviews.filter(review => Array.isArray(review.detail?.findings));
  const priority = details.flatMap(review => review.detail.findings.map(finding => ({review, finding})))
    .filter(({review,finding:f}) => !(f.run_id != null && review.detail.run?.run_id === f.run_id && review.detail.run.dry_run)
      && f.status === 'posted'
      && ['blocking','high','critical'].includes((f.severity || '').toLowerCase())
      && ![0,false].includes(f.verified)
      && (!f.outcome || !['resolved','deleted','disputed','false_positive','duplicate'].some(key => f.outcome[key])))
    .sort((a,b) => severityRank(a.finding) - severityRank(b.finding)
      || Number(Boolean(a.finding.outcome)) - Number(Boolean(b.finding.outcome)));
  return {details, priority, displayed: priority.slice(0, 8)};
}

// Audit evidence must not silently open a different retry of the same revision.
export function auditReview(reviews, row) {
  if (!row.run_id) return undefined;
  return reviews.find(review => review.project === row.project && review.iid === row.iid
    && review.sha === row.sha && review.detail?.run?.run_id === row.run_id);
}

export const reviewStatus = status => ({success:'Review complete',no_findings:'No findings reported',failed:'Review failed',running:'Reviewing',queued:'Queued',reviewing:'Reviewing'})[status] || status || 'Status unknown';

export function reviewSummary(review) {
  const available = Array.isArray(review.detail?.findings);
  const findings = available ? review.detail.findings : [];
  const priority = workbench([review]).priority.map(row => row.finding);
  const verified = findings.filter(f => f.verified === 1 || f.verified === true).length;
  const refuted = findings.filter(f => f.verified === 0 || f.verified === false).length;
  const observed = findings.filter(f => f.outcome != null).length;
  const run = review.detail?.run;
  const duration = run?.finished_at && run?.started_at ? (Date.parse(run.finished_at)-Date.parse(run.started_at))/1000 : null;
  return { available, findings, priority, verified, refuted, unevaluated:findings.length-verified-refuted,
    observed, unobserved:findings.length-observed,
    priorityObserved:priority.filter(f => f.outcome != null).length,
    empty: ['running','reviewing','queued'].includes(review.status) ? 'No findings recorded yet' : 'No findings recorded',
    cost:run?.cost_recorded !== false && Number.isFinite(run?.cost_usd) && run.cost_usd >= 0 ? run.cost_usd : null,
    duration:Number.isFinite(duration) && duration >= 0 ? duration : null };
}
