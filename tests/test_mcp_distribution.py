"""Distribution guard rails for the MCP server runtime dependency."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _project() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_distribution_supports_production_python() -> None:
    """The release artifact must install on the current deployment interpreter."""
    assert _project()["requires-python"] == ">=3.12"


def test_mcp_dependency_excludes_incompatible_major_version() -> None:
    """The FastMCP import used by Bubo is provided by MCP 1.x, not 2.x."""
    dependencies = _project()["dependencies"]

    assert "mcp>=1.2.0,<2" in dependencies


def test_wheel_installs_and_starts_mcp_on_production_python(tmp_path: Path) -> None:
    """Catch resolver drift before a Python 3.12 production deployment."""
    uv = shutil.which("uv")
    python = os.environ.get("BUBO_DEPLOY_PYTHON") or shutil.which("python3.12")
    if uv is None or python is None:
        pytest.skip("requires uv and a Python 3.12 interpreter")

    dist = tmp_path / "dist"
    venv = tmp_path / "venv"
    _run([uv, "build", "--wheel", "--out-dir", str(dist)], cwd=ROOT)
    wheel = next(dist.glob("bubo-*.whl"))
    _run([uv, "venv", "--python", python, str(venv)])
    venv_python = venv / "bin" / "python"
    _run([uv, "pip", "install", "--python", str(venv_python), str(wheel)])

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = {
        **os.environ,
        "BUBO_MCP_TRANSPORT": "http",
        "BUBO_MCP_HOST": "127.0.0.1",
        "BUBO_MCP_PORT": str(port),
        "BUBO_MCP_BEARER_TOKEN": "test-token",
    }
    server = subprocess.Popen(
        [str(venv / "bin" / "bubo-mcp")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            _, stderr = server.communicate(timeout=2)
            pytest.fail(f"bubo-mcp did not start: {stderr[-500:]}")
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=5)


def _run(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
