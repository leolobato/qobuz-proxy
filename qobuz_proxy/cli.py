"""
QobuzProxy CLI entry point.

Provides command-line interface for running QobuzProxy.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from qobuz_proxy import __version__
from qobuz_proxy.config import Config, ConfigError, load_config, AUTO_QUALITY
from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.auth import AuthenticationError
from qobuz_proxy.backends import BackendNotFoundError

logger = logging.getLogger(__name__)

# Exit codes
EXIT_SUCCESS = 0
EXIT_CONFIG_ERROR = 1
EXIT_AUTH_ERROR = 2
EXIT_NETWORK_ERROR = 3


def setup_logging(level: str = "info") -> None:
    """Configure logging to stdout."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _parse_quality(value: str) -> int:
    """Parse quality argument, handling 'auto' and numeric values."""
    if value.lower() == "auto":
        return AUTO_QUALITY
    try:
        v = int(value)
        if v not in {5, 6, 7, 27}:
            raise argparse.ArgumentTypeError(f"Invalid quality: {v}. Use 5, 6, 7, 27, or 'auto'")
        return v
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid quality: {value}. Use 5, 6, 7, 27, or 'auto'")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="qobuz-proxy",
        description="Headless Qobuz music player service with DLNA support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  qobuz-proxy --discover
  qobuz-proxy --discover --timeout 10 --json
  qobuz-proxy --config config.yaml
  qobuz-proxy --email user@example.com --password secret --dlna-ip 192.168.1.50

Environment Variables:
  QOBUZ_EMAIL, QOBUZ_PASSWORD, QOBUZ_MAX_QUALITY
  QOBUZPROXY_DEVICE_NAME, QOBUZPROXY_DLNA_IP, QOBUZPROXY_DLNA_PORT
  QOBUZPROXY_HTTP_PORT, QOBUZPROXY_PROXY_PORT, QOBUZPROXY_LOG_LEVEL
""",
    )

    # General
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Discovery mode
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Scan network for DLNA renderers and exit",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="Discovery timeout in seconds (used with --discover)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON (used with --discover)",
    )

    # Configuration
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("./config.yaml"),
        metavar="PATH",
        help="Path to config file (default: ./config.yaml)",
    )

    # Authentication
    auth_group = parser.add_argument_group("Authentication")
    auth_group.add_argument(
        "--email",
        metavar="TEXT",
        help="Qobuz account email",
    )
    auth_group.add_argument(
        "--password",
        metavar="TEXT",
        help="Qobuz account password",
    )
    auth_group.add_argument(
        "--max-quality",
        type=_parse_quality,
        metavar="INT|auto",
        help="Audio quality (5=MP3, 6=CD, 7=96k, 27=192k, auto=detect)",
    )

    # Device
    device_group = parser.add_argument_group("Device")
    device_group.add_argument(
        "--name",
        metavar="TEXT",
        help="Device name shown in Qobuz app",
    )
    device_group.add_argument(
        "--uuid",
        metavar="TEXT",
        help="Device UUID (auto-generated if omitted)",
    )

    # DLNA Backend
    dlna_group = parser.add_argument_group("DLNA Backend")
    dlna_group.add_argument(
        "--dlna-ip",
        metavar="TEXT",
        help="DLNA renderer IP address",
    )
    dlna_group.add_argument(
        "--dlna-port",
        type=int,
        metavar="INT",
        help="DLNA renderer port (default: 1400)",
    )
    dlna_group.add_argument(
        "--fixed-volume",
        action="store_true",
        help="Ignore volume commands (for external amp control)",
    )

    # Server
    server_group = parser.add_argument_group("Server")
    server_group.add_argument(
        "--http-port",
        type=int,
        metavar="INT",
        help="HTTP server port (default: 8689)",
    )
    server_group.add_argument(
        "--proxy-port",
        type=int,
        metavar="INT",
        help="Audio proxy port (default: 7120)",
    )
    server_group.add_argument(
        "--bind",
        metavar="TEXT",
        help="Bind address (default: 0.0.0.0)",
    )

    # Logging
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        metavar="LEVEL",
        help="Log level: debug, info, warning, error",
    )

    return parser.parse_args()


def _set_nested(d: dict, path: tuple, value: Any) -> None:
    """Set a nested dictionary value."""
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def args_to_dict(args: argparse.Namespace) -> dict:
    """Convert argparse namespace to nested config dict."""
    result: dict = {}

    # Map CLI args to config paths
    mappings = {
        "email": ("qobuz", "email"),
        "password": ("qobuz", "password"),
        "max_quality": ("qobuz", "max_quality"),
        "name": ("device", "name"),
        "uuid": ("device", "uuid"),
        "dlna_ip": ("backend", "dlna", "ip"),
        "dlna_port": ("backend", "dlna", "port"),
        "fixed_volume": ("backend", "dlna", "fixed_volume"),
        "http_port": ("server", "http_port"),
        "proxy_port": ("server", "proxy_port"),
        "bind": ("server", "bind_address"),
        "log_level": ("logging", "level"),
    }

    for arg_name, path in mappings.items():
        value = getattr(args, arg_name, None)
        # Skip None values and False for fixed_volume (only set if explicitly True)
        if value is None:
            continue
        if arg_name == "fixed_volume" and not value:
            continue
        _set_nested(result, path, value)

    return result


def log_config(config: Config) -> None:
    """Log configuration summary (without sensitive data)."""
    logger.info(f"Device: {config.device.name} ({config.device.uuid[:8]}...)")
    logger.info(f"DLNA target: {config.backend.dlna.ip}:{config.backend.dlna.port}")
    logger.info(f"HTTP server: {config.server.bind_address}:{config.server.http_port}")
    logger.info(f"Proxy server: {config.server.bind_address}:{config.server.proxy_port}")
    logger.info(f"Max quality: {config.qobuz.max_quality}")
    if config.backend.dlna.fixed_volume:
        logger.info("Volume control: disabled (fixed_volume=true)")


async def run_discovery(timeout: float, json_output: bool) -> int:
    """
    Run DLNA device discovery.

    Args:
        timeout: Discovery timeout in seconds
        json_output: Output as JSON if True

    Returns:
        Exit code
    """
    from qobuz_proxy.backends.dlna.discovery import DLNADiscovery

    if not json_output:
        print(f"Scanning for DLNA renderers ({timeout}s timeout)...")

    discovery = DLNADiscovery()
    devices = await discovery.discover(timeout=timeout)

    if json_output:
        output = {
            "devices": [
                {
                    "name": d.friendly_name,
                    "ip": d.ip,
                    "port": d.port,
                    "model": d.model_name,
                    "manufacturer": d.manufacturer,
                    "udn": d.udn,
                    "location": d.location,
                }
                for d in devices
            ],
            "count": len(devices),
        }
        print(json.dumps(output, indent=2))
    else:
        if not devices:
            print("\nNo DLNA renderers found.")
            print("\nTroubleshooting tips:")
            print("  - Ensure your DLNA device is powered on and connected")
            print("  - Try increasing timeout with --timeout 10")
            print("  - Check that your device supports UPnP/DLNA")
            return EXIT_SUCCESS

        print(f"\nFound {len(devices)} DLNA renderer(s):\n")

        for d in devices:
            print(f"  {d.friendly_name}")
            print(f"    IP: {d.ip}")
            print(f"    Port: {d.port}")
            if d.model_name:
                print(f"    Model: {d.model_name}")
            if d.manufacturer:
                print(f"    Manufacturer: {d.manufacturer}")
            print()

        # Show config example using first device
        first = devices[0]
        print("Config example (add to config.yaml):")
        print("  backend:")
        print("    dlna:")
        print(f'      ip: "{first.ip}"')
        print(f"      port: {first.port}")

    return EXIT_SUCCESS


def run_serve(args: argparse.Namespace) -> int:
    """
    Run the proxy server.

    Args:
        args: Parsed arguments

    Returns:
        Exit code
    """
    # Setup basic logging first (will be reconfigured after config load)
    setup_logging("info")

    logger.info(f"QobuzProxy v{__version__}")

    try:
        # Load configuration
        cli_config = args_to_dict(args)
        config = load_config(args.config, cli_config)

        # Reconfigure logging with loaded level
        setup_logging(config.logging.level)

        log_config(config)

    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        return EXIT_CONFIG_ERROR

    # Run the application
    try:
        app = QobuzProxy(config)
        asyncio.run(app.run())
        return EXIT_SUCCESS

    except AuthenticationError as e:
        logger.error(f"Authentication failed: {e}")
        return EXIT_AUTH_ERROR

    except BackendNotFoundError as e:
        logger.error(f"Backend error: {e}")
        return EXIT_NETWORK_ERROR

    except (ConnectionError, OSError) as e:
        logger.error(f"Network error: {e}")
        return EXIT_NETWORK_ERROR

    except KeyboardInterrupt:
        logger.info("Interrupted")
        return EXIT_SUCCESS

    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return EXIT_NETWORK_ERROR


def main() -> int:
    """
    Main entry point.

    Returns:
        Exit code: 0=success, 1=config error, 2=auth error, 3=network error
    """
    args = parse_args()

    if args.discover:
        return asyncio.run(run_discovery(args.timeout, args.json_output))
    else:
        return run_serve(args)


if __name__ == "__main__":
    sys.exit(main())
