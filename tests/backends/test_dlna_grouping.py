"""Tests for Sonos group join/unjoin and coordinator-only playback."""

from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.backends.dlna.backend import DLNABackend
from qobuz_proxy.backends.dlna.client import DLNAClient
from qobuz_proxy.backends.dlna.sonos_topology import SonosZoneMember
from qobuz_proxy.backends.types import BackendTrackMetadata
from qobuz_proxy.config import GroupMemberEndpoint


def _member(
    uuid: str, coordinator: str, *, invisible: bool = False, zone: str = ""
) -> SonosZoneMember:
    return SonosZoneMember(
        uuid=uuid,
        zone_name=zone or uuid,
        invisible=invisible,
        is_stereo_pair=False,
        coordinator_uuid=coordinator,
    )


def _metadata() -> BackendTrackMetadata:
    return BackendTrackMetadata(
        track_id="1",
        title="Song",
        artist="Artist",
        album="Album",
        duration_ms=180000,
        artwork_url="",
    )


class TestJoinUnjoinClient:
    async def test_join_group_sets_x_rincon_uri(self) -> None:
        client = DLNAClient("10.0.1.31")
        client.set_av_transport_uri = AsyncMock(return_value=True)  # type: ignore[method-assign]

        assert await client.join_group("RINCON_KITCHEN")
        client.set_av_transport_uri.assert_awaited_once_with("x-rincon:RINCON_KITCHEN", "")

    async def test_unjoin_sends_become_coordinator(self) -> None:
        client = DLNAClient("10.0.1.31")
        client.device_info = MagicMock(av_transport_url="http://10.0.1.31:1400/AVTransport/Control")
        client._soap_action = AsyncMock(return_value="<ok/>")  # type: ignore[method-assign]

        assert await client.unjoin()
        args = client._soap_action.await_args.args
        assert args[2] == "BecomeCoordinatorOfStandaloneGroup"

    def test_root_device_uid_strips_media_renderer_suffix(self) -> None:
        client = DLNAClient("10.0.1.30")
        client.device_info = MagicMock(udn="uuid:RINCON_KITCHEN_MR")
        assert client.root_device_uid() == "RINCON_KITCHEN"

    async def test_set_group_volume_uses_group_rendering_control(self) -> None:
        client = DLNAClient("10.0.1.30")
        client.device_info = MagicMock(
            group_rendering_control_url="http://10.0.1.30:1400/GroupRenderingControl/Control"
        )
        client._soap_action = AsyncMock(return_value="<ok/>")  # type: ignore[method-assign]

        assert await client.set_group_volume(40)
        args = client._soap_action.await_args.args
        assert args[2] == "SetGroupVolume"
        assert args[3]["DesiredVolume"] == "40"


class TestEnsureGrouped:
    async def test_joins_member_not_already_in_group(self) -> None:
        backend = DLNABackend(
            "10.0.1.30",
            group_members=[GroupMemberEndpoint(name="Dining", ip="10.0.1.31")],
        )
        backend._client = MagicMock()
        backend._client.root_device_uid.return_value = "RINCON_KITCHEN"
        backend._fetch_topology = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "RINCON_KITCHEN": _member("RINCON_KITCHEN", "RINCON_KITCHEN"),
                "RINCON_DINING": _member("RINCON_DINING", "RINCON_DINING"),
            }
        )
        member_client = MagicMock()
        member_client.connect = AsyncMock()
        member_client.disconnect = AsyncMock()
        member_client.root_device_uid.return_value = "RINCON_DINING"
        member_client.join_group = AsyncMock(return_value=True)

        with patch("qobuz_proxy.backends.dlna.backend.DLNAClient", return_value=member_client):
            await backend._ensure_grouped()

        member_client.join_group.assert_awaited_once_with("RINCON_KITCHEN")

    async def test_skips_already_grouped_member(self) -> None:
        backend = DLNABackend(
            "10.0.1.30",
            group_members=[GroupMemberEndpoint(name="Dining", ip="10.0.1.31")],
        )
        backend._client = MagicMock()
        backend._client.root_device_uid.return_value = "RINCON_KITCHEN"
        backend._fetch_topology = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "RINCON_KITCHEN": _member("RINCON_KITCHEN", "RINCON_KITCHEN"),
                "RINCON_DINING": _member("RINCON_DINING", "RINCON_KITCHEN"),
            }
        )
        member_client = MagicMock()
        member_client.connect = AsyncMock()
        member_client.disconnect = AsyncMock()
        member_client.root_device_uid.return_value = "RINCON_DINING"
        member_client.join_group = AsyncMock(return_value=True)

        with patch("qobuz_proxy.backends.dlna.backend.DLNAClient", return_value=member_client):
            await backend._ensure_grouped()

        member_client.join_group.assert_not_called()


class TestEnsureUngrouped:
    async def test_unjoins_when_in_multi_room_group(self) -> None:
        backend = DLNABackend("10.0.1.31")
        backend._client = MagicMock()
        backend._client.root_device_uid.return_value = "RINCON_DINING"
        backend._client.unjoin = AsyncMock(return_value=True)
        backend._fetch_topology = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "RINCON_KITCHEN": _member("RINCON_KITCHEN", "RINCON_KITCHEN"),
                "RINCON_DINING": _member("RINCON_DINING", "RINCON_KITCHEN"),
            }
        )

        await backend._ensure_ungrouped()

        backend._client.unjoin.assert_awaited_once()

    async def test_does_not_unjoin_standalone_or_stereo_pair(self) -> None:
        backend = DLNABackend("10.0.1.30")
        backend._client = MagicMock()
        backend._client.root_device_uid.return_value = "RINCON_KITCHEN"
        backend._client.unjoin = AsyncMock(return_value=True)
        backend._fetch_topology = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "RINCON_KITCHEN": _member("RINCON_KITCHEN", "RINCON_KITCHEN"),
                "RINCON_KITCHEN_RF": _member("RINCON_KITCHEN_RF", "RINCON_KITCHEN", invisible=True),
            }
        )

        await backend._ensure_ungrouped()

        backend._client.unjoin.assert_not_called()


class TestGroupPlayPath:
    async def test_group_play_joins_then_queues_only_coordinator(self) -> None:
        backend = DLNABackend(
            "10.0.1.30",
            group_members=[GroupMemberEndpoint(name="Dining", ip="10.0.1.31")],
        )
        backend._client = MagicMock()
        backend._is_sonos = True
        backend._proxy_server = None
        backend._ensure_grouped = AsyncMock()  # type: ignore[method-assign]
        backend._ensure_ungrouped = AsyncMock()  # type: ignore[method-assign]
        backend._play_via_queue = AsyncMock(return_value=True)  # type: ignore[method-assign]
        backend._play_via_transport = AsyncMock(return_value=True)  # type: ignore[method-assign]
        backend._notify_state_change = MagicMock()  # type: ignore[method-assign]
        backend._build_didl = MagicMock(return_value="<didl/>")  # type: ignore[method-assign]

        await backend._play("http://qobuz/track.flac", _metadata())

        backend._ensure_grouped.assert_awaited_once()
        backend._ensure_ungrouped.assert_not_called()
        backend._play_via_queue.assert_awaited_once()
        backend._play_via_transport.assert_not_called()

    async def test_individual_sonos_play_unjoins_first(self) -> None:
        backend = DLNABackend("10.0.1.31")
        backend._client = MagicMock()
        backend._is_sonos = True
        backend._proxy_server = None
        backend._ensure_grouped = AsyncMock()  # type: ignore[method-assign]
        backend._ensure_ungrouped = AsyncMock()  # type: ignore[method-assign]
        backend._play_via_queue = AsyncMock(return_value=True)  # type: ignore[method-assign]
        backend._notify_state_change = MagicMock()  # type: ignore[method-assign]
        backend._build_didl = MagicMock(return_value="<didl/>")  # type: ignore[method-assign]

        await backend._play("http://qobuz/track.flac", _metadata())

        backend._ensure_ungrouped.assert_awaited_once()
        backend._ensure_grouped.assert_not_called()
        backend._play_via_queue.assert_awaited_once()
