"""Connect mDNS should advertise on the LAN NIC, not a VPN or Docker bridge."""

from unittest.mock import MagicMock

from qobuz_proxy.config import Config
from qobuz_proxy.connect import discovery as discovery_mod
from qobuz_proxy.connect.discovery import DiscoveryService, _lan_ipv4


def _adapter(name: str, ipv4: str) -> MagicMock:
    adapter = MagicMock()
    adapter.nice_name = name
    adapter.name = name
    ip = MagicMock()
    ip.ip = ipv4
    adapter.ips = [ip]
    return adapter


def _patch_nics(monkeypatch, *nics: tuple[str, str], route: str | None = None) -> None:
    adapters = [_adapter(name, ip) for name, ip in nics]
    monkeypatch.setattr(discovery_mod, "_iter_adapters", lambda: adapters)
    monkeypatch.setattr(discovery_mod, "_default_route_ipv4", lambda: route)


def test_lan_ipv4_skips_utun_and_loopback(monkeypatch) -> None:
    _patch_nics(
        monkeypatch,
        ("utun4", "10.8.0.2"),
        ("lo0", "127.0.0.1"),
        ("en0", "192.168.68.67"),
        route="10.8.0.2",
    )
    assert _lan_ipv4() == "192.168.68.67"


def test_lan_ipv4_skips_linux_docker_bridge_and_vpn(monkeypatch) -> None:
    _patch_nics(
        monkeypatch,
        ("docker0", "172.17.0.1"),
        ("br-abc123def", "172.18.0.1"),
        ("tun0", "10.8.0.2"),
        ("wg0", "10.7.0.2"),
        ("tailscale0", "100.64.1.2"),
        ("veth0", "172.19.0.2"),
        ("eth0", "192.168.1.10"),
        route="172.17.0.1",
    )
    assert _lan_ipv4() == "192.168.1.10"


def test_lan_ipv4_ignores_adapter_order_for_virtual_nics(monkeypatch) -> None:
    first = (
        ("docker0", "172.17.0.1"),
        ("eth0", "192.168.1.10"),
    )
    second = (
        ("eth0", "192.168.1.10"),
        ("docker0", "172.17.0.1"),
    )
    _patch_nics(monkeypatch, *first, route=None)
    assert _lan_ipv4() == "192.168.1.10"
    _patch_nics(monkeypatch, *second, route=None)
    assert _lan_ipv4() == "192.168.1.10"


def test_lan_ipv4_prefers_wifi_over_ethernet_without_route(monkeypatch) -> None:
    _patch_nics(
        monkeypatch,
        ("eth0", "192.168.1.10"),
        ("wlan0", "192.168.1.20"),
        route=None,
    )
    assert _lan_ipv4() == "192.168.1.20"
    _patch_nics(
        monkeypatch,
        ("wlan0", "192.168.1.20"),
        ("eth0", "192.168.1.10"),
        route=None,
    )
    assert _lan_ipv4() == "192.168.1.20"


def test_lan_ipv4_prefers_default_route_when_it_is_lan(monkeypatch) -> None:
    nics = (
        ("wlan0", "192.168.1.20"),
        ("eth0", "192.168.1.10"),
    )
    _patch_nics(monkeypatch, *nics, route="192.168.1.10")
    assert _lan_ipv4() == "192.168.1.10"
    _patch_nics(
        monkeypatch,
        ("eth0", "192.168.1.10"),
        ("wlan0", "192.168.1.20"),
        route="192.168.1.10",
    )
    assert _lan_ipv4() == "192.168.1.10"


def test_lan_ipv4_preferred_interface_can_pin_virtual_nic(monkeypatch) -> None:
    _patch_nics(
        monkeypatch,
        ("docker0", "172.17.0.1"),
        ("eth0", "192.168.1.10"),
        route=None,
    )
    assert _lan_ipv4(preferred="docker0") == "172.17.0.1"
    assert _lan_ipv4(preferred="192.168.1.10") == "192.168.1.10"


def test_discovery_uses_configured_mdns_interface(monkeypatch) -> None:
    _patch_nics(
        monkeypatch,
        ("docker0", "172.17.0.1"),
        ("eth0", "192.168.1.10"),
        route=None,
    )
    config = Config()
    config.server.mdns_interface = "docker0"
    service = DiscoveryService(config=config, app_id="app")
    assert service._get_local_ip() == "172.17.0.1"
