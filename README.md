# QobuzProxy

A bridge between Qobuz Connect and DLNA speakers. Also supports local audio playback.

## Why?

Qobuz has a "Connect" feature (similar to Spotify Connect) that lets you control playback on supported devices from their app. Unfortunately, many popular speakers — most notably **Sonos** — don't support Qobuz Connect natively. This means you can't pick a Sonos speaker as a playback target in the Qobuz app, even though Sonos fully supports DLNA/UPnP streaming.

QobuzProxy solves this by acting as a virtual Qobuz Connect device on your network. When you open the Qobuz app, QobuzProxy shows up as a selectable speaker. When you play music, it receives the stream from Qobuz and forwards it to your DLNA-compatible speaker (like Sonos), preserving hi-res audio quality.

**In short:** Run QobuzProxy on a Raspberry Pi (or Docker or any always-on machine) on your local network, and your Sonos speakers become fully controllable Qobuz Connect targets — play, pause, skip, and adjust volume, all from the official Qobuz app.

## Features

- Appears as a Qobuz Connect device in the official Qobuz app
- Streams audio to DLNA renderers (Sonos, Denon HEOS, etc.)
- Local audio playback via PortAudio (play directly through your machine's speakers/DAC)
- Auto-detects device capabilities to select optimal audio quality
- Runs on Raspberry Pi, Docker, or any Linux/macOS system

## Installation

```bash
pip install qobuz-proxy

# For local audio playback support (optional)
pip install qobuz-proxy[local]
```

## Quick Start

### 1. Find Your DLNA Renderer

Use the built-in discovery tool to find DLNA devices on your network:

```bash
qobuz-proxy --discover
```

Example output:
```
Scanning for DLNA renderers (3.0s timeout)...

Found 2 DLNA renderer(s):

  Living Room Sonos
    IP: 192.168.1.50
    Port: 1400
    Model: Sonos Play:5
    Manufacturer: Sonos, Inc.

  Bedroom HEOS
    IP: 192.168.1.51
    Port: 60006
    Model: Denon HEOS 1
    Manufacturer: Denon

Config example (add to config.yaml):
  backend:
    dlna:
      ip: "192.168.1.50"
      port: 1400
```

Options:
- `--timeout 10` - Increase discovery timeout (default: 3 seconds)
- `--json` - Output as JSON for scripting

### 2. Start the Proxy

```bash
qobuz-proxy --email your@email.com --password yourpassword --dlna-ip 192.168.1.50
```

### Audio Quality

By default (`max_quality: auto`), QobuzProxy queries your DLNA device's capabilities and automatically selects the best supported quality. You can also set a specific quality level:

| Value | Format |
|-------|--------|
| `auto` | Auto-detect from device (recommended) |
| `5` | MP3 320 kbps |
| `6` | FLAC CD (16-bit/44.1kHz) |
| `7` | FLAC Hi-Res (24-bit/96kHz) |
| `27` | FLAC Hi-Res (24-bit/192kHz) |

```yaml
qobuz:
  max_quality: auto  # or 5, 6, 7, 27
```

Or with a config file:

```bash
qobuz-proxy --config config.yaml
```

## Local Audio Playback

QobuzProxy can also play audio directly through your machine's speakers or DAC, without needing a DLNA device. This requires the `local` extra dependencies:

```bash
pip install qobuz-proxy[local]
```

Then start with the `--backend-type local` flag:

```bash
qobuz-proxy --email your@email.com --password yourpassword --backend-type local
```

You can list available audio devices and select a specific one (e.g. a USB DAC):

```bash
qobuz-proxy --list-audio-devices
qobuz-proxy --backend-type local --audio-device "USB DAC" --email ...
```

## Docker Deployment

### Quick Start

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   nano .env  # Edit with your values
   ```

2. Build and run:
   ```bash
   docker-compose up -d
   ```

3. View logs:
   ```bash
   docker-compose logs -f
   ```

### Network Requirements

**Important**: QobuzProxy requires `network_mode: host` for mDNS discovery to work. This allows the Qobuz app to find the device on your local network.

If you cannot use host networking, consider:
- Using a macvlan network with a dedicated IP on your LAN
- Running QobuzProxy directly on the host (not in Docker)

### Configuration

Configuration can be provided via:

1. **Environment variables** (recommended for Docker):
   Set variables in `.env` file or directly in `docker-compose.yaml`

2. **Config file** (mounted volume):
   ```yaml
   volumes:
     - ./config.yaml:/app/config.yaml:ro
   ```

3. **Credentials cache** (optional, persists Qobuz app credentials):
   ```yaml
   volumes:
     - /path/to/cache:/home/qobuzproxy/.qobuz-proxy
   ```
   This caches the Qobuz web player credentials so they don't need to be re-scraped on each restart. The container runs as user `qobuzproxy`, so the path is `/home/qobuzproxy/.qobuz-proxy` (not `/root/.qobuz-proxy`).

### Ports

| Port | Purpose |
|------|---------|
| 8689 | HTTP server for mDNS discovery |
| 7120 | Audio proxy for DLNA streaming |

With `network_mode: host`, these ports are exposed directly on the host.

### Health Check

The container includes a health check that verifies the HTTP server is responding:
```bash
docker inspect --format='{{.State.Health.Status}}' qobuz-proxy
```

## Development

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Include local audio backend
pip install -e ".[dev,local]"

# Run
qobuz-proxy --help

# Test
pytest

# Code quality
black qobuz_proxy/ tests/
ruff check qobuz_proxy/ tests/
mypy qobuz_proxy/
```

## Acknowledgments

This project is based on the Qobuz Connect reverse-engineering work done by [Tobias Guyer](https://github.com/tobiasguyer) in [StreamCore32](https://github.com/tobiasguyer/StreamCore32). Thanks to his efforts in figuring out the Qobuz Connect protocol, this project was possible.

## Disclaimer

This project was built almost entirely through agentic programming using [Claude Code](https://claude.ai/claude-code). The architecture, implementation, and tests were generated through AI-assisted development with human guidance and review.

## License

MIT
