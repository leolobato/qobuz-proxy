"""Tests for config_writer module."""

from pathlib import Path

import yaml

from qobuz_proxy.config import AUTO_QUALITY, Config, SpeakerConfig
from qobuz_proxy.webui.config_writer import config_to_dict, persist_speaker_uuids, save_config


def _make_config_with_dlna_speaker() -> Config:
    config = Config()
    config.server.http_port = 8689
    config.logging.level = "info"
    config.speakers = [
        SpeakerConfig(
            name="Living Room",
            uuid="abc-123",
            backend_type="dlna",
            max_quality=27,
            http_port=8690,
            bind_address="0.0.0.0",
            dlna_ip="192.168.1.100",
            dlna_port=1400,
            dlna_fixed_volume=False,
            dlna_description_url="",
            proxy_port=7120,
            audio_device="default",
            audio_buffer_size=2048,
        )
    ]
    return config


def _make_config_with_two_speakers() -> Config:
    config = Config()
    config.server.http_port = 8689
    config.logging.level = "debug"
    config.speakers = [
        SpeakerConfig(
            name="Living Room",
            uuid="abc-123",
            backend_type="dlna",
            max_quality=27,
            http_port=8690,
            bind_address="0.0.0.0",
            dlna_ip="192.168.1.100",
            dlna_port=1400,
            dlna_fixed_volume=False,
            dlna_description_url="",
            proxy_port=7120,
            audio_device="default",
            audio_buffer_size=2048,
        ),
        SpeakerConfig(
            name="Headphones",
            uuid="def-456",
            backend_type="local",
            max_quality=6,
            http_port=8691,
            bind_address="0.0.0.0",
            dlna_ip="",
            dlna_port=1400,
            dlna_fixed_volume=False,
            dlna_description_url="",
            proxy_port=0,
            audio_device="Built-in Output",
            audio_buffer_size=4096,
        ),
    ]
    return config


class TestSaveConfig:
    """Test save_config writes config to YAML file."""

    def test_writes_speakers(self, tmp_path: Path) -> None:
        config = _make_config_with_dlna_speaker()
        path = tmp_path / "config.yaml"
        save_config(config, path)
        assert path.exists()
        data = yaml.safe_load(path.read_text())
        assert "speakers" in data
        assert len(data["speakers"]) == 1
        assert data["speakers"][0]["name"] == "Living Room"

    def test_atomic_write(self, tmp_path: Path) -> None:
        """Verify no .tmp file is left behind after successful write."""
        config = _make_config_with_dlna_speaker()
        path = tmp_path / "config.yaml"
        save_config(config, path)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_preserves_server_settings(self, tmp_path: Path) -> None:
        config = _make_config_with_dlna_speaker()
        config.server.http_port = 9000
        config.logging.level = "debug"
        path = tmp_path / "config.yaml"
        save_config(config, path)
        data = yaml.safe_load(path.read_text())
        assert data["server"]["http_port"] == 9000
        assert data["logging"]["level"] == "debug"

    def test_round_trip_auto_quality(self, tmp_path: Path) -> None:
        config = _make_config_with_dlna_speaker()
        config.speakers[0].max_quality = AUTO_QUALITY
        path = tmp_path / "config.yaml"
        save_config(config, path)
        data = yaml.safe_load(path.read_text())
        assert data["speakers"][0]["max_quality"] == "auto"

    def test_writes_multiple_speakers(self, tmp_path: Path) -> None:
        config = _make_config_with_two_speakers()
        path = tmp_path / "config.yaml"
        save_config(config, path)
        data = yaml.safe_load(path.read_text())
        assert len(data["speakers"]) == 2
        names = [s["name"] for s in data["speakers"]]
        assert "Living Room" in names
        assert "Headphones" in names

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("old: data\n")
        config = _make_config_with_dlna_speaker()
        save_config(config, path)
        data = yaml.safe_load(path.read_text())
        assert "old" not in data
        assert "speakers" in data


class TestConfigToDict:
    """Test config_to_dict serializes Config to a plain dict."""

    def test_has_server_section(self) -> None:
        config = _make_config_with_dlna_speaker()
        d = config_to_dict(config)
        assert "server" in d
        assert d["server"]["http_port"] == 8689

    def test_has_logging_section(self) -> None:
        config = _make_config_with_dlna_speaker()
        d = config_to_dict(config)
        assert "logging" in d
        assert d["logging"]["level"] == "info"

    def test_has_speakers_list(self) -> None:
        config = _make_config_with_dlna_speaker()
        d = config_to_dict(config)
        assert "speakers" in d
        assert isinstance(d["speakers"], list)
        assert len(d["speakers"]) == 1

    def test_speakers_are_serialized_dicts(self) -> None:
        config = _make_config_with_dlna_speaker()
        d = config_to_dict(config)
        speaker = d["speakers"][0]
        assert isinstance(speaker, dict)
        assert speaker["name"] == "Living Room"
        assert speaker["backend"] == "dlna"
        assert speaker["uuid"] == "abc-123"


def test_uuid_migration_does_not_freeze_environment_speakers(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    content = "logging:\n  level: debug\n"
    path.write_text(content)
    persist_speaker_uuids(_make_config_with_dlna_speaker(), path)
    assert path.read_text() == content


def test_uuid_migration_does_not_create_config_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    persist_speaker_uuids(_make_config_with_dlna_speaker(), path)
    assert not path.exists()
