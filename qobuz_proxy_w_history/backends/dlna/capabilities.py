"""
DLNA capability discovery and parsing.

Queries DLNA devices for supported audio formats via ConnectionManager GetProtocolInfo
and maps capabilities to Qobuz quality levels.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Quality constants matching Qobuz format IDs
QOBUZ_QUALITY_MP3 = 5
QOBUZ_QUALITY_CD = 6
QOBUZ_QUALITY_96K = 7
QOBUZ_QUALITY_192K = 27


@dataclass(frozen=True)
class DlnaProtocolInfoEntry:
    """Single parsed protocolInfo entry from GetProtocolInfo Sink."""

    protocol: str  # e.g., "http-get"
    network: str  # e.g., "*"
    content_format: str  # e.g., "audio/flac", "audio/L16"
    additional: dict[str, str]  # raw token map
    profile: Optional[str] = None  # DLNA.ORG_PN
    op: Optional[str] = None  # DLNA.ORG_OP (2-bit string)
    flags: Optional[int] = None  # DLNA.ORG_FLAGS as int
    mime: str = ""  # normalized mime type
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: Optional[int] = None


@dataclass
class DLNACapabilities:
    """Parsed device capabilities from GetProtocolInfo."""

    entries: list[DlnaProtocolInfoEntry] = field(default_factory=list)
    supports_flac: bool = False
    supports_mp3: bool = True  # Assume baseline MP3 support
    max_sample_rate: int = 44100
    max_bit_depth: int = 16

    @property
    def max_quality(self) -> int:
        """Map capabilities to Qobuz quality level (conservative)."""
        if not self.supports_flac:
            return QOBUZ_QUALITY_MP3
        if self.max_bit_depth >= 24 and self.max_sample_rate >= 192000:
            return QOBUZ_QUALITY_192K
        if self.max_bit_depth >= 24 and self.max_sample_rate >= 96000:
            return QOBUZ_QUALITY_96K
        return QOBUZ_QUALITY_CD

    def by_mime(self, mime: str) -> list[DlnaProtocolInfoEntry]:
        """Get all entries matching a mime type."""
        return [e for e in self.entries if e.mime == mime]

    def best_entry_for_media(
        self,
        mime: str,
        sample_rate: Optional[int] = None,
        bit_depth: Optional[int] = None,
    ) -> Optional[DlnaProtocolInfoEntry]:
        """Find best matching Sink entry for the media to serve."""
        candidates = []
        for e in self.by_mime(mime):
            if sample_rate and e.sample_rate and e.sample_rate < sample_rate:
                continue
            if bit_depth and e.bit_depth and e.bit_depth < bit_depth:
                continue
            candidates.append(e)
        if not candidates:
            return None
        # Prefer entries with profile annotation
        return max(candidates, key=lambda e: (1 if e.profile else 0, e.sample_rate or 0))


# DLNA profile quality mapping (conservative)
DLNA_PROFILE_QUALITY = {
    "FLAC": (QOBUZ_QUALITY_CD, 16, 44100),
    "FLAC_24": (QOBUZ_QUALITY_96K, 24, 96000),
    "FLAC_96": (QOBUZ_QUALITY_96K, 24, 96000),
    "FLAC_192": (QOBUZ_QUALITY_192K, 24, 192000),
    "MP3": (QOBUZ_QUALITY_MP3, 16, 44100),
}


def parse_protocol_info_sink(sink: str) -> DLNACapabilities:
    """
    Parse ConnectionManager GetProtocolInfo Sink string.

    The Sink string contains comma-separated protocol info entries in the format:
    protocol:network:contentFormat:additionalInfo

    Example entry:
    http-get:*:audio/flac:DLNA.ORG_PN=FLAC;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000

    Args:
        sink: The Sink string from GetProtocolInfo response

    Returns:
        Parsed DLNACapabilities object
    """
    caps = DLNACapabilities()
    if not sink:
        return caps

    for raw_entry in sink.split(","):
        raw_entry = raw_entry.strip()
        if not raw_entry:
            continue

        parts = raw_entry.split(":")
        if len(parts) < 4:
            continue

        protocol, network, content_format = parts[0], parts[1], parts[2]
        additional_str = ":".join(parts[3:])
        additional = _parse_additional(additional_str)

        profile = additional.get("DLNA.ORG_PN")
        op = additional.get("DLNA.ORG_OP")
        flags_str = additional.get("DLNA.ORG_FLAGS")
        flags = int(flags_str, 16) if flags_str else None

        mime = content_format.split(";")[0].strip().lower()
        sr, bd, ch = _parse_format_params(content_format, additional)

        # Apply profile hints if no explicit params
        if profile and profile in DLNA_PROFILE_QUALITY:
            _, profile_bd, profile_sr = DLNA_PROFILE_QUALITY[profile]
            sr = sr or profile_sr
            bd = bd or profile_bd

        entry = DlnaProtocolInfoEntry(
            protocol=protocol,
            network=network,
            content_format=content_format,
            additional=additional,
            profile=profile,
            op=op,
            flags=flags,
            mime=mime,
            sample_rate=sr,
            bit_depth=bd,
            channels=ch,
        )
        caps.entries.append(entry)

        # Update capability flags
        if mime == "audio/flac":
            caps.supports_flac = True
            caps.max_sample_rate = max(caps.max_sample_rate, sr or 44100)
            caps.max_bit_depth = max(caps.max_bit_depth, bd or 16)
        elif mime == "audio/mpeg":
            caps.supports_mp3 = True

    logger.info(
        f"Parsed capabilities: FLAC={caps.supports_flac}, "
        f"max_sr={caps.max_sample_rate}Hz, max_bd={caps.max_bit_depth}bit, "
        f"quality={caps.max_quality}"
    )
    return caps


def _parse_additional(s: str) -> dict[str, str]:
    """Parse DLNA additional info tokens."""
    tokens: dict[str, str] = {}
    for match in re.finditer(r"(?P<k>[^=;]+)=(?P<v>[^;]*)", s):
        tokens[match.group("k").strip()] = match.group("v").strip()
    return tokens


def _parse_format_params(
    content_format: str, additional: dict[str, str]
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Extract sample rate, bit depth, channels from format string or tokens."""
    sr: Optional[int] = None
    bd: Optional[int] = None
    ch: Optional[int] = None

    # Check L16 params: audio/L16;rate=44100;channels=2
    if "audio/l16" in content_format.lower():
        bd = 16
        for part in content_format.split(";")[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().lower()
                if k == "rate":
                    sr = int(v.strip())
                elif k == "channels":
                    ch = int(v.strip())

    # Check additional tokens for hints
    sr = sr or _try_int(additional.get("sampleRate") or additional.get("samplerate"))
    bd = bd or _try_int(additional.get("bitsPerSample") or additional.get("bitdepth"))

    return sr, bd, ch


def _try_int(v: Optional[str]) -> Optional[int]:
    """Safely convert string to int, returning None on failure."""
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def build_protocol_info(
    caps: DLNACapabilities,
    mime: str,
    sr: Optional[int] = None,
    bd: Optional[int] = None,
) -> str:
    """
    Build res@protocolInfo string matching device capabilities.

    Args:
        caps: Device capabilities
        mime: MIME type (e.g., "audio/flac")
        sr: Sample rate in Hz
        bd: Bit depth

    Returns:
        Protocol info string for DIDL-Lite res element
    """
    entry = caps.best_entry_for_media(mime, sr, bd)
    if entry:
        # Re-emit exact Sink entry for compatibility
        add_str = (
            ";".join(f"{k}={v}" for k, v in entry.additional.items()) if entry.additional else "*"
        )
        return f"{entry.protocol}:{entry.network}:{entry.content_format}:{add_str}"
    # Generic fallback
    return f"http-get:*:{mime}:DLNA.ORG_OP=01"


# Known device limitations
DEVICE_OVERRIDES: dict[str, dict[str, int]] = {
    "Sonos": {"max_sample_rate": 48000, "max_bit_depth": 16},
}


def apply_device_overrides(caps: DLNACapabilities, manufacturer: str, model: str) -> None:
    """
    Apply known device-specific limitations.

    Some devices advertise capabilities they don't fully support.
    This applies conservative overrides for known devices.

    Args:
        caps: Capabilities object to modify in place
        manufacturer: Device manufacturer string
        model: Device model string
    """
    device_str = f"{manufacturer} {model}".lower()
    for pattern, overrides in DEVICE_OVERRIDES.items():
        if pattern.lower() in device_str:
            logger.info(f"Applying {pattern} overrides: {overrides}")
            for k, v in overrides.items():
                setattr(caps, k, v)
            break


# Capability cache
@dataclass
class CapabilityCacheEntry:
    """Cache entry for device capabilities."""

    capabilities: DLNACapabilities
    fetched_at: float
    device_id: str


class CapabilityCache:
    """
    Cache for DLNA device capabilities.

    Capabilities are cached by device UUID for 24 hours by default.
    """

    def __init__(self, ttl_seconds: int = 86400):
        """
        Initialize cache.

        Args:
            ttl_seconds: Time-to-live for cache entries (default 24 hours)
        """
        self._ttl = ttl_seconds
        self._entries: dict[str, CapabilityCacheEntry] = {}

    def get(self, device_id: str) -> Optional[DLNACapabilities]:
        """
        Get cached capabilities for device.

        Args:
            device_id: Device UUID or IP

        Returns:
            Capabilities if cached and not expired, None otherwise
        """
        entry = self._entries.get(device_id)
        if not entry:
            return None
        if time.time() - entry.fetched_at > self._ttl:
            self._entries.pop(device_id, None)
            return None
        return entry.capabilities

    def set(self, device_id: str, caps: DLNACapabilities) -> None:
        """
        Store capabilities in cache.

        Args:
            device_id: Device UUID or IP
            caps: Capabilities to cache
        """
        self._entries[device_id] = CapabilityCacheEntry(
            capabilities=caps, fetched_at=time.time(), device_id=device_id
        )

    def invalidate(self, device_id: str) -> None:
        """
        Remove cached capabilities for device.

        Args:
            device_id: Device UUID or IP
        """
        self._entries.pop(device_id, None)
