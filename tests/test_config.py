"""Tests for QobuzProxy configuration system."""

import logging

import pytest

from qobuz_proxy.config import (
    AUTO_QUALITY,
    Config,
    ConfigError,
    QobuzConfig,
    SpeakerConfig,
    dict_to_config,
    slugify_name,
    speaker_config_to_dict,
    validate_config,
)


class TestQobuzConfigFields:
    """Test QobuzConfig dataclass fields and defaults."""

    def test_auth_token_field_exists_with_default(self) -> None:
        qc = QobuzConfig()
        assert qc.auth_token == ""

    def test_user_id_field_exists_with_default(self) -> None:
        qc = QobuzConfig()
        assert qc.user_id == ""

    def test_email_field_exists_with_default(self) -> None:
        qc = QobuzConfig()
        assert qc.email == ""

    def test_max_quality_default(self) -> None:
        qc = QobuzConfig()
        assert qc.max_quality == 27

    def test_all_fields_assignable(self) -> None:
        qc = QobuzConfig(
            email="user@example.com",
            auth_token="my_token_123",
            user_id="12345",
            max_quality=6,
        )
        assert qc.email == "user@example.com"
        assert qc.auth_token == "my_token_123"
        assert qc.user_id == "12345"
        assert qc.max_quality == 6


class TestDictToConfigAuthToken:
    """Test dict_to_config handles auth_token and password alias."""

    def test_auth_token_from_dict(self) -> None:
        d = {"qobuz": {"auth_token": "token_abc"}}
        config = dict_to_config(d)
        assert config.qobuz.auth_token == "token_abc"

    def test_user_id_from_dict(self) -> None:
        d = {"qobuz": {"user_id": "99999"}}
        config = dict_to_config(d)
        assert config.qobuz.user_id == "99999"

    def test_password_alias_maps_to_auth_token(self) -> None:
        d = {"qobuz": {"password": "legacy_pass"}}
        config = dict_to_config(d)
        assert config.qobuz.auth_token == "legacy_pass"

    def test_password_alias_logs_deprecation_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        d = {"qobuz": {"password": "legacy_pass"}}
        with caplog.at_level(logging.WARNING, logger="qobuz_proxy.config"):
            dict_to_config(d)
        assert any(
            "password" in rec.message.lower() and "deprecated" in rec.message.lower()
            for rec in caplog.records
        )

    def test_auth_token_takes_precedence_over_password(self) -> None:
        d = {"qobuz": {"auth_token": "new_token", "password": "old_pass"}}
        config = dict_to_config(d)
        assert config.qobuz.auth_token == "new_token"

    def test_defaults_when_no_qobuz_section(self) -> None:
        config = dict_to_config({})
        assert config.qobuz.auth_token == ""
        assert config.qobuz.user_id == ""
        assert config.qobuz.email == ""


class TestValidateConfigWithoutCredentials:
    """Test that validation passes without email/password credentials."""

    def test_validation_passes_without_credentials(self) -> None:
        config = Config()
        config.backend.type = "stub"
        # Should NOT raise — credentials are no longer required
        validate_config(config)

    def test_validation_passes_with_empty_email(self) -> None:
        config = Config()
        config.backend.type = "stub"
        config.qobuz.email = ""
        validate_config(config)

    def test_validation_checks_email_format_when_provided(self) -> None:
        config = Config()
        config.backend.type = "stub"
        config.qobuz.email = "not-an-email"
        with pytest.raises(ConfigError, match="Invalid email format"):
            validate_config(config)

    def test_validation_passes_with_valid_email(self) -> None:
        config = Config()
        config.backend.type = "stub"
        config.qobuz.email = "user@example.com"
        validate_config(config)

    def test_validation_still_checks_quality(self) -> None:
        config = Config()
        config.backend.type = "stub"
        config.qobuz.max_quality = 99
        with pytest.raises(ConfigError, match="Invalid max_quality"):
            validate_config(config)

    def test_validation_still_checks_backend(self) -> None:
        config = Config()
        config.backend.type = "unknown_backend"
        with pytest.raises(ConfigError, match="Unknown backend type"):
            validate_config(config)

    def test_validation_still_checks_ports(self) -> None:
        config = Config()
        config.backend.type = "stub"
        config.server.http_port = 99999
        with pytest.raises(ConfigError, match="Invalid HTTP port"):
            validate_config(config)


class TestSlugifyName:
    """Test slugify_name converts speaker names to URL-safe slugs."""

    def test_simple_name(self) -> None:
        assert slugify_name("Living Room") == "living-room"

    def test_special_chars(self) -> None:
        assert slugify_name("Den & Office!") == "den-office"

    def test_multiple_spaces(self) -> None:
        assert slugify_name("My  Speaker") == "my-speaker"

    def test_already_slug(self) -> None:
        assert slugify_name("kitchen") == "kitchen"

    def test_trailing_hyphens(self) -> None:
        assert slugify_name("Bedroom!") == "bedroom"

    def test_numbers_preserved(self) -> None:
        assert slugify_name("Speaker 2") == "speaker-2"

    def test_uppercase_lowercased(self) -> None:
        assert slugify_name("SONOS") == "sonos"


class TestSpeakerConfigToDict:
    """Test speaker_config_to_dict serializes SpeakerConfig to YAML-ready dict."""

    def _make_dlna_speaker(self, **kwargs) -> SpeakerConfig:  # type: ignore[return]
        defaults = dict(
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
        defaults.update(kwargs)
        return SpeakerConfig(**defaults)

    def _make_local_speaker(self, **kwargs) -> SpeakerConfig:  # type: ignore[return]
        defaults = dict(
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
        )
        defaults.update(kwargs)
        return SpeakerConfig(**defaults)

    def test_dlna_speaker_has_required_fields(self) -> None:
        sc = self._make_dlna_speaker()
        d = speaker_config_to_dict(sc)
        assert d["name"] == "Living Room"
        assert d["backend"] == "dlna"
        assert d["max_quality"] == 27
        assert d["dlna_ip"] == "192.168.1.100"
        assert d["dlna_port"] == 1400
        assert d["dlna_fixed_volume"] is False

    def test_dlna_speaker_persists_uuid_and_omits_ports(self) -> None:
        sc = self._make_dlna_speaker()
        d = speaker_config_to_dict(sc)
        assert d["uuid"] == "abc-123"
        assert "http_port" not in d
        assert "proxy_port" not in d

    def test_dlna_speaker_omits_empty_description_url(self) -> None:
        sc = self._make_dlna_speaker(dlna_description_url="")
        d = speaker_config_to_dict(sc)
        assert "dlna_description_url" not in d

    def test_dlna_speaker_includes_description_url_when_set(self) -> None:
        sc = self._make_dlna_speaker(
            dlna_description_url="http://192.168.1.100:1400/xml/device_description.xml"
        )
        d = speaker_config_to_dict(sc)
        assert d["dlna_description_url"] == "http://192.168.1.100:1400/xml/device_description.xml"

    def test_local_speaker_has_audio_fields(self) -> None:
        sc = self._make_local_speaker()
        d = speaker_config_to_dict(sc)
        assert d["name"] == "Headphones"
        assert d["backend"] == "local"
        assert d["audio_device"] == "Built-in Output"
        assert d["audio_buffer_size"] == 4096

    def test_local_speaker_omits_dlna_fields(self) -> None:
        sc = self._make_local_speaker()
        d = speaker_config_to_dict(sc)
        assert "dlna_ip" not in d
        assert "dlna_port" not in d
        assert "dlna_fixed_volume" not in d
        assert "dlna_description_url" not in d

    def test_auto_quality_serialized_as_string(self) -> None:
        sc = self._make_dlna_speaker(max_quality=AUTO_QUALITY)
        d = speaker_config_to_dict(sc)
        assert d["max_quality"] == "auto"

    def test_numeric_quality_stays_as_int(self) -> None:
        sc = self._make_dlna_speaker(max_quality=6)
        d = speaker_config_to_dict(sc)
        assert d["max_quality"] == 6
        assert isinstance(d["max_quality"], int)

    def test_uuid_round_trips_through_serialized_dict(self) -> None:
        from qobuz_proxy.config import Config, _parse_yaml_speakers

        sc = self._make_dlna_speaker(uuid="stable-uuid")
        loaded = _parse_yaml_speakers([speaker_config_to_dict(sc)], Config())
        assert loaded[0].uuid == "stable-uuid"
