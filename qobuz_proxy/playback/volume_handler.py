"""
Volume command handler for WebSocket integration.

Processes volume commands from the Qobuz app via WsManager.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .player import QobuzPlayer

logger = logging.getLogger(__name__)

# QConnect message types for volume commands
MSG_TYPE_SRVR_RNDR_SET_VOLUME = 42  # Server -> Renderer: set volume
MSG_TYPE_SRVR_CTRL_VOLUME_CHANGED = 87  # Server broadcast: volume changed


class VolumeCommandHandler:
    """
    Handles volume commands from WebSocket.

    Processes both absolute volume and volume delta commands.
    """

    def __init__(self, player: "QobuzPlayer"):
        """Initialize handler."""
        self.player = player

    def get_message_types(self) -> list[int]:
        """Get list of message types this handler processes."""
        return [
            MSG_TYPE_SRVR_RNDR_SET_VOLUME,
            MSG_TYPE_SRVR_CTRL_VOLUME_CHANGED,
        ]

    async def handle_message(self, msg_type: int, message: Any) -> None:
        """Handle a volume command message."""
        try:
            if msg_type == MSG_TYPE_SRVR_RNDR_SET_VOLUME:
                await self._handle_set_volume(message)
            elif msg_type == MSG_TYPE_SRVR_CTRL_VOLUME_CHANGED:
                await self._handle_volume_changed(message)
            else:
                logger.warning(f"Unhandled volume message type: {msg_type}")
        except Exception as e:
            logger.error(f"Error handling volume command {msg_type}: {e}", exc_info=True)

    async def _handle_set_volume(self, message: Any) -> None:
        """
        Handle SRVR_RNDR_SET_VOLUME message.

        Supports both absolute volume and volume delta.
        """
        if not message.HasField("srvrRndrSetVolume"):
            return

        vol_msg = message.srvrRndrSetVolume

        # Check for absolute volume first
        if vol_msg.HasField("volume"):
            volume = vol_msg.volume
            logger.debug(f"Received set volume: {volume}")
            await self.player.set_volume(volume)

        # Check for volume delta
        elif vol_msg.HasField("volumeDelta"):
            delta = vol_msg.volumeDelta
            logger.debug(f"Received volume delta: {delta}")
            await self.player.set_volume_delta(delta)

    async def _handle_volume_changed(self, message: Any) -> None:
        """
        Handle SRVR_CTRL_VOLUME_CHANGED message.

        This is a broadcast from server when another controller changed volume.
        We apply it if the renderer ID matches ours.
        """
        if not message.HasField("srvrCtrlVolumeChanged"):
            return

        vol_msg = message.srvrCtrlVolumeChanged

        # Note: In a full implementation, we would check rendererId
        # For single-instance, we assume it's for us
        if vol_msg.HasField("volume"):
            volume = vol_msg.volume
            logger.debug(f"Received volume changed broadcast: {volume}")
            await self.player.set_volume(volume)
