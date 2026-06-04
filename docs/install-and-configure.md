# Install and configure

Before you start, make sure the runtime and provider tools listed in
[prerequisites.md](prerequisites.md) are on `PATH` and the bot/LLM
credentials are in hand.

## Install

Clone the repo and install in place. The install needs the bundled
prompt, skill, config template, wrapper scripts, and deployment
templates that ship with the checkout — pip-only installs are not
supported.

```sh
git clone https://github.com/mountainowl/ai-code-review.git
cd ai-code-review
./scripts/install-package.sh
```

For a remote host:

```sh
./scripts/deploy-package.sh user@host
# or
./scripts/deploy-package.sh user@host --root /opt/llm-reviewer --sudo
```

For local development:

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
