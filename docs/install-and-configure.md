# Install and configure

Before you start, make sure the runtime and provider tools listed in
[prerequisites.md](prerequisites.md) are on `PATH` and the bot/LLM
credentials are in hand.

## Install

The install needs the bundled prompt, skill, config template, wrapper
scripts, and deployment templates that ship with the checkout.
**`pip install` is not a supported deploy path** — the
`llm_reviewer-X.Y.Z-py3-none-any.whl` and `llm_reviewer-X.Y.Z.tar.gz`
artifacts attached to each GitHub Release carry only the Python package
(no `config/`, `scripts/`, `deploy/`, `bin/`, `prompts/`, or
`skills/`). Use one of the two deploy artifacts below instead.

### Option 1 — deploy bundle from the GitHub Release (recommended)

Every release attaches an `llm-reviewer-deploy-X.Y.Z.tar.gz` bundle
containing the full deployable tree (cosign-signed, with `.pem` + `.sig`
sidecars).

```sh
version=0.5.1
curl -LO "https://github.com/mountainowl/ai-code-review/releases/download/v${version}/llm-reviewer-deploy-${version}.tar.gz"
tar -xzf "llm-reviewer-deploy-${version}.tar.gz"
cd "llm-reviewer-${version}"
./scripts/install-package.sh
```

Pass `--install-agent-config` on the first install to write the Codex
profile and Claude settings, then drop the flag on subsequent upgrades
to avoid clobbering local agent-config tweaks.

### Option 2 — clone the repo

```sh
git clone https://github.com/mountainowl/ai-code-review.git
cd ai-code-review
git checkout v0.5.1                    # or whatever the latest tag is
./scripts/install-package.sh --install-agent-config
```

### Remote host

```sh
./scripts/deploy-package.sh user@host
# or
./scripts/deploy-package.sh user@host --root /opt/llm-reviewer --sudo --install-agent-config
```

### Local development

```sh
uv sync --dev
uv run pytest
```

## Configure

Copy the example config and edit it locally — `config/env.toml` is
gitignored and holds your tokens:

```sh
cp config/env.example.toml config/env.toml
```

Minimum changes to get a first review running:

```toml
[gitlab]
token = "glpat-..."          # api scope

[agents]
llm_api_key = "..."          # your LLM provider key
llm_model = "gpt-5.5"        # match what your CLI is configured for

[[projects]]
path = "your-group/your-repo"
enabled = true
```

Keep `[review].dry_run = true` (the default) until your first real
review output looks right — the poller will plan findings without
posting comments. The full set of knobs and defaults lives in the
[configuration reference](configuration.md).

## GitLab bot setup

1. Create a bot user, for example `llm-reviewer`.
2. Add it to every target GitLab project with permission to read MRs and
   create discussions.
3. Create a token with `api` scope.
4. Put the token in ignored `config/env.toml` under `[gitlab].token`.
5. List the projects under `[[projects]]` in the same file.

## GitHub bot setup

1. Create a bot user (or use a machine account) and add it as a
   collaborator with pull-request read+write on every target repository.
2. Generate a personal access token with pull-request read+write scope.
3. Put the token in ignored `config/env.toml` under `[github].token`.
4. Set `[scm].provider = "github"` (or run `bin/gh-review-poller`, which
   forces it) and list the projects under `[[projects]]`.
