# References & further reading

The sources below shaped Bubo's design, packaging, supply-chain posture,
and the way the project presents itself. They're grouped by topic, and
each entry notes *why* it's here — what we actually took from it — rather
than being a bare link dump. Where a source is a peer-reviewed/academic
preprint vs. vendor documentation vs. engineering writing, that's called
out so you can weight it accordingly.

## Research & academic

- **Launch-Day Diffusion: Tracking Hacker News Impact on GitHub Stars
  for AI Tools** — arXiv preprint [2511.04453](https://arxiv.org/abs/2511.04453).
  An empirical study (n≈138 AI/LLM tool launches, 2024–2025) measuring
  how a Hacker News appearance translates into GitHub stars, and how
  *posting time* affects the outcome. We used it to reason about launch
  timing. Caveat worth keeping in mind: the **direction** of the timing
  effect (optimal windows produce materially more stars) is well
  supported, but the paper's specific absolute star-count figures are a
  single unreplicated preprint — treat them as indicative, not precise.

## How GitHub discovery actually works

- **GitHub Engineering — "Topics" (the `repo-topix` suggestion engine)**:
  [github.blog/engineering/user-experience/topics](https://github.blog/engineering/user-experience/topics/).
  Describes how GitHub reads repo name + description + README and uses
  **tf-idf** to score candidate topics, meaning rare/distinctive terms
  carry more signal than generic ones. This is why Bubo's topics lean
  toward specific terms (`mcp-server`, `gitlab-ci`, `opentelemetry`)
  over generic ones.
- **GitHub Docs — Searching for repositories**:
  [docs.github.com/.../searching-for-repositories](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories).
  The load-bearing fact for repo discoverability: *"only the repository
  name, description, and topics are searched"* by default — README text
  is **not** indexed unless a searcher adds `in:readme`.
- **GitHub Docs — Classifying your repository with topics**:
  [docs.github.com/.../classifying-your-repository-with-topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).
  20-topic cap; each topic page is itself a browsable discovery surface.
- **GitHub Docs — Social media preview**:
  [docs.github.com/.../customizing-your-repositorys-social-media-preview](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).
  Recommended 1280×640 preview image; until set, link unfurls show a
  generic card.

## Python packaging & distribution

- **uv — Tools guide**:
  [docs.astral.sh/uv/guides/tools](https://docs.astral.sh/uv/guides/tools/).
  `uv tool install git+https://…` is Bubo's primary install path; this
  documents the Git/tag/subdirectory syntax and PATH placement.
- **uv — Build backend concepts**:
  [docs.astral.sh/uv/concepts/build-backend](https://docs.astral.sh/uv/concepts/build-backend/).
  Why the default backend ships only the module root — the root cause of
  an early release that dropped the deploy assets.
- **astral-sh/uv #11502** (uv maintainer *konstin*'s recommendation):
  [github.com/astral-sh/uv/issues/11502](https://github.com/astral-sh/uv/issues/11502).
  The basis for Bubo switching its build backend to **Hatchling** for
  flexible non-Python asset packaging.
- **Hatch / Hatchling build configuration**:
  [hatch.pypa.io/latest/config/build](https://hatch.pypa.io/latest/config/build/).
  The `force-include` mechanism that places Bubo's prompts, skills,
  plugins, and deploy templates into the wheel under `bubo/_assets/`.

## Standards & specifications Bubo implements

- **Model Context Protocol (MCP)** — [modelcontextprotocol.io](https://modelcontextprotocol.io/).
  Bubo ships an MCP server (`bubo-mcp`) and consumes upstream MCP servers.
- **Conventional Commits 1.0** — [conventionalcommits.org](https://www.conventionalcommits.org/en/v1.0.0/).
  Enforced on every commit; drives the automated release flow.
- **Semantic Versioning 2.0** — [semver.org](https://semver.org/), with the
  pre-1.0 "anything-goes under 0.x" carve-out.
- **Keep a Changelog 1.1** — [keepachangelog.com](https://keepachangelog.com/en/1.1.0/).
  The format of this project's hand-curated `CHANGELOG.md`.
- **OpenTelemetry** — [opentelemetry.io](https://opentelemetry.io/).
  Bubo's metrics, spans, and cost-attribution backend.

## Supply-chain security

- **OpenSSF Scorecard** — [github.com/ossf/scorecard](https://github.com/ossf/scorecard).
  The checks Bubo hardens against (SHA-pinned Actions, branch protection,
  signed releases, …).
- **Sigstore** — [sigstore.dev](https://www.sigstore.dev/).
  Keyless OIDC signing; Bubo's release artifacts are cosign-signed.
- **SLSA** — [slsa.dev](https://slsa.dev/). Supply-chain integrity levels
  that frame the release provenance goals.
- **SPDX** — [spdx.dev](https://spdx.dev/). The SBOM format attached to
  every Bubo release.

## Ecosystem & directories

- **punkpeye/awesome-mcp-servers** — [github.com/punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers).
  The high-traffic, low-bar directory of MCP servers — a discovery
  channel Bubo qualifies for via `bubo-mcp`.

## Prior art evaluated

- **Serena** — [github.com/oraios/serena](https://github.com/oraios/serena).
  An MCP-based coding agent toolkit reviewed while scoping Bubo's MCP
  surface and token-efficiency approach.
