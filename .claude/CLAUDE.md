# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QobuzProxy is a headless Qobuz music player that appears as a Qobuz Connect device, controllable from the official Qobuz app. It streams audio to DLNA renderers (Sonos, Denon HEOS, etc.).

## Commands

```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"

# Run
python3 -m qobuz_proxy
qobuz-proxy --config config.yaml
qobuz-proxy --discover                    # Find DLNA renderers

# Test
pytest                                     # All tests
pytest tests/connect/test_protocol.py      # Single file
pytest tests/connect/test_protocol.py::TestProtocol::test_method  # Single test

# Code quality
black qobuz_proxy/ tests/                 # Format (100 char line length)
ruff check qobuz_proxy/ tests/            # Lint
mypy qobuz_proxy/                         # Type check (strict)
```

**Always use `python3`**, never bare `python`.

## Protocol Buffer Compilation

Must be run before first use. Re-run if `.proto` files change:

```bash
protoc --python_out=qobuz_proxy/proto -I protos protos/*.proto

# Fix relative imports in generated files (macOS uses sed -i '', Linux uses sed -i)
sed -i'' -e 's/^import qconnect_common_pb2/from . import qconnect_common_pb2/g' qobuz_proxy/proto/qconnect_payload_pb2.py qobuz_proxy/proto/qconnect_queue_pb2.py
sed -i'' -e 's/^import qconnect_queue_pb2/from . import qconnect_queue_pb2/g' qobuz_proxy/proto/qconnect_payload_pb2.py
```

Proto files in `protos/`: `qconnect_common.proto`, `qconnect_envelope.proto`, `qconnect_payload.proto`, `qconnect_queue.proto`, `ws.proto`

## Architecture

### Component Wiring (app.py)

`QobuzProxy` in `app.py` is the main orchestrator. It wires together:

1. **Auth** (`auth/`): Scrapes Qobuz web player for app credentials (`credentials.py`), signs API requests with MD5 (`api_client.py`), manages session/JWT tokens (`tokens.py`)
2. **Connect** (`connect/`): Registers as mDNS device + HTTP discovery endpoints (`discovery.py`), manages WebSocket connection to Qobuz servers (`ws_manager.py`), encodes/decodes protobuf messages (`protocol.py`)
3. **Playback** (`playback/`): State machine player (`player.py`), queue management (`queue.py`), track metadata from Qobuz API (`metadata.py`), command handlers (`command_handler.py`, `queue_handler.py`, `volume_handler.py`), periodic state reporting to Qobuz app (`state_reporter.py`)
4. **Backend** (`backends/`): Abstract `AudioBackend` interface (`base.py`), DLNA implementation in `dlna/` subpackage with SOAP/UPnP client (`client.py`), device capability detection (`capabilities.py`), audio proxy server (`proxy_server.py`)

### Key Data Flows

**Qobuz app command → audio playback**: WebSocket message → `protocol.py` decodes → `ws_manager.py` dispatches to handler → `command_handler.py`/`queue_handler.py` → `player.py` state machine → `DLNABackend` → DLNA SOAP commands to device

**Audio streaming**: Qobuz CDN → `proxy_server.py` (aiohttp server on port 7120) → DLNA device. The proxy is needed because DLNA devices fetch audio via HTTP GET, and Qobuz streaming URLs require specific headers.

**State reporting**: `state_reporter.py` periodically builds state (playback state, position, queue) → `protocol.py` encodes to protobuf → WebSocket → Qobuz servers → Qobuz app UI

### Quality Auto-Detection

When `max_quality: auto`: DLNA `GetProtocolInfo` → `capabilities.py` parses Sink string → maps to Qobuz quality (27=Hi-Res 192k, 7=Hi-Res 96k, 6=CD, 5=MP3). Falls back to CD quality (6) on failure.

## Configuration Priority

1. CLI arguments (highest) → 2. Environment variables → 3. YAML config file → 4. Code defaults

Key env vars: `QOBUZ_EMAIL`, `QOBUZ_PASSWORD`, `QOBUZ_MAX_QUALITY`, `QOBUZPROXY_DEVICE_NAME`, `QOBUZPROXY_DLNA_IP`, `QOBUZPROXY_LOG_LEVEL`

## Code Style

- **Black** with 100 char line length, **Ruff** for linting, **mypy** strict
- Type hints required on all public functions, Google-style docstrings only for non-obvious APIs
- All I/O is async. No blocking calls in main event loop
- Never log passwords

## Testing

- `asyncio_mode = "auto"` in pyproject.toml — no `@pytest.mark.asyncio` decorators needed
- Tests mirror source structure in `tests/`

## Commit Convention

`feat(module):`, `fix(module):`, `refactor(module):`, `test(module):`, `docs:`

## Reference Materials

- **Protocol reference**: [StreamCore32](https://github.com/tobiasguyer/StreamCore32) (C++ ESP32 Qobuz Connect implementation)

## Debugging

### Reference Implementation

For Qobuz Connect protocol issues, the key files in [StreamCore32](https://github.com/tobiasguyer/StreamCore32) are:
- `stream/qobuz/src/QobuzPlayer.cpp` — Position tracking, state management
- `stream/qobuz/src/QobuzStream.cpp` — WebSocket message handling

### Position Tracking Data Flow

```
DLNA Device (GetPositionInfo SOAP → RelTime string)
  → DLNAClient.get_position_info() (parses to ms)
  → DLNABackend.get_position() (updates _position_ms, notifies callback)
  → Player._on_position_update() (sets _position_value_ms + _position_timestamp_ms)
  → StateReporter._build_state_report() (reads player._position_value_ms)
  → Protocol.encode_state_update() (encodes Position{timestamp, value})
  → WebSocket → Qobuz app
```

The protocol uses `Position { timestamp: fixed64, value: uint32 }`. The app interpolates: `value + (now - timestamp)`.

### Common Issues

1. **Position always 0**: DLNA device may not support `GetPositionInfo` for the current source
2. **State not updating**: `_playback_monitor_loop` only polls when `_state == PlaybackState.PLAYING`
3. **Protocol encoding**: Log values passed to `encode_state_update()` — binary issues are invisible otherwise

### Known Issues

**Qobuz app shows wrong quality** (FR-DLNA-08): App always displays "Hi-Res 96k" regardless of actual streaming quality. Audio streams correctly at auto-detected quality. Investigation needed: check if a protocol message reports quality capability, review StreamCore32 for quality reporting fields.

## Docker

Uses `network_mode: host` (required for mDNS). Ports: 8689 (HTTP discovery), 7120 (audio proxy). See `docker-compose.yaml` and `.env.example`.
