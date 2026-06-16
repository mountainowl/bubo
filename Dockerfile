# syntax=docker/dockerfile:1
#
# Container image for bubo. Multi-stage: build the wheel (hatchling, with the
# force-included `bubo init` assets) in a throwaway builder, then install ONLY
# the wheel into a slim runtime so no build toolchain ships in the final image.
#
# NOTE on the reviewer agent: the review LLM CLI (codex / claude / your own) is
# intentionally NOT bundled — it is BYO and often large. Operators provide it by
# deriving from this image (`FROM ghcr.io/mountainowl/bubo` + install the agent)
# or by mounting it in. bubo's own CLIs (`bubo`, `bubo-poller`, `bubo-mcp`) and
# all `bubo init` assets ARE installed here. See docs/operate.md.

# ---- builder: produce the wheel ------------------------------------------------
FROM python:3.14-slim AS builder
WORKDIR /src
COPY . .
# Build isolation pulls hatchling itself; we only need the `build` frontend.
RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist

# ---- runtime: bubo + git on a slim base ---------------------------------------
FROM python:3.14-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/mountainowl/bubo" \
      org.opencontainers.image.description="Bubo — agentic AI code review for GitLab MRs and GitHub PRs, BYO-LLM." \
      org.opencontainers.image.licenses="MIT"

# git: bubo checks out each change before reviewing it.
# ca-certificates: TLS to the SCM + LLM APIs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install bubo from the wheel built above — no compiler / build deps in runtime.
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 bubo
USER bubo
WORKDIR /home/bubo

# Default to the poller; override for other entrypoints, e.g.:
#   docker run --rm ghcr.io/mountainowl/bubo bubo init
#   docker run --rm ghcr.io/mountainowl/bubo bubo report
#   docker run --rm -p 8765:8765 ghcr.io/mountainowl/bubo bubo-mcp
CMD ["bubo-poller"]
