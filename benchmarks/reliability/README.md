# Review instruction format pilot

This diagnostic compares Markdown, XML and hybrid wrappers while holding canonical
instruction sentences and supplied excerpts constant. It contains six bug/control
pairs: two historical Bubo fixes and four synthetic language examples. This is not
a repository-separated benchmark or evidence of generalization to arbitrary code.

Run fixture checks first:

```sh
PYTHONPATH=src .venv/bin/python -m pytest benchmarks/reliability/test_pilot.py
```

Generate a randomized manifest without model calls:

```sh
PYTHONPATH=src .venv/bin/python benchmarks/reliability/run.py \
  --model MODEL_ID --out /tmp/bubo-format-plan --dry-run
```

Remove `--dry-run` only after authorizing source excerpts to the configured Codex
provider. Default execution is 72 calls (12 cases, three formats, two repeats),
with three workers and a 120-second timeout per call. Use a fresh output directory.
The manifest records the requested model, CLI version, seed and fixture digest;
records retain raw responses, timing, parsed outcomes and reported model where
available. Sources are supplied inline; the temporary checkout is intentionally
empty. No claim about whole-repository retrieval can be derived from this pilot.

Adjudicate the blinded records against the hidden oracles using concrete causal
reasoning. Preserve disagreements and rationales. Count transport/schema/mode
failures, incomplete coverage, omitted findings, bug detection and clean-control
false positives separately; never remove failures from the denominator. Agent
adjudication is not independent human adjudication. The lexical scorer in the
separate benchmark is not semantic adjudication. Small paired samples cannot
establish universal format superiority.

No live format comparison results are currently claimed. macOS filesystem
isolation was exercised independently; credential-free Linux Codex 0.154.0/Bubblewrap
filesystem nesting passed on Ubuntu 22.04. Authenticated Linux reviews and network
isolation are separate checks; this pilot does not establish either.
