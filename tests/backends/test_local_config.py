"""Tests for local audio backend configuration and factory."""

import os
from unittest.mock import MagicMock, patch

import pytest

from qobuz_proxy.backends import (
    BackendFactory,
    BackendRegistry,
    LocalAudioBackend,
)
from qobuz_proxy.config import (
    BackendConfig,
    Config,
    ConfigError,
    LocalConfig,
    dict_to_config,
    load_env_config,
    validate_config,
)
from qobuz_proxy.cli import args_to_dict


class TestLocalConfigDefaults:
    """Test LocalConfig dataclass defaults."""

    def test_defaults(self) -> None:
        config = LocalConfig()
        assert config.device == "default"
        assert config.buffer_size == 2048

    def test_custom_values(self) -> None:
        config = LocalConfig(device="USB Audio DAC", buffer_size=4096)
        assert config.device == "USB Audio DAC"
        assert config.buffer_size == 4096

    def test_backend_config_has_local(self) -> None:
        config = BackendConfig()
        assert isinstance(config.local, LocalConfig)
        assert config.local.device == "default"
        assert config.local.buffer_size == 2048


class TestDictToConfigLocal:
    """Test dict_to_config with local backend settings."""

    def test_parse_local_backend(self) -> None:
        d = {
            "backend": {
                "type": "local",
                "local": {
                    "device": "USB Audio DAC",
                    "buffer_size": 4096,
                },
            }
        }
        config = dict_to_config(d)
        assert config.backend.type == "local"
        assert config.backend.local.device == "USB Audio DAC"
        assert config.backend.local.buffer_size == 4096

    def test_parse_local_defaults(self) -> None:
        d = {"backend": {"type": "local"}}
        config = dict_to_config(d)
        assert config.backend.type == "local"
        assert config.backend.local.device == "default"
        assert config.backend.local.buffer_size == 2048

    def test_parse_local_partial(self) -> None:
        d = {"backend": {"type": "local", "local": {"device": "hw:1"}}}
        config = dict_to_config(d)
        assert config.backend.local.device == "hw:1"
        assert config.backend.local.buffer_size == 2048


class TestValidationLocal:
    """Test validate_config with local backend."""

    def _make_valid_local_config(self) -> Config:
        config = Config()
        config.qobuz.email = "test@example.com"
        config.qobuz.password = "testpass"
        config.backend.type = "local"
        return config

    def test_validation_skips_dlna_ip_for_local(self) -> None:
        config = self._make_valid_local_config()
        # Should not raise — no DLNA IP required
        validate_config(config)

    def test_validation_buffer_size_too_small(self) -> None:
        config = self._make_valid_local_config()
        config.backend.local.buffer_size = 32
        with pytest.raises(ConfigError, match="Invalid buffer_size"):
            validate_config(config)

    def test_validation_buffer_size_too_large(self) -> None:
        config = self._make_valid_local_config()
        config.backend.local.buffer_size = 32768
        with pytest.raises(ConfigError, match="Invalid buffer_size"):
            validate_config(config)

    def test_validation_buffer_size_min_boundary(self) -> None:
        config = self._make_valid_local_config()
        config.backend.local.buffer_size = 64
        validate_config(config)  # Should not raise

    def test_validation_buffer_size_max_boundary(self) -> None:
        config = self._make_valid_local_config()
        config.backend.local.buffer_size = 16384
        validate_config(config)  # Should not raise

    def test_validation_unknown_backend_type(self) -> None:
        config = self._make_valid_local_config()
        config.backend.type = "unknown"
        with pytest.raises(ConfigError, match="Unknown backend type"):
            validate_config(config)


class TestEnvVarsLocal:
    """Test environment variables for local backend."""

    def test_audio_device_env(self) -> None:
        with patch.dict(os.environ, {"QOBUZPROXY_AUDIO_DEVICE": "hw:1"}, clear=False):
            result = load_env_config()
            assert result["backend"]["local"]["device"] == "hw:1"

    def test_audio_buffer_size_env(self) -> None:
        with patch.dict(os.environ, {"QOBUZPROXY_AUDIO_BUFFER_SIZE": "4096"}, clear=False):
            result = load_env_config()
            assert result["backend"]["local"]["buffer_size"] == 4096

    def test_audio_buffer_size_invalid(self) -> None:
        with patch.dict(os.environ, {"QOBUZPROXY_AUDIO_BUFFER_SIZE": "notanumber"}, clear=False):
            result = load_env_config()
            # Should be skipped (not in result)
            assert "backend" not in result or "local" not in result.get("backend", {})


class TestArgsToDict:
    """Test CLI args mapping for local backend."""

    def test_audio_device_arg(self) -> None:
        from argparse import Namespace

        args = Namespace(
            email=None, password=None, max_quality=None, name=None, uuid=None,
            dlna_ip=None, dlna_port=None, fixed_volume=False,
            audio_device="USB DAC", audio_buffer_size=None, backend_type=None,
            http_port=None, proxy_port=None, bind=None, log_level=None,
        )
        result = args_to_dict(args)
        assert result["backend"]["local"]["device"] == "USB DAC"

    def test_backend_type_arg(self) -> None:
        from argparse import Namespace

        args = Namespace(
            email=None, password=None, max_quality=None, name=None, uuid=None,
            dlna_ip=None, dlna_port=None, fixed_volume=False,
            audio_device=None, audio_buffer_size=None, backend_type="local",
            http_port=None, proxy_port=None, bind=None, log_level=None,
        )
        result = args_to_dict(args)
        assert result["backend"]["type"] == "local"

    def test_audio_buffer_size_arg(self) -> None:
        from argparse import Namespace

        args = Namespace(
            email=None, password=None, max_quality=None, name=None, uuid=None,
            dlna_ip=None, dlna_port=None, fixed_volume=False,
            audio_device=None, audio_buffer_size=4096, backend_type=None,
            http_port=None, proxy_port=None, bind=None, log_level=None,
        )
        result = args_to_dict(args)
        assert result["backend"]["local"]["buffer_size"] == 4096


def _mock_sounddevice():
    """Create a mock sounddevice module with a default output device."""
    sd = MagicMock()
    sd.query_devices.return_value = [
        {
            "name": "Default Output",
            "max_output_channels": 2,
            "max_input_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            "name": "USB DAC",
            "max_output_channels": 2,
            "max_input_channels": 0,
            "default_samplerate": 96000.0,
        },
    ]
    sd.default.device = (0, 0)  # (input, output)
    return sd


_SD_PATCH = "qobuz_proxy.backends.local.device._import_sounddevice"


class TestFactoryLocal:
    """Test BackendFactory with local backend."""

    def test_local_registered(self) -> None:
        assert "local" in BackendRegistry.available_types()

    async def test_factory_creates_local_backend(self) -> None:
        config = Config()
        config.backend.type = "local"
        config.backend.local.device = "default"
        config.backend.local.buffer_size = 2048

        with patch(_SD_PATCH, return_value=_mock_sounddevice()):
            backend = await BackendFactory.create_from_config(config)

        assert isinstance(backend, LocalAudioBackend)
        assert backend.is_connected()

        info = backend.get_info()
        assert info.backend_type == "local"

        await backend.disconnect()

    async def test_factory_create_local_custom(self) -> None:
        with patch(_SD_PATCH, return_value=_mock_sounddevice()):
            backend = await BackendFactory.create_local(
                device="USB DAC", buffer_size=4096, name="My DAC"
            )
        assert isinstance(backend, LocalAudioBackend)
        assert "USB DAC" in backend.name
        await backend.disconnect()


class TestLocalAudioBackendStub:
    """Test LocalAudioBackend stub behavior."""

    async def test_stub_connect_disconnect(self) -> None:
        backend = LocalAudioBackend()
        assert not backend.is_connected()

        with patch(_SD_PATCH, return_value=_mock_sounddevice()):
            result = await backend.connect()
        assert result is True
        assert backend.is_connected()

        await backend.disconnect()
        assert not backend.is_connected()

    async def test_stub_volume(self) -> None:
        backend = LocalAudioBackend()
        await backend.set_volume(75)
        assert await backend.get_volume() == 75

    async def test_stub_volume_clamped(self) -> None:
        backend = LocalAudioBackend()
        await backend.set_volume(150)
        assert await backend.get_volume() == 100
        await backend.set_volume(-10)
        assert await backend.get_volume() == 0

    async def test_stub_position(self) -> None:
        backend = LocalAudioBackend()
        assert await backend.get_position() == 0

    async def test_stub_get_info(self) -> None:
        backend = LocalAudioBackend(device="test-device")
        info = backend.get_info()
        assert info.backend_type == "local"
        assert info.name == "Local Audio"
        assert info.device_id == "local-test-device"
