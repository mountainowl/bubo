// Descriptive counts only: overlapping periods and selected evaluations are
// not independent samples from which to estimate trends or accuracy.
export function reviewScope(metric, requested = null) {
  const scope = requested ?? (metric?.production?.runs === 0 && metric?.validation?.runs > 0 ? 'dry-run' : 'production');
  return {scope, runs: scope === 'dry-run' ? metric?.validation : metric?.production};
}

export function dashboardCharts(metric, requested = null) {
  if (!metric) return null;
  const {scope, runs:p} = reviewScope(metric, requested);
  const productionCount = metric.production?.runs;
  const f = metric.findings;
  const a = metric.audit_evidence;
  const valid = value => Number.isFinite(value) && value >= 0;
  if (!p || !f || !a || !metric.usage || !p.cost_usd
      || !p.successful_usage?.cost_usd || !p.failed_usage?.cost_usd) return null;
  const successfulCost = p.successful_usage.cost_usd;
  const failedCost = p.failed_usage.cost_usd;
  const inputs = [p.runs, p.completed, p.unfinished, p.successful, p.failed,
    p.cost_usd.total, p.cost_usd.recorded, p.cost_usd.missing,
    successfulCost.total, successfulCost.recorded, failedCost.total, failedCost.recorded,
    f.total, f.verified, f.refuted, metric.usage.dry_runs,
    a.production_runs, a.provenance_known, a.provenance_unknown];
  if (!inputs.every(valid)) return null;
  const otherFinished = p.completed - p.successful - p.failed;
  const otherRecorded = p.cost_usd.recorded - successfulCost.recorded - failedCost.recorded;
  const unevaluated = f.total - f.verified - f.refuted;
  const residual = p.cost_usd.total - successfulCost.total - failedCost.total;
  const epsilon = Number.EPSILON * Math.max(1, p.cost_usd.total,
    successfulCost.total, failedCost.total) * 8;
  if (otherFinished < 0 || otherRecorded < 0 || unevaluated < 0 || residual < -epsilon
      || p.completed + p.unfinished !== p.runs
      || p.cost_usd.recorded + p.cost_usd.missing !== p.runs
      || a.production_runs !== productionCount
      || a.provenance_known + a.provenance_unknown !== productionCount
      || typeof a.available !== 'boolean'
      || (a.available && (![a.policy_recorded,a.policy_missing].every(valid)
        || a.policy_recorded + a.policy_missing !== productionCount))) return null;
  return {
    delivery: [
      {key:'successful', label:'Successful', value:p.successful},
      {key:'failed', label:'Failed', value:p.failed},
      {key:'unfinished', label:'Unfinished', value:p.unfinished},
      {key:'other', label:'Other finished', value:otherFinished},
    ],
    spend: [
      {key:'successful', label:'Successful runs', value:successfulCost.total, recorded:successfulCost.recorded},
      {key:'failed', label:'Failed runs', value:failedCost.total, recorded:failedCost.recorded},
      {key:'other', label:'Other runs', value:Math.abs(residual) <= epsilon ? 0 : residual, recorded:otherRecorded},
    ],
    evaluation: [
      {key:'verified', label:'Verified', value:f.verified},
      {key:'refuted', label:'Refuted', value:f.refuted},
      {key:'unknown', label:'Not evaluated', value:unevaluated},
    ],
    audit: {
      available:a.available, denominator:a.production_runs,
      policyRecorded:a.available ? a.policy_recorded : null,
      policyMissing:a.available ? a.policy_missing : null,
      provenanceKnown:a.provenance_known, provenanceUnknown:a.provenance_unknown,
    },
    costRecorded:p.cost_usd.recorded, costMissing:p.cost_usd.missing,
    scope, runCount:p.runs, productionRuns:productionCount, dryRuns:metric.usage.dry_runs, findingCount:f.total,
  };
}
