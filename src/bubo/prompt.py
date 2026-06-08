"""Meta-prompt rendering: substitute config values into ``prompts/00-meta.md``.

The review meta prompt is a Markdown file that contains placeholder tokens
the runtime replaces with operator-configured values before handing it to
the agent. Currently there is exactly one placeholder
(:data:`MAX_FINDINGS_PLACEHOLDER`) but the module is structured so adding
more is a one-line change.

A rendered copy is cached on disk under ``var/rendered-prompts/`` keyed by
the substituted values, so the agent always reads from the same path and
the rendering work happens at most once per unique value combination.
"""

from __future__ import annotations

from pathlib import Path

from bubo.config_values import positive_int

# Token replaced with ``max_findings_per_merge_request`` at render time.
# Kept as a module constant so the meta prompt template and the renderer
# never disagree on the spelling.
MAX_FINDINGS_PLACEHOLDER = "{{MAX_FINDINGS_PER_REVIEW}}"


def render_meta_prompt(prompt_text: str, max_findings: int) -> str:
    """Substitute config values into the meta prompt and return the result.

    Pure string transformation — no IO. Useful for tests that want to
    verify substitution without touching the filesystem.

    Raises :class:`ConfigError` (via :func:`positive_int`) if
    ``max_findings`` is not a positive integer.
    """
    limit = positive_int(max_findings, "max_findings_per_merge_request")
    return prompt_text.replace(MAX_FINDINGS_PLACEHOLDER, str(limit))


def write_rendered_meta_prompt(prompt_file: Path, output_dir: Path, max_findings: int) -> Path:
    """Render ``prompt_file`` and write the result under ``output_dir``.

    The output filename embeds the rendered ``max_findings`` value so
    different caps coexist on disk without overwriting each other. The
    write is content-addressed: if a previously-rendered file already
    matches the new content exactly, the file is left untouched (preserves
    mtime and avoids spurious syscalls).

    Returns the absolute path of the rendered file. Creates ``output_dir``
    if it does not exist.
    """
    limit = positive_int(max_findings, "max_findings_per_merge_request")
    rendered = render_meta_prompt(prompt_file.read_text(encoding="utf-8"), limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{prompt_file.stem}.max-{limit}{prompt_file.suffix}"
    if not target.exists() or target.read_text(encoding="utf-8") != rendered:
        target.write_text(rendered, encoding="utf-8")
    return target
