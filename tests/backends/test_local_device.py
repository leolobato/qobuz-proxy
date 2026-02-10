"""Tests for local audio device discovery and selection."""

from unittest.mock import MagicMock, patch

import pytest

from qobuz_proxy.backends.local.device import (
    AudioDeviceInfo,
    format_device_list,
    list_audio_devices,
    resolve_device,
)


# Mock device data matching sounddevice.query_devices() format
MOCK_DEVICES = [
    {
        "name": "Built-in Output",
        "max_output_channels": 2,
        "max_input_channels": 0,
        "default_samplerate": 44100.0,
    },
    {
        "name": "USB Audio DAC",
        "max_output_channels": 2,
        "max_input_channels": 0,
        "default_samplerate": 96000.0,
    },
    {
        "name": "Built-in Microphone",
        "max_output_channels": 0,
        "max_input_channels": 2,
        "default_samplerate": 48000.0,
    },
    {
        "name": "HDMI Audio Output",
        "max_output_channels": 8,
        "max_input_channels": 0,
        "default_samplerate": 48000.0,
    },
    {
        "name": "USB Audio Interface",
        "max_output_channels": 4,
        "max_input_channels": 2,
        "default_samplerate": 192000.0,
    },
]

# Default output device index (index 0 = Built-in Output)
MOCK_DEFAULT_OUTPUT = 0


def _mock_sounddevice():
    """Create a mock sounddevice module."""
    sd = MagicMock()
    sd.query_devices.return_value = MOCK_DEVICES
    sd.default.device = (2, MOCK_DEFAULT_OUTPUT)  # (input, output)
    return sd


class TestAudioDeviceInfo:
    """Test AudioDeviceInfo dataclass."""

    def test_fields(self) -> None:
        info = AudioDeviceInfo(
            index=1,
            name="Test DAC",
            channels=2,
            default_samplerate=96000.0,
            is_default=False,
        )
        assert info.index == 1
        assert info.name == "Test DAC"
        assert info.channels == 2
        assert info.default_samplerate == 96000.0
        assert info.is_default is False

    def test_default_marker(self) -> None:
        info = AudioDeviceInfo(
            index=0, name="Default", channels=2, default_samplerate=44100.0, is_default=True
        )
        assert info.is_default is True


class TestListAudioDevices:
    """Test list_audio_devices()."""

    def test_lists_output_devices_only(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            devices = list_audio_devices()

        # Should exclude Built-in Microphone (input only)
        assert len(devices) == 4
        names = [d.name for d in devices]
        assert "Built-in Microphone" not in names
        assert "Built-in Output" in names
        assert "USB Audio DAC" in names
        assert "HDMI Audio Output" in names
        assert "USB Audio Interface" in names

    def test_default_device_marked(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            devices = list_audio_devices()

        defaults = [d for d in devices if d.is_default]
        assert len(defaults) == 1
        assert defaults[0].name == "Built-in Output"

    def test_device_properties(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            devices = list_audio_devices()

        dac = next(d for d in devices if d.name == "USB Audio DAC")
        assert dac.index == 1
        assert dac.channels == 2
        assert dac.default_samplerate == 96000.0
        assert dac.is_default is False

    def test_import_error(self) -> None:
        def raise_import():
            raise ImportError("sounddevice is required")

        with patch("qobuz_proxy.backends.local.device._import_sounddevice", side_effect=raise_import):
            with pytest.raises(ImportError, match="sounddevice is required"):
                list_audio_devices()


class TestResolveDevice:
    """Test resolve_device()."""

    def test_resolve_default(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            dev = resolve_device("default")

        assert dev.name == "Built-in Output"
        assert dev.is_default is True

    def test_resolve_default_case_insensitive(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            dev = resolve_device("DEFAULT")

        assert dev.name == "Built-in Output"

    def test_resolve_default_fallback_when_no_default(self) -> None:
        sd = _mock_sounddevice()
        sd.default.device = (2, -1)  # No default output
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=sd):
            dev = resolve_device("default")

        # Falls back to first device
        assert dev.name == "Built-in Output"

    def test_resolve_by_index(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            dev = resolve_device("1")

        assert dev.name == "USB Audio DAC"
        assert dev.index == 1

    def test_resolve_by_index_invalid(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            with pytest.raises(ValueError, match="No audio output device at index 99"):
                resolve_device("99")

    def test_resolve_by_index_input_only(self) -> None:
        """Index 2 is an input-only device — should not resolve."""
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            with pytest.raises(ValueError, match="No audio output device at index 2"):
                resolve_device("2")

    def test_resolve_by_exact_name(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            dev = resolve_device("USB Audio DAC")

        assert dev.name == "USB Audio DAC"

    def test_resolve_by_exact_name_case_insensitive(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            dev = resolve_device("usb audio dac")

        assert dev.name == "USB Audio DAC"

    def test_resolve_by_substring(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            dev = resolve_device("HDMI")

        assert dev.name == "HDMI Audio Output"

    def test_resolve_by_substring_multiple_matches(self) -> None:
        """'USB' matches both 'USB Audio DAC' and 'USB Audio Interface'."""
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            dev = resolve_device("USB")

        # Should return first match
        assert dev.name == "USB Audio DAC"

    def test_resolve_not_found(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            with pytest.raises(ValueError, match="No audio device matching 'NonexistentDevice'"):
                resolve_device("NonexistentDevice")

    def test_resolve_not_found_lists_devices(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            with pytest.raises(ValueError, match="Available devices:"):
                resolve_device("NonexistentDevice")

    def test_resolve_no_devices(self) -> None:
        sd = _mock_sounddevice()
        sd.query_devices.return_value = []
        sd.default.device = (-1, -1)
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=sd):
            with pytest.raises(ValueError, match="No audio output devices found"):
                resolve_device("default")


class TestFormatDeviceList:
    """Test format_device_list()."""

    def test_format(self) -> None:
        devices = [
            AudioDeviceInfo(0, "Built-in Output", 2, 44100.0, True),
            AudioDeviceInfo(1, "USB DAC", 2, 96000.0, False),
        ]
        output = format_device_list(devices)
        assert "[0] Built-in Output (default)" in output
        assert "2ch, 44100Hz" in output
        assert "[1] USB DAC" in output
        assert "96000Hz" in output

    def test_format_empty(self) -> None:
        assert format_device_list([]) == ""

    def test_format_fetches_devices_when_none(self) -> None:
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            output = format_device_list(None)

        assert "Built-in Output" in output
        assert "USB Audio DAC" in output


class TestLocalBackendConnect:
    """Test LocalAudioBackend.connect() with device resolution."""

    async def test_connect_resolves_device(self) -> None:
        from qobuz_proxy.backends.local import LocalAudioBackend

        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            backend = LocalAudioBackend(device="USB Audio DAC")
            result = await backend.connect()

        assert result is True
        assert backend.is_connected()
        assert "USB Audio DAC" in backend.name

    async def test_connect_default_device(self) -> None:
        from qobuz_proxy.backends.local import LocalAudioBackend

        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            backend = LocalAudioBackend(device="default")
            result = await backend.connect()

        assert result is True
        assert "Built-in Output" in backend.name

    async def test_connect_invalid_device(self) -> None:
        from qobuz_proxy.backends.local import LocalAudioBackend

        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            backend = LocalAudioBackend(device="NonexistentDevice")
            result = await backend.connect()

        assert result is False
        assert not backend.is_connected()

    async def test_connect_import_error(self) -> None:
        from qobuz_proxy.backends.local import LocalAudioBackend

        with patch(
            "qobuz_proxy.backends.local.device._import_sounddevice",
            side_effect=ImportError("sounddevice is required"),
        ):
            backend = LocalAudioBackend(device="default")
            result = await backend.connect()

        assert result is False


class TestCliListAudioDevices:
    """Test --list-audio-devices CLI integration."""

    def test_run_list_audio_devices(self, capsys) -> None:
        from qobuz_proxy.cli import run_list_audio_devices

        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=_mock_sounddevice()):
            exit_code = run_list_audio_devices()

        assert exit_code == 0
        output = capsys.readouterr().out
        assert "audio output device" in output.lower()
        assert "Built-in Output" in output
        assert "USB Audio DAC" in output
        assert "Config example" in output

    def test_run_list_audio_devices_empty(self, capsys) -> None:
        from qobuz_proxy.cli import run_list_audio_devices

        sd = _mock_sounddevice()
        sd.query_devices.return_value = []
        sd.default.device = (-1, -1)
        with patch("qobuz_proxy.backends.local.device._import_sounddevice", return_value=sd):
            exit_code = run_list_audio_devices()

        assert exit_code == 0
        output = capsys.readouterr().out
        assert "No audio output devices found" in output
