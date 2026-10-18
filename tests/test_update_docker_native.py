"""Opt-in acceptance against an isolated daemon and local fixture registry."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not os.environ.get("BUBO_NATIVE_DOCKER_SOCKET"),
    reason="Requires an explicitly selected isolated Linux Docker daemon",
)


@pytest.mark.parametrize("fail_migration", [False, True, "process_death"])
def test_real_container_update_and_matched_recovery(tmp_path, monkeypatch, fail_migration):
    from bubo import update_docker
    from bubo.analytics_config import AnalyticsConfig
    from bubo.update_docker import DockerAPI, resolve_release
    from bubo.update_docker_installer import DockerInstaller
    from bubo.update_docker_runtime import DockerResidents
    from bubo.update_docker_setup import configure
    from bubo.updater import Release, UpdateEngine

    # Use real digest verification against a loopback fixture registry. Production
    # keeps its upstream-only allowlist; this override exists only in this test.
    image = os.environ["BUBO_NATIVE_DOCKER_IMAGE"]
    assert image.startswith("127.0.0.1:")
    monkeypatch.setattr(update_docker, "IMAGE", image)
    monkeypatch.setattr(
        update_docker, "_DIGEST", re.compile(re.escape(image) + r"@sha256:[0-9a-f]{64}\Z")
    )
    socket = Path(os.environ["BUBO_NATIVE_DOCKER_SOCKET"])
    api = DockerAPI(socket)
    root = tmp_path / "root"
    state = root / "var" / "state"
    state.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "env.toml").write_text("auto_update=false\n[analytics]\nenabled=false\n")
    identity = str(uuid.uuid4())
    (state / "install_id").write_text(identity)
    with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE native_preserved (value TEXT)")
        connection.execute("INSERT INTO native_preserved VALUES ('fixture data')")
    assert os.getuid() == 10001, "Run acceptance as the shipping image runtime UID"
    name = "bubo-native-" + uuid.uuid4().hex
    identifier = api.request(
        "POST",
        "/containers/create",
        {
            "Image": image + ":0.27.0",
            "User": "10001",
            "Cmd": ["bubo-poller", "service", "start", "--foreground"],
            "Env": ["BUBO_ROOT=/data/bubo"],
            "Labels": {"io.bubo.native.test": name},
            "HostConfig": {
                "Binds": [str(root) + ":/data/bubo:rw"],
                "NetworkMode": "none",
                "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            },
        },
        query={"name": name},
    )["Id"]
    try:
        api.request("POST", "/containers/" + identifier + "/start")
        deadline = time.monotonic() + 20
        while not (state / "bubo-service.pid").exists():
            assert api.inspect_container(identifier)["State"]["Running"]
            assert time.monotonic() < deadline, "Source service did not initialize"
            time.sleep(0.1)
        changes = api.request("GET", "/containers/" + identifier + "/changes")
        assert not changes, changes
        record = configure(
            root,
            identifier,
            socket,
            home=tmp_path / "home",
            runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
        )
        installation = root / ".updates" / "installation"
        assert record["ready"]
        assert record["source_version"] == "0.27.0"
        adapter = DockerInstaller(api, state, identifier)
        release = resolve_release(
            api,
            Release("0.28.0", "fixture", "0" * 64),
            architecture=api.inspect_image(image + ":0.28.0")["Architecture"],
        )
        migrate = adapter.migrate_and_check

        def target_migration(*args):
            adapter.migrate_and_check = migrate
            migrate(*args)
            with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection, connection:
                connection.execute("CREATE TABLE native_target_migration (id INTEGER)")
            if fail_migration == "process_death":
                os._exit(73)
            if fail_migration:
                raise OSError("Synthetic failure after real container migration")

        monkeypatch.setattr(adapter, "migrate_and_check", target_migration)
        engine = UpdateEngine(
            installation,
            [state],
            adapter,
            source_version="0.27.0",
            producer_version="0.27.0",
            analytics=AnalyticsConfig(enabled=False),
            enabled=lambda _: True,
            residents=DockerResidents(adapter, timeout=60),
        )
        if fail_migration == "process_death":
            child = os.fork()
            if child == 0:
                try:
                    engine.run(release)
                finally:
                    os._exit(74)
            _, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 73
            assert engine.fence.exists()
            # The killed child consumed its injection; recovery runs clean code.
            adapter.migrate_and_check = migrate
            result = engine.recover()
        else:
            result = engine.run(release)
        assert result.status == ("failed" if fail_migration else "updated"), result
        if fail_migration:
            assert result.code == "restored", result
        current = api.inspect_container(name)
        expected = "0.27.0" if fail_migration else "0.28.0"
        assert current["Image"] == api.inspect_image(image + ":" + expected)["Id"]
        assert current["State"]["Running"]
        assert current["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
        assert current["HostConfig"]["NetworkMode"] == "none"
        assert not any(
            mount.get("Destination") == "/var/run/docker.sock" for mount in current["Mounts"]
        )
        assert (state / "install_id").read_text() == identity
        with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection:
            assert connection.execute("SELECT value FROM native_preserved").fetchall() == [
                ("fixture data",)
            ]
            migrated = connection.execute(
                "SELECT name FROM sqlite_master WHERE name='native_target_migration'"
            ).fetchone()
            assert bool(migrated) is (not fail_migration)
        assert not engine.fence.exists()
    finally:
        for item in api.request("GET", "/containers/json", query={"all": 1}):
            container = api.inspect_container(item["Id"])
            if any(mount.get("Source") == str(root) for mount in container.get("Mounts", [])):
                api.request("DELETE", "/containers/" + item["Id"], query={"force": 1})
