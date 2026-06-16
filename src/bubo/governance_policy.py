"""Pure governance-policy gate (governance Rec ②b, advisory only).

IO-free, mirroring :mod:`bubo.provenance`: the poller computes a
:class:`~bubo.provenance.ProvenanceSignal`, the DB layer persists the decision,
and everything here is a deterministic function — so it is unit-testable in
isolation. This module imports only :class:`ProvenanceSignal` and stdlib.

Honesty contract (see ``docs/configuration.md`` → "Governance & provenance"):

* **A band + an advisory action, never a verdict.** A decision pairs the
  provenance *band* (echoed from the signal) with an action that is at most a
  *flag* — :data:`ACTION_FLAG` vs :data:`ACTION_CLEAR`. bubo never asserts that
  a change is good or bad; the band and the escalation predicate are the whole
  story.
* **Advisory, never blocking.** bubo cannot block a merge. A flag injects
  heightened-scrutiny context into the review prompt at most; it is the
  human reviewer (or the SCM's own gates) that decides anything.
* **One shared predicate.** :func:`is_escalated` is the single definition of
  "escalated" so the rigor-modulation path (which injects the directive) and
  the policy gate (which sets the action) can never disagree about whether a
  change escalated.
* **Evaluated ≠ flagged.** :func:`evaluate_policy` returns a decision even when
  nothing fired — "evaluated, escalation predicate did not match" is itself an
  audit artifact. ``mode`` is echoed, not enforced here: the caller decides
  whether to persist or act based on the mode.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from bubo.provenance import ProvenanceSignal

# Policy modes (operator chooses via ``[governance].policy_mode``). Echoed into
# the decision for the audit trail; this module does not act on the mode.
POLICY_OFF = "off"
POLICY_REPORT_ONLY = "report-only"
POLICY_SOFT = "soft"

# Decision actions — advisory only; bubo never blocks a merge.
ACTION_CLEAR = "clear"  # evaluated; escalation predicate did not match
ACTION_FLAG = "flag"  # escalation matched; advisory flag only

# The context a caller MAY inject into the review prompt when a change is
# flagged. It is heightened-scrutiny guidance, explicitly not a verdict.
HEIGHTENED_SCRUTINY_DIRECTIVE = (
    "GOVERNANCE NOTICE: this change is AI-assisted or collaborative and touches "
    "sensitive paths. Apply heightened scrutiny and prioritize the security lens "
    "(authentication/authorization, secret handling, input validation, injection). "
    "This is advisory context, not a verdict — review the diff on its own merits."
)


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    """An auditable, advisory governance decision for one change.

    Carries a provenance *band* plus an advisory *action* (at most a flag) —
    never a verdict, since bubo cannot block a merge. The fields together form
    the "why" of the decision for the audit trail.
    """

    mode: str  # off | report-only | soft (echoed; not enforced here)
    action: str  # clear | flag (advisory only)
    triggered: bool  # escalation predicate matched
    matched_rule: str  # audit "why": "band+sensitive" | "band" | ""
    band: str  # echoed from the provenance signal
    sensitive_paths: list[str]
    reason: str  # human-readable audit sentence
    rigor_injected: bool = False  # did the caller inject the directive?


def is_escalated(
    signal: ProvenanceSignal,
    *,
    escalate_bands: Iterable[str],
    require_sensitive: bool,
) -> bool:
    """Return ``True`` when the change escalates under the predicate.

    Escalated iff the signal's band is in ``escalate_bands`` AND (a sensitive
    path matched OR ``require_sensitive`` is ``False``). This is the SINGLE
    shared definition of "escalated" so rigor modulation and the policy gate
    always agree.
    """
    if signal.band not in escalate_bands:
        return False
    return bool(signal.sensitive_paths) or not require_sensitive


def heightened_scrutiny_directive() -> str:
    """Return :data:`HEIGHTENED_SCRUTINY_DIRECTIVE`.

    Indirection so future per-band directive variants can be selected here
    without changing callers.
    """
    return HEIGHTENED_SCRUTINY_DIRECTIVE


def evaluate_policy(
    signal: ProvenanceSignal,
    *,
    mode: str,
    escalate_bands: Iterable[str],
    require_sensitive: bool,
    rigor_injected: bool = False,
) -> GovernanceDecision:
    """Evaluate provenance against the escalation predicate into a decision.

    ``triggered`` comes from :func:`is_escalated` (the shared predicate), and
    ``action`` is :data:`ACTION_FLAG` when triggered else :data:`ACTION_CLEAR`
    — an advisory flag, never a verdict, and never a merge block. ``mode`` is
    echoed unchanged; it is NOT enforced here (a decision is returned even for
    :data:`POLICY_OFF`, and the caller decides whether to persist or act on it).

    ``matched_rule`` records the audit "why" from the *signal contents*:
    ``"band+sensitive"`` when triggered with a sensitive-path match,
    ``"band"`` when triggered on band alone, ``""`` when nothing fired.
    ``band``/``sensitive_paths`` are echoed from the signal and
    ``rigor_injected`` is passed through.
    """
    triggered = is_escalated(
        signal, escalate_bands=escalate_bands, require_sensitive=require_sensitive
    )
    action = ACTION_FLAG if triggered else ACTION_CLEAR
    if triggered and signal.sensitive_paths:
        matched_rule = "band+sensitive"
        reason = (
            f"escalated: band {signal.band!r} in escalate set and "
            f"{len(signal.sensitive_paths)} sensitive path(s) matched (advisory flag)"
        )
    elif triggered:
        matched_rule = "band"
        reason = (
            f"escalated: band {signal.band!r} in escalate set (advisory flag)"
        )
    else:
        matched_rule = ""
        reason = (
            f"evaluated: band {signal.band!r} did not match escalation predicate"
        )
    return GovernanceDecision(
        mode=mode,
        action=action,
        triggered=triggered,
        matched_rule=matched_rule,
        band=signal.band,
        sensitive_paths=list(signal.sensitive_paths),
        reason=reason,
        rigor_injected=rigor_injected,
    )


__all__ = [
    "ACTION_CLEAR",
    "ACTION_FLAG",
    "HEIGHTENED_SCRUTINY_DIRECTIVE",
    "POLICY_OFF",
    "POLICY_REPORT_ONLY",
    "POLICY_SOFT",
    "GovernanceDecision",
    "evaluate_policy",
    "heightened_scrutiny_directive",
    "is_escalated",
]
