"""Config serialization and atomic YAML writer."""

import os
import tempfile
from pathlib import Path

import yaml

from qobuz_proxy.config import Config, speaker_config_to_dict


def config_to_dict(config: Config) -> dict:
    """Serialize a Config object to a YAML-ready dict.

    Only persists server settings, logging, and speakers. Auth credentials
    are excluded (managed separately). Speaker UUIDs are included so Qobuz
    Connect identity survives restarts.
    """
    return {
        "server": {
            "http_port": config.server.http_port,
            "bind_address": config.server.bind_address,
        },
        "logging": {
            "level": config.logging.level,
        },
        "speakers": [speaker_config_to_dict(s) for s in config.speakers],
    }


def save_config(config: Config, path: Path) -> None:
    """Persist web UI configuration changes."""
    _write_yaml(config_to_dict(config), path)


def persist_speaker_uuids(config: Config, path: Path) -> None:
    """Fill missing UUIDs in existing YAML speaker entries at startup.

    Preserve other settings, including credentials and explicit ports which
    the web UI serializer omits. Do not turn environment-only configuration
    into a YAML speakers list, since that would override later env changes.
    """
    if not config.speakers or not path.exists():
        return
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    uuids = {sc.name: sc.uuid for sc in config.speakers}
    changed = False
    for speaker in data.get("speakers") or []:
        speaker_uuid = uuids.get(speaker.get("name", "QobuzProxy"))
        if not speaker.get("uuid") and speaker_uuid:
            speaker["uuid"] = speaker_uuid
            changed = True
    if changed:
        _write_yaml(data, path)


def _write_yaml(data: dict, path: Path) -> None:
    """Write config to a YAML file using an atomic replace.

    Writes to a temporary file in the same directory, then uses os.replace()
    so readers never see a partial write.
    """
    dir_path = path.parent
    dir_path.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
