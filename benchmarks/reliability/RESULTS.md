# Bounded review-format pilot — 2026-10-16

The approved pilot completed 71 of 72 review attempts; one XML/SQL-control attempt timed out
at 120 seconds. It remains a failed run in the denominator and was not retried.
This diagnostic provides no evidence for changing the current instruction format.

| Format | Completed attempts | Defect runs detected | Clean controls completed without findings | Supported findings |
|---|---:|---:|---:|---:|
| markdown | 24/24 | 14/14 | 10/10 | 16 |
| xml | 23/24 | 14/14 | 9/10 | 16 |
| hybrid | 24/24 | 14/14 | 10/10 | 16 |

These denominators reflect the corrected ground truth described below. There were
48 supported findings, zero adjudicated unsupported findings, and no established
misses in completed runs. One finding's wording overstated when the connection
fails; the supported mechanism is a write-requiring journal transition, not every
SQLite connection. The timeout receives no clean-control credit.

## Protocol and evidence

- Codex CLI 0.154.0, requested/reported model `gpt-6-astra`; three workers,
  120-second attempt timeout, seed 245, two repeats per format and case.
- Twelve supplied-excerpt cases across six families: two historical Bubo families
  and four synthetic language families. Markdown/XML/hybrid preserve canonical
  instructions, order and evidence; wrapper/escaping differs.
- Two agents independently adjudicated all 72 format-blinded records; a devil's
  advocate reconciled them. Categorical decisions agreed on all 72. This is agent
  adjudication, not independent human review.
- Fixture SHA256: `40e9b6d3d3307c3ed3cbc547c5e86badc727e02b624e9d0d119dba8793a143f6`.
- Runtime fixes and tests are captured by commit `db63470`. The first attempted
  run failed before model execution; the pilot launched 72 CLI invocations. Underlying provider requests or retries
  were not measured.
  Raw responses, diagnostic transcripts, manifest, blinded packets, peer decisions,
  adjudication rationales and the independent reproduction are retained locally.

## Corrected control and limitations

The historical snapshot-copy variant originally labeled `correct` is defective
under its supplied contract: `copy2` preserves mode `0444`, so the purported writable
copy can fail a SQLite WAL-mode transition for a nonprivileged process. A local
SQLite reproduction confirmed that mechanism with the source unchanged. All six
responses for this variant identified it. Original inputs and labels are retained
for audit, but that variant must not be used as a clean control in future scoring.
The adjudicated set therefore has seven defective cases and five clean cases.

This is a small, repeated-case, within-project/synthetic diagnostic. It is not an
independent repository holdout, a before/after baseline, a full-repository retrieval
exercise or a statistical claim about arbitrary code. Do not derive universal
format superiority, population precision/recall, or release certification from it.
The timeout is not evidence that XML caused a failure. Broader held-out evaluation
and deployment-specific authenticated runtime checks remain release considerations.

No unrelated exporter change was made from the historical finding. Keep the current
trusted-instruction format; use the explicit completion contract and fail-closed
gates independently of these small-sample quality results.
