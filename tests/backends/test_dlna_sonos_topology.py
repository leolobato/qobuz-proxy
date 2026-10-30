"""Tests for Sonos zone group topology enrichment of DLNA discovery."""

import socket
from xml.sax.saxutils import escape

from aiohttp import web

from qobuz_proxy.backends.dlna.discovery import DiscoveredDevice
from qobuz_proxy.backends.dlna.sonos_topology import (
    enrich_discovered_devices,
    fetch_sonos_topology,
    parse_zone_group_state,
    sonos_groups_from_topology,
)

# Modeled on a real GetZoneGroupState response (S2 firmware 95.1-78010):
# a bonded stereo pair (Kitchen), a standalone room (Bedroom), a home
# theater with a satellite (Den), and an invisible zone bridge (Boost).
ZONE_GROUP_STATE = """<ZoneGroupState><ZoneGroups>
<ZoneGroup Coordinator="RINCON_KITCHEN_LF" ID="RINCON_KITCHEN_LF:1">
  <ZoneGroupMember UUID="RINCON_KITCHEN_LF"
    Location="http://10.0.1.30:1400/xml/device_description.xml"
    ZoneName="Kitchen"
    ChannelMapSet="RINCON_KITCHEN_LF:LF,LF;RINCON_KITCHEN_RF:RF,RF"/>
  <ZoneGroupMember UUID="RINCON_KITCHEN_RF"
    Location="http://10.0.1.33:1400/xml/device_description.xml"
    ZoneName="Kitchen" Invisible="1"
    ChannelMapSet="RINCON_KITCHEN_LF:LF,LF;RINCON_KITCHEN_RF:RF,RF"/>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_BEDROOM" ID="RINCON_BEDROOM:1">
  <ZoneGroupMember UUID="RINCON_BEDROOM"
    Location="http://10.0.1.31:1400/xml/device_description.xml"
    ZoneName="Bedroom"/>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_DEN_BAR" ID="RINCON_DEN_BAR:1">
  <ZoneGroupMember UUID="RINCON_DEN_BAR"
    Location="http://10.0.1.50:1400/xml/device_description.xml"
    ZoneName="Den"
    HTSatChanMapSet="RINCON_DEN_BAR:LF,RF;RINCON_DEN_SUB:SW">
    <Satellite UUID="RINCON_DEN_SUB"
      Location="http://10.0.1.51:1400/xml/device_description.xml"
      ZoneName="Den" Invisible="1"
      HTSatChanMapSet="RINCON_DEN_BAR:LF,RF;RINCON_DEN_SUB:SW"/>
  </ZoneGroupMember>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_BOOST" ID="RINCON_BOOST:1">
  <ZoneGroupMember UUID="RINCON_BOOST"
    Location="http://10.0.1.133:1400/xml/device_description.xml"
    ZoneName="Boost" Invisible="1" IsZoneBridge="1"/>
</ZoneGroup>
</ZoneGroups></ZoneGroupState>"""

# Kitchen (stereo pair coordinator) grouped with Bedroom.
MULTI_ROOM_ZONE_GROUP_STATE = """<ZoneGroupState><ZoneGroups>
<ZoneGroup Coordinator="RINCON_KITCHEN_LF" ID="RINCON_KITCHEN_LF:2">
  <ZoneGroupMember UUID="RINCON_KITCHEN_LF"
    Location="http://10.0.1.30:1400/xml/device_description.xml"
    ZoneName="Kitchen"
    ChannelMapSet="RINCON_KITCHEN_LF:LF,LF;RINCON_KITCHEN_RF:RF,RF"/>
  <ZoneGroupMember UUID="RINCON_KITCHEN_RF"
    Location="http://10.0.1.33:1400/xml/device_description.xml"
    ZoneName="Kitchen" Invisible="1"
    ChannelMapSet="RINCON_KITCHEN_LF:LF,LF;RINCON_KITCHEN_RF:RF,RF"/>
  <ZoneGroupMember UUID="RINCON_BEDROOM"
    Location="http://10.0.1.31:1400/xml/device_description.xml"
    ZoneName="Bedroom"/>
</ZoneGroup>
</ZoneGroups></ZoneGroupState>"""


def soap_response(zone_group_state: str) -> str:
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:GetZoneGroupStateResponse xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
        f"<ZoneGroupState>{escape(zone_group_state)}</ZoneGroupState>"
        "</u:GetZoneGroupStateResponse></s:Body></s:Envelope>"
    )


def sonos_device(ip: str, udn: str, port: int = 1400) -> DiscoveredDevice:
    return DiscoveredDevice(
        friendly_name=f"{ip} - Sonos One",
        ip=ip,
        port=port,
        model_name="Sonos One",
        manufacturer="Sonos, Inc.",
        udn=f"uuid:{udn}",
        location=f"http://{ip}:{port}/xml/device_description.xml",
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestParseZoneGroupState:
    def test_parses_all_members_including_satellites(self) -> None:
        topology = parse_zone_group_state(soap_response(ZONE_GROUP_STATE))

        assert topology is not None
        assert set(topology) == {
            "RINCON_KITCHEN_LF",
            "RINCON_KITCHEN_RF",
            "RINCON_BEDROOM",
            "RINCON_DEN_BAR",
            "RINCON_DEN_SUB",
            "RINCON_BOOST",
        }

    def test_stereo_pair_and_invisible_flags(self) -> None:
        topology = parse_zone_group_state(soap_response(ZONE_GROUP_STATE))

        assert topology is not None
        coordinator = topology["RINCON_KITCHEN_LF"]
        assert coordinator.zone_name == "Kitchen"
        assert coordinator.is_stereo_pair
        assert not coordinator.invisible

        pair_member = topology["RINCON_KITCHEN_RF"]
        assert pair_member.invisible

        standalone = topology["RINCON_BEDROOM"]
        assert standalone.zone_name == "Bedroom"
        assert not standalone.is_stereo_pair
        assert not standalone.invisible

    def test_home_theater_is_not_a_stereo_pair(self) -> None:
        topology = parse_zone_group_state(soap_response(ZONE_GROUP_STATE))

        assert topology is not None
        soundbar = topology["RINCON_DEN_BAR"]
        assert not soundbar.is_stereo_pair
        assert not soundbar.invisible
        satellite = topology["RINCON_DEN_SUB"]
        assert satellite.invisible

    def test_invalid_xml_returns_none(self) -> None:
        assert parse_zone_group_state("not xml at all") is None

    def test_members_keep_group_coordinator(self) -> None:
        topology = parse_zone_group_state(soap_response(ZONE_GROUP_STATE))

        assert topology is not None
        assert topology["RINCON_KITCHEN_LF"].coordinator_uuid == "RINCON_KITCHEN_LF"
        assert topology["RINCON_KITCHEN_RF"].coordinator_uuid == "RINCON_KITCHEN_LF"
        assert topology["RINCON_BEDROOM"].coordinator_uuid == "RINCON_BEDROOM"

    def test_multi_room_group_shares_coordinator(self) -> None:
        topology = parse_zone_group_state(soap_response(MULTI_ROOM_ZONE_GROUP_STATE))

        assert topology is not None
        assert topology["RINCON_BEDROOM"].coordinator_uuid == "RINCON_KITCHEN_LF"
        groups = sonos_groups_from_topology(topology)
        assert len(groups) == 1
        assert groups[0].coordinator_uuid == "RINCON_KITCHEN_LF"
        assert set(groups[0].member_uuids) == {
            "RINCON_KITCHEN_LF",
            "RINCON_KITCHEN_RF",
            "RINCON_BEDROOM",
        }

    def test_empty_zone_group_state_returns_none(self) -> None:
        assert parse_zone_group_state(soap_response("")) is None
        # Sonos removed /status/topology in recent S2 firmware; an empty
        # ZoneGroupState element must not crash either
        assert parse_zone_group_state(soap_response("<ZoneGroupState/>")) is None


class TestEnrichDiscoveredDevices:
    def _topology(self) -> dict:
        topology = parse_zone_group_state(soap_response(ZONE_GROUP_STATE))
        assert topology is not None
        return topology

    def test_drops_invisible_members(self) -> None:
        devices = [
            sonos_device("10.0.1.30", "RINCON_KITCHEN_LF"),
            sonos_device("10.0.1.33", "RINCON_KITCHEN_RF"),
            sonos_device("10.0.1.31", "RINCON_BEDROOM"),
        ]

        result = enrich_discovered_devices(devices, self._topology())

        assert [d.ip for d in result] == ["10.0.1.30", "10.0.1.31"]

    def test_renames_to_zone_name_with_pair_tag(self) -> None:
        devices = [
            sonos_device("10.0.1.30", "RINCON_KITCHEN_LF"),
            sonos_device("10.0.1.31", "RINCON_BEDROOM"),
        ]

        result = enrich_discovered_devices(devices, self._topology())

        assert result[0].friendly_name == "Kitchen (stereo pair)"
        assert result[1].friendly_name == "Bedroom"

    def test_non_sonos_devices_pass_through_unchanged(self) -> None:
        denon = DiscoveredDevice(
            friendly_name="Denon AVR-X1700H",
            ip="10.0.1.99",
            port=8080,
            model_name="AVR-X1700H",
            manufacturer="Denon",
            udn="uuid:5f9ec1b3-ff59-19bb-8530-0005cd1a2b3c",
        )

        result = enrich_discovered_devices([denon], self._topology())

        assert result == [denon]
        assert result[0].friendly_name == "Denon AVR-X1700H"


class TestFetchSonosTopology:
    async def test_no_sonos_devices_returns_none(self) -> None:
        denon = DiscoveredDevice(
            friendly_name="Denon AVR-X1700H",
            ip="10.0.1.99",
            port=8080,
            manufacturer="Denon",
        )

        assert await fetch_sonos_topology([denon]) is None

    async def test_fetches_from_first_answering_device(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            assert "GetZoneGroupState" in request.headers.get("SOAPACTION", "")
            return web.Response(text=soap_response(ZONE_GROUP_STATE))

        app = web.Application()
        app.router.add_post("/ZoneGroupTopology/Control", handler)
        port = _free_port()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()

        try:
            dead_port = _free_port()  # nothing listening: first device fails
            devices = [
                sonos_device("127.0.0.1", "RINCON_KITCHEN_RF", port=dead_port),
                sonos_device("127.0.0.1", "RINCON_KITCHEN_LF", port=port),
            ]

            topology = await fetch_sonos_topology(devices, timeout=2.0)

            assert topology is not None
            assert topology["RINCON_KITCHEN_LF"].zone_name == "Kitchen"
        finally:
            await runner.cleanup()

    async def test_unreachable_devices_return_none(self) -> None:
        devices = [sonos_device("127.0.0.1", "RINCON_KITCHEN_LF", port=_free_port())]

        assert await fetch_sonos_topology(devices, timeout=2.0) is None
