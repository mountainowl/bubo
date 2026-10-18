// Audit is a bounded sample; never derive aggregate metrics from these rows.
export function reportSelection(data, label) {
  const audit = data.reports?.find(w => w.label === label)?.report?.audit ?? [];
  const seen = new Set();
  const unique = audit.filter(row => {
    if (!row.run_id) return true;
    if (seen.has(row.run_id)) return false;
    seen.add(row.run_id);
    return true;
  });
  return {
    metrics: data.dashboard?.metrics?.windows?.find(w => w.label === label) ?? null,
    audit: unique.slice(0, 50),
  };
}

export { auditReview } from './workbench.js';

export function filterAudit(rows, query) {
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  return rows.filter(row => {
    const value = [row.run_id, row.project, row.iid, row.sha, row.model,
      row.status, row.policy_action, row.provenance_band, row.dry_run ? 'dry run' : 'production']
      .filter(v => v != null).join(' ').toLowerCase();
    return terms.every(term => value.includes(term));
  });
}

const columns = ['run_id', 'project', 'iid', 'sha', 'started_at', 'finished_at',
  'status', 'model', 'review_mode', 'dry_run', 'provenance_band', 'provenance_source',
  'provenance_confidence', 'sensitive_paths_count', 'cost_usd', 'cost_recorded',
  'policy_action', 'policy_mode'];

export function auditCsv(rows) {
  const cell = value => {
    let text = value == null ? '' : String(value);
    // CSV quoting alone does not stop spreadsheet formulas, including whitespace prefixes.
    if (/^[\s\u0000-\u001f]*[=+@-]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  return [columns, ...rows.map(row => columns.map(key =>
    key === 'cost_usd' && row.cost_recorded !== true ? null : row[key]))]
    .map(row => row.map(cell).join(',')).join('\r\n');
}
