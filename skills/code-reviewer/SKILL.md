---
name: code-reviewer
description: Review GitLab merge requests, pull requests, branches, diffs, or changed code using the llm-code-review meta prompt. Use when asked to perform code review, MR review, PR review, diff review, or changeset review.
---

# Code Reviewer

Use this skill for technical code review only.

## Required Prompt

Before reviewing, load the meta prompt from:

`${BUBO_PROMPT:-$BUBO_ROOT/prompts/00-meta.md}`

Treat that file as the review contract. Follow its output format, evidence standard, uncertainty rule, tone, and false-positive constraints.

If the file is missing or unreadable, stop and report that the review prompt is unavailable.

## Review Modes

- **Diff review:** Review only the provided diff, MR, PR, or changed files.
- **Full code review:** Review the requested repository or path when the user explicitly asks for a full review.

Default to diff review for merge requests and pull requests.

## GitLab

Use the GitLab token from the inherited environment:

- `GITLAB_TOKEN`
- `GITLAB_PERSONAL_ACCESS_TOKEN`
- `GLAB_TOKEN`

Do not print token values.

Prefer `glab` for GitLab metadata and API calls. Use `git` for local checkout and diff inspection.

## Output

Return only the findings allowed by the configured meta prompt.

If there are no findings, return the schema-version-1 JSON object with an empty `findings` array, and set `status` and `coverage` as the meta prompt requires.

## Read-only execution and output

Review only: do not edit files, run reviewed code/tests/builds/hooks/installers, or change external systems. Use authorized read-only inspection. Select diff or audit mode explicitly; change causality applies only in diff mode. Return the versioned JSON object from the meta contract, never a bare findings array or prose acknowledgement. Missing essential evidence produces incomplete coverage and a separate question.
