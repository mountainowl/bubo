"""Source-control provider abstraction.

The poller is provider-agnostic: it sequences the review pipeline against
an :class:`~bubo.scm.base.ScmProvider` without knowing whether the
backend is GitLab or GitHub. :func:`get_provider` resolves the concrete
implementation from ``cfg.provider``.

Adding a provider is: implement the :class:`ScmProvider` protocol in a new
module and register it in :func:`get_provider`. Nothing in the poller
changes.
"""

from __future__ import annotations

from bubo.errors import describe
from bubo.review_config import ReviewConfig
from bubo.scm.base import ScmProvider


def get_provider(cfg: ReviewConfig) -> ScmProvider:
    """Return the provider implementation for ``cfg.provider``.

    Raises :class:`ValueError` for an unknown provider — though
    :func:`bubo.review_config.review_config_from_dict` already
    validates the value, so this is a defense-in-depth guard.
    """
    if cfg.provider == "gitlab":
        from bubo.scm.gitlab import GitLabProvider

        return GitLabProvider()
    if cfg.provider == "github":
        from bubo.scm.github import GitHubProvider

        return GitHubProvider()
    raise ValueError(
        describe(
            f"unknown provider: {cfg.provider!r}",
            reason="the provider is not registered",
            fix="set [scm].provider to 'gitlab' or 'github'.",
        )
    )


__all__ = ["ScmProvider", "get_provider"]
