"""Connect mDNS should advertise on the LAN NIC, not a VPN tunnel."""

from unittest.mock import MagicMock

from qobuz_proxy.connect.discovery import _lan_ipv4


def test_lan_ipv4_skips_utun_and_loopback(monkeypatch) -> None:
    utun = MagicMock()
    utun.nice_name = "utun4"
    utun.name = "utun4"
    utun_ip = MagicMock()
    utun_ip.ip = "10.8.0.2"
    utun.ips = [utun_ip]

    lo = MagicMock()
    lo.nice_name = "lo0"
    lo.name = "lo0"
    lo_ip = MagicMock()
    lo_ip.ip = "127.0.0.1"
    lo.ips = [lo_ip]

    wifi = MagicMock()
    wifi.nice_name = "en0"
    wifi.name = "en0"
    wifi_ip = MagicMock()
    wifi_ip.ip = "192.168.68.67"
    wifi.ips = [wifi_ip]

    monkeypatch.setattr(
        "ifaddr.get_adapters", lambda: [utun, lo, wifi], raising=False
    )
    import ifaddr

    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [utun, lo, wifi])
    assert _lan_ipv4() == "192.168.68.67"
