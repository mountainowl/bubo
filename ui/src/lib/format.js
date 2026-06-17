// Tiny presentation helpers. No business logic — bubo is the source of truth;
// these only format values the snapshot already computed.

export function shortSha(sha) {
  return sha ? String(sha).slice(0, 8) : "";
}

export function usd(n) {
  if (n == null) return "$0.00";
  return `$${Number(n).toFixed(2)}`;
}

export function num(n) {
  return Number(n ?? 0).toLocaleString();
}

export function pct(rate) {
  if (rate == null) return "—";
  return `${(Number(rate) * 100).toFixed(1)}%`;
}

export function secs(s) {
  if (s == null) return "—";
  const v = Number(s);
  if (v < 60) return `${v.toFixed(1)}s`;
  const m = Math.floor(v / 60);
  return `${m}m ${Math.round(v % 60)}s`;
}

export function ago(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const delta = (Date.now() - then) / 1000;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

// Map a review/run status to a token-backed pill class group.
export function statusTone(status) {
  switch (status) {
    case "success":
    case "no_findings":
      return "ok";
    case "failed":
      return "danger";
    case "running":
    case "queued":
      return "info";
    default:
      return "muted";
  }
}

// Derive the dominant outcome label for a finding's embedded outcome row.
export function outcomeLabel(outcome) {
  if (!outcome) return "no outcome yet";
  if (outcome.false_positive) return "false positive";
  if (outcome.disputed) return "disputed";
  if (outcome.duplicate) return "duplicate";
  if (outcome.resolved) return "resolved";
  if (outcome.merged_unresolved) return "merged unresolved";
  if (outcome.developer_replied) return "replied";
  return "open";
}

export function outcomeTone(outcome) {
  if (!outcome) return "muted";
  if (outcome.false_positive || outcome.disputed) return "danger";
  if (outcome.resolved) return "ok";
  if (outcome.duplicate || outcome.merged_unresolved) return "warn";
  return "info";
}
