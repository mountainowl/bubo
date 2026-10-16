# Review result contract and execution policy

Live review commands must emit exactly one schema-version-1 JSON object. Bare arrays, prose acknowledgements, error objects, duplicate keys, fences, and mixed transcripts are rejected. The offline `extract_findings` adapter and subscription health probe retain their separate compatibility contract; they cannot establish live review completion. Codex `exec` uses `--output-last-message` so tool transcripts are never parsed as results. Custom wrappers must emit the final object directly.

```json
{"schema_version":1,"mode":"diff","status":"complete","findings":[],"questions":[],"coverage":{"reviewed":["specified paths at pinned revision"],"unreviewed":[],"limitations":[]},"omitted_findings_count":0}
```

`complete` requires a nonempty reviewed scope and no unresolved questions, unreviewed scope, or limitations. `incomplete` retains supported findings and describes missing evidence. `failed` publishes no findings. Diff publishers reject audit results and stale heads. Findings require title, file, positive line, issue/suggestion type, blocking/non-blocking severity, numeric confidence from 0 to 1, category, impact, evidence, and fix. Questions are separate from defect confidence. Output limits preserve omission counts; no clean acknowledgement is emitted when candidates were merely filtered or omitted.

Incomplete worker runs use the existing failed/backoff state and preserve publication counts and the full result in their report. They never reconcile old comments or acknowledge a clean change. MCP clients receive explicit status and coverage. A verifier confirms or refutes only with a decisive evidence quorum; unavailable context, split votes, and exhausted budgets remain undecided and retryable. Only decisive votes for the exact revision/finding and unchanged verification policy are reused.

## Read-only execution

All reviewer, verifier, and reconciliation subprocesses enter a filesystem sandbox. macOS requires `sandbox-exec`; Linux requires `bubblewrap` (`bwrap`) and permitted namespaces. Unsupported or denied sandbox setup fails closed. Provision and verify the sandbox before upgrading service, container, or GitHub Action deployments; custom launchers do not bypass this requirement. No Linux kernel validation or live model authentication run is claimed here.

The source checkout and host filesystem are read-only to the child. A disposable home/cache/temp directory is writable. Selected provider authentication/config files are copied with private permissions so session writes do not modify the originals; refreshes are not persisted back, and subscription/provider authentication should be smoke-tested before rollout. SCM credentials are removed from the child environment. Explicitly configured custom-provider credentials remain allowed. MCP prepares a target checkout and passes its cwd instead of reviewing the server's directory.

This is a filesystem boundary, not a network firewall: model-provider network access remains enabled, and configured remote tools can have side effects outside the sandbox. The review instructions prohibit those operations, but strict external read-only deployments must additionally disable write-capable remote tools and constrain service credentials/network permissions. Do not claim XML delimiters or this filesystem sandbox enforce HTTP-level read-only semantics.

## Migration and evaluation

Deploy prompt and runtime versions together. Provider wrappers embed the trusted packaged contract; they no longer depend on ambient global skill discovery. Requirements and escaped XML metadata remain evidence, never execution authorization. Legacy custom reviewers must emit the new object; unsupported versions fail explicitly. Enterprise pass-through adapters can continue reading `findings` through the compatibility extractor, while transformed live output must preserve the envelope. The Marketplace Action installs the selected Bubo package: keep its pinned package/runtime compatible and pre-provision the OS sandbox before releasing this branch. No independent mirror repository is changed automatically.

See `benchmarks/README.md` for the held-out evaluation protocol. The default benchmark matcher now requires exact normalized summaries as well as location, so location-only coincidences receive no quality credit. Exact string matching is conservative, not a substitute for independent semantic adjudication.
