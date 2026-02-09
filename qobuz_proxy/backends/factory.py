"""
Backend factory and registry.

Provides factory methods to instantiate backends by type name.
"""

import logging
from typing import Optional

from qobuz_proxy.config import Config

from .base import AudioBackend
from .dlna import DLNABackend

logger = logging.getLogger(__name__)


class BackendNotFoundError(Exception):
    """Raised when requested backend type is not available."""

    pass


class BackendRegistry:
    """
    Registry of available backend types.

    Backends register themselves here with their type name.
    Factory uses this to instantiate backends.
    """

    _backends: dict[str, type[AudioBackend]] = {}

    @classmethod
    def register(cls, type_name: str, backend_class: type[AudioBackend]) -> None:
        """Register a backend class."""
        cls._backends[type_name] = backend_class
        logger.debug(f"Registered backend type: {type_name}")

    @classmethod
    def get(cls, type_name: str) -> Optional[type[AudioBackend]]:
        """Get backend class by type name."""
        return cls._backends.get(type_name)

    @classmethod
    def available_types(cls) -> list[str]:
        """Get list of registered backend type names."""
        return list(cls._backends.keys())


class BackendFactory:
    """
    Factory for creating audio backend instances.

    Usage:
        backend = await BackendFactory.create_from_config(config)
    """

    @classmethod
    async def create_from_config(cls, config: Config) -> AudioBackend:
        """Create a backend based on configuration."""
        backend_type = config.backend.type

        # Check if type is available
        backend_class = BackendRegistry.get(backend_type)
        if not backend_class:
            available = BackendRegistry.available_types()
            raise BackendNotFoundError(
                f"Backend type '{backend_type}' not available. " f"Available types: {available}"
            )

        # Dispatch to type-specific factory method
        if backend_type == "dlna":
            return await cls.create_dlna(
                ip=config.backend.dlna.ip,
                port=config.backend.dlna.port or 1400,
            )
        else:
            # Generic instantiation for registered backends
            return backend_class(name=f"{backend_type} Backend")

    @classmethod
    async def create_dlna(
        cls,
        ip: str,
        port: int = 1400,
        fixed_volume: bool = False,
        name: Optional[str] = None,
    ) -> AudioBackend:
        """
        Create a DLNA backend.

        Args:
            ip: DLNA device IP address
            port: DLNA device port (default 1400 for Sonos)
            fixed_volume: If True, ignore volume commands
            name: Display name (auto-detected if not provided)

        Returns:
            Connected DLNABackend instance

        Raises:
            BackendNotFoundError: If connection fails
        """
        backend = DLNABackend(
            ip=ip,
            port=port,
            fixed_volume=fixed_volume,
            name=name,
        )
        if await backend.connect():
            return backend
        raise BackendNotFoundError(f"Failed to connect to DLNA device at {ip}:{port}")

    @classmethod
    def list_available_backends(cls) -> list[str]:
        """List available backend types."""
        return BackendRegistry.available_types()


# Register backends
BackendRegistry.register("dlna", DLNABackend)
