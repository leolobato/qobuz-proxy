"""
Audio device discovery and selection.

Enumerates system audio output devices via sounddevice (PortAudio)
and resolves user configuration to a specific device.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AudioDeviceInfo:
    """Information about an audio output device."""

    index: int
    name: str
    channels: int
    default_samplerate: float
    is_default: bool


def _import_sounddevice():
    """Lazy import of sounddevice."""
    try:
        import sounddevice as sd

        return sd
    except ImportError:
        raise ImportError(
            "sounddevice is required for local audio backend. "
            "Install with: pip install qobuz-proxy[local]"
        )


def list_audio_devices() -> list[AudioDeviceInfo]:
    """
    List available audio output devices.

    Returns:
        List of output devices with their properties.
    """
    sd = _import_sounddevice()
    devices = sd.query_devices()
    default_output = sd.default.device[1]  # Index of default output

    result = []
    for i, dev in enumerate(devices):
        if dev["max_output_channels"] > 0:
            result.append(
                AudioDeviceInfo(
                    index=i,
                    name=dev["name"],
                    channels=dev["max_output_channels"],
                    default_samplerate=dev["default_samplerate"],
                    is_default=(i == default_output),
                )
            )

    return result


def resolve_device(device_config: str) -> AudioDeviceInfo:
    """
    Resolve a device configuration string to a specific device.

    Args:
        device_config: "default", device index (int string), or device name/substring

    Returns:
        Resolved AudioDeviceInfo

    Raises:
        ValueError: If device not found, with helpful message listing available devices
    """
    devices = list_audio_devices()

    if not devices:
        raise ValueError("No audio output devices found on this system")

    # "default" — use system default
    if device_config.lower() == "default":
        for dev in devices:
            if dev.is_default:
                logger.info(f"Using default audio device: {dev.name}")
                return dev
        # Fallback to first device
        logger.warning("No default device found, using first available")
        return devices[0]

    # Try as integer index
    try:
        index = int(device_config)
        for dev in devices:
            if dev.index == index:
                logger.info(f"Using audio device by index {index}: {dev.name}")
                return dev
        raise ValueError(
            f"No audio output device at index {index}. "
            f"Available devices:\n{format_device_list(devices)}"
        )
    except ValueError as e:
        if "No audio output device at index" in str(e):
            raise

    # Try exact name match (case-insensitive)
    config_lower = device_config.lower()
    for dev in devices:
        if dev.name.lower() == config_lower:
            logger.info(f"Using audio device: {dev.name}")
            return dev

    # Try substring match (case-insensitive)
    matches = [d for d in devices if config_lower in d.name.lower()]
    if len(matches) == 1:
        logger.info(f"Using audio device (substring match): {matches[0].name}")
        return matches[0]
    elif len(matches) > 1:
        logger.warning(
            f"Multiple devices match '{device_config}', using first: {matches[0].name}"
        )
        return matches[0]

    raise ValueError(
        f"No audio device matching '{device_config}'. "
        f"Available devices:\n{format_device_list(devices)}"
    )


def format_device_list(devices: Optional[list[AudioDeviceInfo]] = None) -> str:
    """Format device list for display."""
    if devices is None:
        devices = list_audio_devices()

    lines = []
    for dev in devices:
        default_marker = " (default)" if dev.is_default else ""
        lines.append(
            f"  [{dev.index}] {dev.name}{default_marker} "
            f"- {dev.channels}ch, {int(dev.default_samplerate)}Hz"
        )
    return "\n".join(lines)
