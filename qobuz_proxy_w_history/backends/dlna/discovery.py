"""
DLNA/UPnP Discovery

SSDP-based discovery for DLNA renderers (Sonos, HEOS, etc.)
"""

import asyncio
import logging
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

# SSDP constants
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 3  # Maximum wait time in seconds

# UPnP device type we're looking for
MEDIA_RENDERER_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"


@dataclass
class DiscoveredDevice:
    """Discovered DLNA device information."""

    friendly_name: str
    ip: str
    port: int
    model_name: str = ""
    manufacturer: str = ""
    udn: str = ""
    location: str = ""


class DLNADiscovery:
    """
    SSDP-based discovery for DLNA Media Renderers.

    Usage:
        discovery = DLNADiscovery()
        devices = await discovery.discover(timeout=5.0)
        for device in devices:
            print(f"Found: {device.friendly_name} at {device.ip}:{device.port}")
    """

    def __init__(self) -> None:
        self._devices: dict[str, _RawDevice] = {}
        self._on_device_found: Callable[[DiscoveredDevice], None] | None = None

    async def discover(
        self,
        timeout: float = 5.0,
        search_target: str = MEDIA_RENDERER_TYPE,
    ) -> list[DiscoveredDevice]:
        """
        Discover DLNA devices on the network.

        Args:
            timeout: Discovery timeout in seconds
            search_target: UPnP device/service type to search for

        Returns:
            List of discovered DiscoveredDevice objects
        """
        self._devices.clear()

        # Create SSDP M-SEARCH message
        search_message = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {SSDP_MX}\r\n"
            f"ST: {search_target}\r\n"
            "\r\n"
        ).encode("utf-8")

        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)

        try:
            # Send M-SEARCH to multicast address
            sock.sendto(search_message, (SSDP_ADDR, SSDP_PORT))
            logger.debug(f"Sent SSDP M-SEARCH for {search_target}")

            # Collect responses
            loop = asyncio.get_event_loop()
            end_time = loop.time() + timeout

            while loop.time() < end_time:
                remaining = end_time - loop.time()
                if remaining <= 0:
                    break

                try:
                    await asyncio.wait_for(
                        self._receive_response(sock),
                        timeout=min(remaining, 1.0),
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.debug(f"Error receiving SSDP response: {e}")
                    continue

        finally:
            sock.close()

        # Fetch device descriptions
        raw_devices = list(self._devices.values())
        result = await self._fetch_device_descriptions(raw_devices)

        logger.debug(f"Discovered {len(result)} DLNA device(s)")
        return result

    async def _receive_response(self, sock: socket.socket) -> None:
        """Receive and parse a single SSDP response."""
        loop = asyncio.get_event_loop()

        # Use run_in_executor for non-blocking recv
        data, addr = await loop.run_in_executor(None, lambda: sock.recvfrom(4096))

        response = data.decode("utf-8", errors="ignore")
        self._parse_ssdp_response(response, addr[0])

    def _parse_ssdp_response(self, response: str, source_ip: str) -> None:
        """Parse SSDP response headers."""
        headers: dict[str, str] = {}
        lines = response.split("\r\n")

        for line in lines[1:]:  # Skip first line (HTTP status)
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.upper().strip()] = value.strip()

        location = headers.get("LOCATION", "")
        usn = headers.get("USN", "")

        if not location or not usn:
            return

        # Extract port from location URL
        try:
            parsed = urlparse(location)
            port = parsed.port or 80
            ip = parsed.hostname or source_ip
        except Exception:
            port = 80
            ip = source_ip

        # Use USN as unique key (avoid duplicates)
        if usn not in self._devices:
            device = _RawDevice(
                location=location,
                usn=usn,
                ip=ip,
                port=port,
            )
            self._devices[usn] = device
            logger.debug(f"Found device: {location}")

    async def _fetch_device_descriptions(
        self, raw_devices: list["_RawDevice"]
    ) -> list[DiscoveredDevice]:
        """Fetch device description XML for all devices."""
        result: list[DiscoveredDevice] = []

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                tasks = [self._fetch_device_description(session, device) for device in raw_devices]
                fetched = await asyncio.gather(*tasks, return_exceptions=True)
                for item in fetched:
                    if isinstance(item, DiscoveredDevice):
                        result.append(item)

        except Exception as e:
            logger.error(f"Error fetching device descriptions: {e}")

        return result

    async def _fetch_device_description(
        self, session: aiohttp.ClientSession, raw_device: "_RawDevice"
    ) -> DiscoveredDevice | None:
        """Fetch and parse device description XML."""
        try:
            async with session.get(raw_device.location) as response:
                if response.status != 200:
                    return None

                xml_text = await response.text()
                return self._parse_device_description(raw_device, xml_text)

        except Exception as e:
            logger.debug(f"Error fetching description for {raw_device.location}: {e}")
            return None

    def _parse_device_description(
        self, raw_device: "_RawDevice", xml_text: str
    ) -> DiscoveredDevice | None:
        """Parse device description XML."""
        try:
            # Define namespace
            ns = {"upnp": "urn:schemas-upnp-org:device-1-0"}

            root = ET.fromstring(xml_text)

            # Find root device element
            device_elem = root.find(".//upnp:device", ns)
            if device_elem is None:
                # Try without namespace (some devices don't use it)
                device_elem = root.find(".//device")
            if device_elem is None:
                logger.debug("No device element found in XML")
                return None

            # Extract device info from root device
            def get_text(parent: ET.Element, elem_name: str) -> str:
                # Try with namespace
                elem = parent.find(f"upnp:{elem_name}", ns)
                if elem is None:
                    # Try without namespace
                    elem = parent.find(elem_name)
                return elem.text if elem is not None and elem.text else ""

            friendly_name = get_text(device_elem, "friendlyName")
            manufacturer = get_text(device_elem, "manufacturer")
            model_name = get_text(device_elem, "modelName")
            udn = get_text(device_elem, "UDN")

            device = DiscoveredDevice(
                friendly_name=friendly_name or f"Unknown ({raw_device.ip})",
                ip=raw_device.ip,
                port=raw_device.port,
                model_name=model_name,
                manufacturer=manufacturer,
                udn=udn,
                location=raw_device.location,
            )

            logger.debug(f"Parsed device: {device.friendly_name}")

            if self._on_device_found:
                self._on_device_found(device)

            return device

        except ET.ParseError as e:
            logger.debug(f"Error parsing device XML: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error parsing device description: {e}")
            return None

    def on_device_found(self, callback: Callable[[DiscoveredDevice], None]) -> None:
        """Register callback for when a device is found."""
        self._on_device_found = callback


@dataclass
class _RawDevice:
    """Internal raw device from SSDP response (before XML fetch)."""

    location: str
    usn: str
    ip: str
    port: int


async def discover_dlna_devices(timeout: float = 5.0) -> list[DiscoveredDevice]:
    """
    Convenience function to discover DLNA devices.

    Args:
        timeout: Discovery timeout in seconds

    Returns:
        List of DiscoveredDevice objects
    """
    discovery = DLNADiscovery()
    return await discovery.discover(timeout=timeout)


__all__ = ["DLNADiscovery", "DiscoveredDevice", "discover_dlna_devices"]
