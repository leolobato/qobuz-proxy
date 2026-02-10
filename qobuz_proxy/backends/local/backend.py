"""
Local audio backend (stub).

Full implementation in QPROXY-021. This stub allows the factory
and configuration to be tested before audio playback is implemented.
"""

import logging

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import (
    BackendInfo,
    BackendTrackMetadata,
    PlaybackState,
)

logger = logging.getLogger(__name__)


class LocalAudioBackend(AudioBackend):
    """Local audio output backend using sounddevice/PortAudio."""

    def __init__(
        self,
        device: str = "default",
        buffer_size: int = 2048,
        name: str = "Local Audio",
    ):
        super().__init__(name)
        self._device_config = device
        self._buffer_size = buffer_size

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        logger.warning("[LOCAL-STUB] play() not implemented yet")

    async def pause(self) -> None:
        logger.warning("[LOCAL-STUB] pause() not implemented yet")

    async def resume(self) -> None:
        logger.warning("[LOCAL-STUB] resume() not implemented yet")

    async def stop(self) -> None:
        logger.warning("[LOCAL-STUB] stop() not implemented yet")

    async def seek(self, position_ms: int) -> None:
        logger.warning("[LOCAL-STUB] seek() not implemented yet")

    async def get_position(self) -> int:
        return 0

    async def set_volume(self, level: int) -> None:
        self._volume = max(0, min(100, level))

    async def get_volume(self) -> int:
        return self._volume

    async def get_state(self) -> PlaybackState:
        return self._state

    async def connect(self) -> bool:
        logger.info(f"[LOCAL-STUB] connect(device={self._device_config})")
        self._is_connected = True
        return True

    async def disconnect(self) -> None:
        self._is_connected = False

    def get_info(self) -> BackendInfo:
        return BackendInfo(
            backend_type="local",
            name=self.name,
            device_id=f"local-{self._device_config}",
        )
