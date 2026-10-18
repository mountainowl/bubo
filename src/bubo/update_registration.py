"""Resolve one shared registration from its host or container state directory."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


def _absolute(value) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError("Invalid update mapping path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("Update mapping paths must be normalized and absolute")
    return path


def load_registration(state: Path) -> dict:
    record = json.loads((state / "update-registration.json").read_text())
    if not isinstance(record, dict):
        raise ValueError("Invalid update registration")
    if "external" not in record:
        return record
    if record["external"] != "docker" or record.get("protocol_version") != 1:
        raise ValueError("Unknown external update controller")
    mapping = record.get("mapping")
    if not isinstance(mapping, dict) or set(mapping) != {"host", "container"}:
        raise ValueError("Incomplete update path mapping")
    for side in mapping.values():
        if not isinstance(side, dict) or set(side) != {"root", "state", "installation"}:
            raise ValueError("Incomplete update path mapping")
        root, storage, installation = (
            _absolute(side[key]) for key in ("root", "state", "installation")
        )
        if not storage.is_relative_to(root) or not installation.is_relative_to(root):
            raise ValueError("Update paths must stay within the persistent root")
        if storage == root or installation == root:
            raise ValueError("Update storage requires a distinct directory")
    host, container = mapping["host"], mapping["container"]
    for key in ("state", "installation"):
        if PurePosixPath(host[key]).relative_to(host["root"]) != PurePosixPath(
            container[key]
        ).relative_to(container["root"]):
            raise ValueError("Update path mapping does not preserve mount-relative paths")
    matches = [side for side in mapping if mapping[side]["state"] == str(state.resolve())]
    if not matches or (len(matches) > 1 and host != container):
        raise ValueError("State directory is not an unambiguous registered mount")
    view = matches[0] if len(matches) == 1 else "shared"
    selected = mapping[matches[0]]
    root = Path(selected["root"]).resolve()
    if not all(
        Path(selected[key]).resolve().is_relative_to(root) for key in ("state", "installation")
    ):
        raise ValueError("Update path symlink escapes the persistent root")
    return {
        **record,
        **selected,
        "view": view,
        "state_dir": selected["state"],
        "base_dir": str(PurePosixPath(selected["state"]).parent),
    }


def journal_path(state: Path, value: str) -> Path:
    """Journal paths are canonical host paths; map them only at the reader."""
    record = load_registration(state)
    if record.get("external") != "docker":
        return Path(value)
    path = _absolute(value)
    host = record["mapping"]["host"]
    if not path.is_relative_to(host["root"]):
        raise ValueError("Journal path is outside the registered persistent root")
    local = Path(record["root"]).joinpath(*path.relative_to(host["root"]).parts).resolve()
    if not local.is_relative_to(Path(record["root"]).resolve()):
        raise ValueError("Journal path symlink escapes the persistent root")
    return local
