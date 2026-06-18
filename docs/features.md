# Features

Bubo reviews your merge and pull requests with the AI model **you** choose, and
leaves behind only the comments worth reading. Here's what you get — in plain
terms. Curious how it all fits together? See [How it works](how-it-works.md). Ready
to try it? Jump to the [Recipes](recipes.md).

## Reviews worth reading

- **Only the comments that matter.** No praise, no summaries, no chatbot chatter —
  just the issues worth an author's time. Every comment says *what's wrong*, *why
  it matters*, and *how to fix it*.
- **A clear "all good" when nothing's wrong.** Bubo leaves one short note on a clean
  change, so "reviewed and happy" looks different from "nobody checked it."
- **Try it safely first.** A built-in preview mode works out every comment but posts
  nothing, so you can see a real review before it goes live. [Get started →](recipes.md)
- **Pick the tone.** Terse, collaborative, socratic, formal, or casual — same
  findings, friendlier words, your choice.
  [Choose a tone →](configuration.md#review-comment-tone-moods)

## Use the AI you already trust

- **Bring your own model.** Codex, Claude, or any model your command line can run —
  no lock-in to a single vendor. [What you need →](prerequisites.md)
- **Runs on your own machines.** Your code, your diffs, and your review history never
  leave your infrastructure. [Install and configure →](install-and-configure.md)
- **Works with GitLab and GitHub.** Merge requests and pull requests, the same simple
  setup for both. [Copy-paste recipes →](recipes.md)

## Decide what gets flagged

- **Strict gate, or full collaboration.** Show only the problems that should block a
  merge, or everything — including suggestions and open questions.
  [Pick a mode →](configuration.md#surface-mode-gate-vs-collaborate)
- **Set the confidence bar.** Post only findings the reviewer is sure about, and
  raise the bar even higher for the kinds of issues you care about most.
  [Tune confidence →](configuration.md#calibrated-per-class-confidence)
- **Learns your team's taste.** When your team keeps brushing off a certain kind of
  comment, Bubo quietly stops raising it.
  [How it learns →](configuration.md#dispute-driven-suppression)
- **Gets sharper over time.** Bubo looks at how your team reacted to past comments and
  tunes itself automatically — no manual fiddling.
  [Auto-tuning →](configuration.md#calibrated-per-class-confidence)
- **Double-checks before posting.** An optional second look — even from a different
  model — drops findings that don't hold up.
  [Turn on verification →](configuration.md#verification-before-posting)

## Fits how your team already works

- **Reviews on demand.** Ask for a review of a specific request straight from your
  editor or other tools. [Use the MCP server →](mcp.md)
- **Reviews in your pipeline.** A ready-made GitHub Action comments on pull requests
  right in CI. [Add it to CI →](github-action.md)
- **A simple dashboard.** A read-only page of recent reviews, health, and reports —
  nothing to click wrong. [Operate Bubo →](operate.md)

## Built for teams that answer to someone

- **Knows when AI wrote the code.** Bubo can spot AI-assisted or sensitive changes and
  review them more carefully — it advises, it never blocks the merge.
  [Governance →](configuration.md#governance-provenance)
- **A report you can hand to an auditor.** Acceptance rate, value delivered, noise
  trends, and every decision made — all kept on your own servers.
  [Reports & grading →](operate.md)
- **Keeps your secrets safe.** Tokens and credentials are scrubbed out and never
  handed to the AI. [Security policy →](https://github.com/mountainowl/bubo/blob/main/SECURITY.md)
- **See what it's doing.** Standard metrics and per-review cost tracking you can drop
  onto a dashboard. [Metrics & telemetry →](telemetry.md)
- **Releases you can trust.** Every release is signed and ships with a list of exactly
  what's inside. [Releases →](https://github.com/mountainowl/bubo/releases)

---

**New here?** Start with the [Recipes](recipes.md) or
[Install and configure](install-and-configure.md). Want the mechanics behind all of
this? Read [How it works](how-it-works.md).
