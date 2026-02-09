"""
QobuzProxy Configuration System.

Priority order (highest to lowest):
1. Command-line arguments
2. Environment variables
3. Configuration file (YAML)
4. Default values
"""

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


# Valid quality values (0 = auto-detect from device capabilities)
AUTO_QUALITY = 0
AUTO_FALLBACK_QUALITY = 6  # CD quality fallback when auto-detection fails
VALID_QUALITIES = {0, 5, 6, 7, 27}

# Valid log levels
VALID_LOG_LEVELS = {"debug", "info", "warning", "error"}

# Environment variable mappings
ENV_MAPPINGS = {
    # Qobuz
    "QOBUZ_EMAIL": ("qobuz", "email"),
    "QOBUZ_PASSWORD": ("qobuz", "password"),
    "QOBUZ_MAX_QUALITY": ("qobuz", "max_quality"),
    # Device
    "QOBUZPROXY_DEVICE_NAME": ("device", "name"),
    # DLNA
    "QOBUZPROXY_DLNA_IP": ("backend", "dlna", "ip"),
    "QOBUZPROXY_DLNA_PORT": ("backend", "dlna", "port"),
    "QOBUZPROXY_DLNA_FIXED_VOLUME": ("backend", "dlna", "fixed_volume"),
    # Server
    "QOBUZPROXY_HTTP_PORT": ("server", "http_port"),
    "QOBUZPROXY_PROXY_PORT": ("server", "proxy_port"),
    # Logging
    "QOBUZPROXY_LOG_LEVEL": ("logging", "level"),
}


class ConfigError(Exception):
    """Configuration error."""

    pass


@dataclass
class QobuzConfig:
    """Qobuz account configuration."""

    email: str = ""
    password: str = ""
    max_quality: int = 27  # 5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k


@dataclass
class DeviceConfig:
    """Device identification configuration."""

    name: str = "QobuzProxy"
    uuid: str = ""  # Auto-generated if empty

    def __post_init__(self) -> None:
        if not self.uuid:
            self.uuid = str(uuid.uuid4())


@dataclass
class DLNAConfig:
    """DLNA backend configuration."""

    ip: str = ""
    port: int = 1400
    fixed_volume: bool = False


@dataclass
class BackendConfig:
    """Audio backend configuration."""

    type: str = "dlna"
    dlna: DLNAConfig = field(default_factory=DLNAConfig)


@dataclass
class ServerConfig:
    """Server configuration."""

    http_port: int = 8689
    proxy_port: int = 7120
    bind_address: str = "0.0.0.0"


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "info"


@dataclass
class Config:
    """Complete QobuzProxy configuration."""

    qobuz: QobuzConfig = field(default_factory=QobuzConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def validate_email(email: str) -> bool:
    """Validate email format."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def validate_port(port: int) -> bool:
    """Validate port number."""
    return 1 <= port <= 65535


def validate_config(config: Config) -> None:
    """
    Validate configuration.

    Raises:
        ConfigError: If configuration is invalid
    """
    errors = []

    # Qobuz credentials
    if not config.qobuz.email:
        errors.append("Qobuz email is required")
    elif not validate_email(config.qobuz.email):
        errors.append(f"Invalid email format: {config.qobuz.email}")

    if not config.qobuz.password:
        errors.append("Qobuz password is required")

    if config.qobuz.max_quality not in VALID_QUALITIES:
        errors.append(
            f"Invalid max_quality: {config.qobuz.max_quality}. "
            f"Valid values: {sorted(VALID_QUALITIES)}"
        )

    # Backend
    if config.backend.type == "dlna":
        if not config.backend.dlna.ip:
            errors.append("DLNA IP address is required when backend type is 'dlna'")
        if not validate_port(config.backend.dlna.port):
            errors.append(f"Invalid DLNA port: {config.backend.dlna.port}")

    # Server ports
    if not validate_port(config.server.http_port):
        errors.append(f"Invalid HTTP port: {config.server.http_port}")
    if not validate_port(config.server.proxy_port):
        errors.append(f"Invalid proxy port: {config.server.proxy_port}")

    # Logging
    if config.logging.level.lower() not in VALID_LOG_LEVELS:
        errors.append(
            f"Invalid log level: {config.logging.level}. "
            f"Valid values: {sorted(VALID_LOG_LEVELS)}"
        )

    if errors:
        raise ConfigError("Configuration validation failed:\n  - " + "\n  - ".join(errors))


def load_yaml_config(path: Path) -> dict:
    """
    Load configuration from YAML file.

    Args:
        path: Path to YAML file

    Returns:
        Configuration dictionary

    Raises:
        ConfigError: If file cannot be read or parsed
    """
    if not path.exists():
        logger.debug(f"Config file not found: {path}")
        return {}

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
            return data if data else {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Error parsing YAML config: {e}")
    except IOError as e:
        raise ConfigError(f"Error reading config file: {e}")


def _set_nested(d: dict, path: tuple, value: Any) -> None:
    """Set a nested dictionary value using a path tuple."""
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def load_env_config() -> dict:
    """
    Load configuration from environment variables.

    Returns:
        Configuration dictionary with values from environment
    """
    result: dict = {}

    for env_var, path in ENV_MAPPINGS.items():
        value: Any = os.environ.get(env_var)
        if value is not None:
            # Handle max_quality specially to support "auto"
            if env_var == "QOBUZ_MAX_QUALITY":
                if value.lower() == "auto":
                    value = AUTO_QUALITY
                else:
                    try:
                        value = int(value)
                    except ValueError:
                        logger.warning(f"Invalid value for {env_var}: {value}")
                        continue
            # Convert other numeric values
            elif env_var in (
                "QOBUZPROXY_DLNA_PORT",
                "QOBUZPROXY_HTTP_PORT",
                "QOBUZPROXY_PROXY_PORT",
            ):
                try:
                    value = int(value)
                except ValueError:
                    logger.warning(f"Invalid integer for {env_var}: {value}")
                    continue
            # Convert boolean values
            elif env_var == "QOBUZPROXY_DLNA_FIXED_VOLUME":
                value = value.lower() in ("true", "1", "yes", "on")

            # Set nested value
            _set_nested(result, path, value)

    return result


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def merge_configs(*configs: dict) -> dict:
    """
    Deep merge multiple configuration dictionaries.
    Later configs override earlier ones.
    """
    result: dict = {}
    for config in configs:
        _deep_merge(result, config)
    return result


def dict_to_config(d: dict) -> Config:
    """Convert a dictionary to Config dataclass."""
    config = Config()

    # Qobuz
    if "qobuz" in d:
        q = d["qobuz"]
        config.qobuz.email = q.get("email", config.qobuz.email)
        config.qobuz.password = q.get("password", config.qobuz.password)
        max_quality = q.get("max_quality", config.qobuz.max_quality)
        # Handle "auto" string from YAML
        if isinstance(max_quality, str) and max_quality.lower() == "auto":
            config.qobuz.max_quality = AUTO_QUALITY
        else:
            config.qobuz.max_quality = max_quality

    # Device
    if "device" in d:
        dev = d["device"]
        config.device.name = dev.get("name", config.device.name)
        if dev.get("uuid"):
            config.device.uuid = dev["uuid"]

    # Backend
    if "backend" in d:
        b = d["backend"]
        config.backend.type = b.get("type", config.backend.type)
        if "dlna" in b:
            dlna = b["dlna"]
            config.backend.dlna.ip = dlna.get("ip", config.backend.dlna.ip)
            config.backend.dlna.port = dlna.get("port", config.backend.dlna.port)
            config.backend.dlna.fixed_volume = dlna.get(
                "fixed_volume", config.backend.dlna.fixed_volume
            )

    # Server
    if "server" in d:
        s = d["server"]
        config.server.http_port = s.get("http_port", config.server.http_port)
        config.server.proxy_port = s.get("proxy_port", config.server.proxy_port)
        config.server.bind_address = s.get("bind_address", config.server.bind_address)

    # Logging
    if "logging" in d:
        config.logging.level = d["logging"].get("level", config.logging.level)

    return config


def load_config(
    config_path: Optional[Path] = None,
    cli_args: Optional[dict] = None,
) -> Config:
    """
    Load configuration from all sources.

    Priority (highest to lowest):
    1. CLI arguments
    2. Environment variables
    3. Config file
    4. Defaults

    Args:
        config_path: Path to YAML config file
        cli_args: Dictionary of CLI arguments

    Returns:
        Merged Config object

    Raises:
        ConfigError: If configuration is invalid
    """
    # Start with empty dict (defaults come from dataclasses)
    configs = []

    # 1. Load from file (lowest priority of explicit configs)
    if config_path:
        file_config = load_yaml_config(config_path)
        if file_config:
            configs.append(file_config)
            logger.debug(f"Loaded config from {config_path}")

    # 2. Load from environment
    env_config = load_env_config()
    if env_config:
        configs.append(env_config)
        logger.debug("Loaded config from environment variables")

    # 3. Load from CLI (highest priority)
    if cli_args:
        configs.append(cli_args)
        logger.debug("Loaded config from CLI arguments")

    # Merge all configs
    merged = merge_configs(*configs) if configs else {}

    # Convert to Config object (fills in defaults)
    config = dict_to_config(merged)

    # Validate
    validate_config(config)

    return config
