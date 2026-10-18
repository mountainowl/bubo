"""Bubo's entry points must run without any OTLP exporter installed.

Telemetry is exported through the in-process PostHog bridge
(``bubo.telemetry.posthog``); nothing imports an OTLP exporter, so the
gRPC/HTTP exporter packages (and grpcio/protobuf under them) are not runtime
dependencies. This runs each entry point in a fresh interpreter so a stray
top-level import would be caught.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_MODULES = ("bubo.cli", "bubo.poller", "bubo.mcp_server", "bubo.report", "bubo.ui_export")


def test_entry_points_import_no_otlp_exporter_or_grpc() -> None:
    probe = (
        "import importlib, sys\n"
        f"for name in {ENTRY_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "bad = sorted(m for m in sys.modules\n"
        "             if m.startswith(('opentelemetry.exporter', 'grpc', 'google.protobuf')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert out.stdout.strip() == ""


def test_no_otlp_exporter_runtime_dependency() -> None:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert not [dep for dep in deps if "exporter-otlp" in dep or dep.startswith("grpcio")]
