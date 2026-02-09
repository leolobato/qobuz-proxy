"""
DLNA/UPnP SOAP client.

Low-level client for sending UPnP commands to DLNA devices.
"""

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, Optional

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)

# Minimum interval between volume commands (ms) to avoid overwhelming device
VOLUME_MIN_INTERVAL_MS = 200

# SOAP constants
SOAP_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
UPNP_AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
UPNP_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
UPNP_CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass
class DLNADeviceInfo:
    """DLNA device information from UPnP description."""

    friendly_name: str = ""
    manufacturer: str = ""
    model_name: str = ""
    udn: str = ""  # Unique Device Name
    av_transport_url: str = ""
    rendering_control_url: str = ""
    connection_manager_url: str = ""


class DLNAClientError(Exception):
    """DLNA client error."""

    pass


class DLNAClient:
    """
    Low-level DLNA/UPnP SOAP client.

    Handles:
    - Device description fetching and parsing
    - SOAP action encoding and sending
    - Response parsing
    - Retry logic for transient failures
    """

    def __init__(self, ip: str, port: int = 1400):
        """
        Initialize DLNA client.

        Args:
            ip: Device IP address
            port: Device port (default 1400 for Sonos)
        """
        self.ip = ip
        self.port = port
        self.device_info: Optional[DLNADeviceInfo] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_volume_time_ms: float = 0
        self._pending_volume: Optional[int] = None
        self._volume_debounce_task: Optional[asyncio.Task] = None
        self._volume_lock = asyncio.Lock()

    async def connect(self) -> DLNADeviceInfo:
        """
        Connect to device and fetch description.

        Returns:
            Device information

        Raises:
            DLNAClientError: If connection fails
        """
        self._session = aiohttp.ClientSession(timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS))

        # Try to fetch device description
        self.device_info = await self._fetch_device_description()
        if not self.device_info.av_transport_url:
            raise DLNAClientError(f"Device at {self.ip}:{self.port} does not support AVTransport")

        logger.info(f"Connected to DLNA device: {self.device_info.friendly_name}")
        return self.device_info

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._session:
            await self._session.close()
            self._session = None

    # =========================================================================
    # AVTransport Actions
    # =========================================================================

    async def set_av_transport_uri(
        self,
        url: str,
        metadata: str = "",
    ) -> bool:
        """
        Set the URI for playback.

        Args:
            url: Audio URL to play
            metadata: DIDL-Lite metadata XML

        Returns:
            True if successful
        """
        if not self.device_info:
            return False

        return (
            await self._soap_action(
                self.device_info.av_transport_url,
                UPNP_AV_TRANSPORT,
                "SetAVTransportURI",
                {
                    "InstanceID": "0",
                    "CurrentURI": url,
                    "CurrentURIMetaData": metadata,
                },
            )
            is not None
        )

    async def play(self, speed: str = "1") -> bool:
        """Start playback."""
        if not self.device_info:
            return False

        return (
            await self._soap_action(
                self.device_info.av_transport_url,
                UPNP_AV_TRANSPORT,
                "Play",
                {"InstanceID": "0", "Speed": speed},
            )
            is not None
        )

    async def pause(self) -> bool:
        """Pause playback."""
        if not self.device_info:
            return False

        return (
            await self._soap_action(
                self.device_info.av_transport_url,
                UPNP_AV_TRANSPORT,
                "Pause",
                {"InstanceID": "0"},
            )
            is not None
        )

    async def stop(self) -> bool:
        """Stop playback."""
        if not self.device_info:
            return False

        return (
            await self._soap_action(
                self.device_info.av_transport_url,
                UPNP_AV_TRANSPORT,
                "Stop",
                {"InstanceID": "0"},
            )
            is not None
        )

    async def seek(self, position_ms: int) -> bool:
        """
        Seek to position.

        Args:
            position_ms: Position in milliseconds
        """
        if not self.device_info:
            return False

        time_str = self._ms_to_time_string(position_ms)
        return (
            await self._soap_action(
                self.device_info.av_transport_url,
                UPNP_AV_TRANSPORT,
                "Seek",
                {
                    "InstanceID": "0",
                    "Unit": "REL_TIME",
                    "Target": time_str,
                },
            )
            is not None
        )

    async def get_transport_info(self) -> Optional[str]:
        """
        Get current transport state.

        Returns:
            Transport state string: "PLAYING", "PAUSED_PLAYBACK", "STOPPED", etc.
        """
        if not self.device_info:
            return None

        response = await self._soap_action(
            self.device_info.av_transport_url,
            UPNP_AV_TRANSPORT,
            "GetTransportInfo",
            {"InstanceID": "0"},
        )

        if response:
            return self._parse_xml_value(response, "CurrentTransportState")
        return None

    async def get_position_info(self) -> Optional[int]:
        """
        Get current position.

        Returns:
            Position in milliseconds
        """
        if not self.device_info:
            return None

        response = await self._soap_action(
            self.device_info.av_transport_url,
            UPNP_AV_TRANSPORT,
            "GetPositionInfo",
            {"InstanceID": "0"},
        )

        if response:
            time_str = self._parse_xml_value(response, "RelTime")
            if time_str:
                ms = self._time_string_to_ms(time_str)
                logger.debug(f"GetPositionInfo: RelTime={time_str} -> {ms}ms")
                return ms
            else:
                logger.debug("GetPositionInfo: RelTime not found in response")
        else:
            logger.debug("GetPositionInfo: No response from SOAP action")
        return None

    # =========================================================================
    # RenderingControl Actions
    # =========================================================================

    async def get_volume(self) -> Optional[int]:
        """
        Get current volume.

        Returns:
            Volume 0-100
        """
        if not self.device_info or not self.device_info.rendering_control_url:
            return None

        response = await self._soap_action(
            self.device_info.rendering_control_url,
            UPNP_RENDERING_CONTROL,
            "GetVolume",
            {"InstanceID": "0", "Channel": "Master"},
        )

        if response:
            vol_str = self._parse_xml_value(response, "CurrentVolume")
            if vol_str:
                return int(vol_str)
        return None

    async def set_volume(self, volume: int) -> bool:
        """
        Set volume with debouncing.

        Rate limits volume commands but ensures the final value is always sent.

        Args:
            volume: Volume 0-100
        """
        if not self.device_info or not self.device_info.rendering_control_url:
            logger.warning("Cannot set volume: no RenderingControl URL")
            return False

        async with self._volume_lock:
            now_ms = time.time() * 1000
            elapsed = now_ms - self._last_volume_time_ms

            if elapsed < VOLUME_MIN_INTERVAL_MS:
                # Too soon - store pending value and schedule delayed send
                self._pending_volume = volume
                if self._volume_debounce_task is None or self._volume_debounce_task.done():
                    delay_ms = VOLUME_MIN_INTERVAL_MS - elapsed
                    self._volume_debounce_task = asyncio.create_task(
                        self._send_pending_volume(delay_ms / 1000.0)
                    )
                logger.debug(f"Debouncing SetVolume({volume}), will send in {delay_ms:.0f}ms")
                return True

            # Clear any pending volume since we're sending now
            self._pending_volume = None
            return await self._do_set_volume(volume)

    async def _send_pending_volume(self, delay: float) -> None:
        """Send pending volume after delay."""
        await asyncio.sleep(delay)
        async with self._volume_lock:
            if self._pending_volume is not None:
                volume = self._pending_volume
                self._pending_volume = None
                await self._do_set_volume(volume)

    async def _do_set_volume(self, volume: int) -> bool:
        """Actually send the volume command."""
        if not self.device_info or not self.device_info.rendering_control_url:
            return False
        self._last_volume_time_ms = time.time() * 1000
        logger.debug(f"SetVolume({volume}) to {self.device_info.rendering_control_url}")

        result = await self._soap_action(
            self.device_info.rendering_control_url,
            UPNP_RENDERING_CONTROL,
            "SetVolume",
            {
                "InstanceID": "0",
                "Channel": "Master",
                "DesiredVolume": str(volume),
            },
            max_retries=1,  # Don't retry volume commands (UI will send new ones)
        )
        return result is not None

    # =========================================================================
    # ConnectionManager Actions
    # =========================================================================

    async def get_protocol_info(self) -> Optional[str]:
        """
        Query device for supported protocols via ConnectionManager GetProtocolInfo.

        Returns the Sink string containing supported audio formats.

        Returns:
            Sink protocol info string, or None if not supported
        """
        if not self.device_info or not self.device_info.connection_manager_url:
            logger.debug("ConnectionManager not available on this device")
            return None

        response = await self._soap_action(
            self.device_info.connection_manager_url,
            UPNP_CONNECTION_MANAGER,
            "GetProtocolInfo",
            {},
        )

        if response:
            sink = self._parse_xml_value(response, "Sink")
            if sink:
                logger.debug(f"GetProtocolInfo Sink: {sink[:200]}...")
                return sink
        return None

    # =========================================================================
    # Internal Methods
    # =========================================================================

    async def _fetch_device_description(self) -> DLNADeviceInfo:
        """Fetch and parse device description XML."""
        if not self._session:
            raise DLNAClientError("Session not initialized")

        # Common paths to try
        paths = [
            "/xml/device_description.xml",  # Sonos
            "/description.xml",
            "/DeviceDescription.xml",
            "/upnp/desc/aios_device/aios_device.xml",  # Denon/Marantz
            "/dmr/SamsungMRDesc.xml",  # Samsung
            "/rootDesc.xml",
        ]

        xml_text = None
        base_url = None

        for path in paths:
            url = f"http://{self.ip}:{self.port}{path}"
            try:
                async with self._session.get(url) as response:
                    if response.status == 200:
                        xml_text = await response.text()
                        base_url = f"http://{self.ip}:{self.port}"
                        logger.debug(f"Found device description at {url}")
                        break
            except Exception as e:
                logger.debug(f"Path {path} failed: {e}")
                continue

        if not xml_text:
            raise DLNAClientError(f"Could not find device description for {self.ip}:{self.port}")

        return self._parse_device_description(xml_text, base_url or "")

    def _parse_device_description(self, xml_text: str, base_url: str) -> DLNADeviceInfo:
        """Parse device description XML."""
        info = DLNADeviceInfo()

        try:
            root = ET.fromstring(xml_text)

            # Find device element (handle namespaces)
            for elem in root.iter():
                tag = elem.tag.split("}")[-1]  # Remove namespace

                if tag == "friendlyName":
                    info.friendly_name = elem.text or ""
                elif tag == "manufacturer":
                    info.manufacturer = elem.text or ""
                elif tag == "modelName":
                    info.model_name = elem.text or ""
                elif tag == "UDN":
                    info.udn = elem.text or ""

            # Find service URLs
            for service in root.iter():
                if not service.tag.endswith("service"):
                    continue

                service_type = ""
                control_url = ""

                for child in service:
                    tag = child.tag.split("}")[-1]
                    if tag == "serviceType":
                        service_type = child.text or ""
                    elif tag == "controlURL":
                        control_url = child.text or ""

                if "AVTransport" in service_type and control_url:
                    if control_url.startswith("/"):
                        info.av_transport_url = base_url + control_url
                    else:
                        info.av_transport_url = control_url

                elif "RenderingControl" in service_type and control_url:
                    # Prefer standard RenderingControl over GroupRenderingControl
                    # GroupRenderingControl is Sonos-specific and uses different API
                    is_standard = "GroupRenderingControl" not in service_type
                    if is_standard or not info.rendering_control_url:
                        if control_url.startswith("/"):
                            info.rendering_control_url = base_url + control_url
                        else:
                            info.rendering_control_url = control_url

                elif "ConnectionManager" in service_type and control_url:
                    if control_url.startswith("/"):
                        info.connection_manager_url = base_url + control_url
                    else:
                        info.connection_manager_url = control_url

        except ET.ParseError as e:
            logger.error(f"Failed to parse device description: {e}")

        logger.debug(
            f"Parsed device info: friendly_name={info.friendly_name}, "
            f"manufacturer={info.manufacturer}, model={info.model_name}"
        )
        logger.debug(
            f"Service URLs: AVTransport={info.av_transport_url}, "
            f"RenderingControl={info.rendering_control_url}"
        )
        return info

    async def _soap_action(
        self,
        url: str,
        service: str,
        action: str,
        args: Dict[str, str],
        max_retries: Optional[int] = None,
    ) -> Optional[str]:
        """
        Send SOAP action with retry logic.

        Args:
            url: Service control URL
            service: UPnP service type
            action: SOAP action name
            args: Action arguments
            max_retries: Override default retry count (default: MAX_RETRIES)

        Returns:
            Response body or None on failure
        """
        if not url or not self._session:
            logger.warning(f"No URL for service {service}")
            return None

        retries = max_retries if max_retries is not None else MAX_RETRIES
        envelope = self._build_soap_envelope(service, action, args)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service}#{action}"',
        }

        last_error = None
        for attempt in range(retries):
            try:
                async with self._session.post(url, data=envelope, headers=headers) as response:
                    if response.status == 200:
                        return await response.text()
                    else:
                        text = await response.text()
                        logger.warning(f"SOAP {action} failed ({response.status}): {text[:500]}")
                        # Try to extract UPnP error details
                        error_code = self._parse_xml_value(text, "errorCode")
                        error_desc = self._parse_xml_value(text, "errorDescription")
                        if error_code or error_desc:
                            logger.warning(
                                f"UPnP error: code={error_code}, description={error_desc}"
                            )
                        last_error = f"HTTP {response.status}"

            except Exception as e:
                logger.warning(f"SOAP {action} error (attempt {attempt + 1}): {e}")
                last_error = str(e)

            if attempt < retries - 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        if retries > 1:
            logger.error(f"SOAP {action} failed after {retries} attempts: {last_error}")
        else:
            logger.warning(f"SOAP {action} failed: {last_error}")
        return None

    def _build_soap_envelope(
        self,
        service: str,
        action: str,
        args: Dict[str, str],
    ) -> str:
        """Build SOAP envelope XML.

        Uses single-line format matching SoCo library for Sonos compatibility.
        """

        def escape(s: str) -> str:
            return (
                s.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;")
            )

        args_xml = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())

        # Single-line format for Sonos compatibility (matches SoCo library)
        return (
            '<?xml version="1.0"?>'
            f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NS}" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action} xmlns:u="{service}">'
            f"{args_xml}"
            f"</u:{action}>"
            "</s:Body>"
            "</s:Envelope>"
        )

    def _parse_xml_value(self, xml_text: str, tag_name: str) -> Optional[str]:
        """Extract value from XML response by tag name."""
        try:
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                if tag_name in elem.tag:
                    return elem.text
        except ET.ParseError:
            pass
        return None

    def _ms_to_time_string(self, ms: int) -> str:
        """Convert milliseconds to HH:MM:SS format."""
        seconds = ms // 1000
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _time_string_to_ms(self, time_str: str) -> int:
        """Convert HH:MM:SS to milliseconds."""
        try:
            parts = time_str.split(":")
            if len(parts) == 3:
                hours, minutes, seconds = parts
                total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                return int(total * 1000)
        except (ValueError, IndexError):
            pass
        return 0
