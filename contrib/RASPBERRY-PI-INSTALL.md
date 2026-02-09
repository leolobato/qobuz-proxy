# QobuzProxy Raspberry Pi Installation Guide

This guide covers installing QobuzProxy on a Raspberry Pi running Raspberry Pi OS.

## Prerequisites

- Raspberry Pi 3B+, 4, or Zero 2 W (ARMv7 or newer)
- Raspberry Pi OS (Bullseye or newer)
- Python 3.10 or newer
- Network connection
- A DLNA-compatible speaker/receiver on your network

## Step 1: Install System Dependencies

```bash
# Update package list
sudo apt update

# Install Python and venv
sudo apt install -y python3-pip python3-venv

# Optional: Install git if you want to clone the repo
sudo apt install -y git
```

## Step 2: Create Installation Directory

```bash
# Create directory for QobuzProxy
sudo mkdir -p /opt/qobuz-proxy
sudo chown pi:pi /opt/qobuz-proxy
```

## Step 3: Create Virtual Environment

```bash
# Create virtual environment
python3 -m venv /opt/qobuz-proxy

# Activate it
source /opt/qobuz-proxy/bin/activate

# Upgrade pip
pip install --upgrade pip
```

## Step 4: Install QobuzProxy

### Option A: Install from PyPI (when published)

```bash
pip install qobuz-proxy
```

### Option B: Install from Source

```bash
# Clone the repository
git clone https://github.com/your-repo/qobuz-proxy.git /tmp/qobuz-proxy

# Install
pip install /tmp/qobuz-proxy

# Clean up
rm -rf /tmp/qobuz-proxy
```

### Option C: Install from Local Copy

```bash
# Copy files to Pi (from your development machine)
scp -r qobuz-proxy/ pi@raspberrypi:/tmp/

# On the Pi
pip install /tmp/qobuz-proxy
rm -rf /tmp/qobuz-proxy
```

## Step 5: Create Configuration Directory

```bash
# Create config directory
sudo mkdir -p /etc/qobuz-proxy

# Create config file
sudo nano /etc/qobuz-proxy/config.yaml
```

Add your configuration (see example below).

```bash
# Set proper permissions (readable only by owner)
sudo chmod 600 /etc/qobuz-proxy/config.yaml
sudo chown pi:pi /etc/qobuz-proxy/config.yaml
```

## Step 6: Example Configuration

Create `/etc/qobuz-proxy/config.yaml`:

```yaml
# QobuzProxy Configuration for Raspberry Pi

qobuz:
  email: "your-email@example.com"
  password: "your-password"
  max_quality: 27  # 5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k

device:
  name: "Pi Music Player"  # Name shown in Qobuz app
  # uuid: auto-generated if not specified

backend:
  type: "dlna"
  dlna:
    ip: "192.168.1.50"     # Your DLNA speaker's IP
    port: 1400             # Default for Sonos
    fixed_volume: false

server:
  http_port: 8689
  proxy_port: 7120
  bind_address: "0.0.0.0"

logging:
  level: "info"  # Use "debug" for troubleshooting
```

## Step 7: Test the Installation

Before setting up the service, test that QobuzProxy works:

```bash
# Activate virtual environment
source /opt/qobuz-proxy/bin/activate

# Run manually
qobuz-proxy --config /etc/qobuz-proxy/config.yaml
```

You should see:
- "Authentication successful"
- "Connected to DLNA device: [Your Speaker]"
- "device 'Pi Music Player' is now visible in Qobuz app"

Press Ctrl+C to stop.

## Step 8: Install systemd Service

```bash
# Copy the service file (from the qobuz-proxy repo)
sudo cp contrib/qobuz-proxy.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable qobuz-proxy

# Start the service
sudo systemctl start qobuz-proxy
```

## Step 9: Verify Service is Running

```bash
# Check status
sudo systemctl status qobuz-proxy

# View logs
journalctl -u qobuz-proxy -f
```

## Managing the Service

```bash
# Stop the service
sudo systemctl stop qobuz-proxy

# Restart the service
sudo systemctl restart qobuz-proxy

# Disable auto-start on boot
sudo systemctl disable qobuz-proxy

# View recent logs
journalctl -u qobuz-proxy --since "10 minutes ago"
```

## Troubleshooting

### Service fails to start

1. Check logs: `journalctl -u qobuz-proxy -e`
2. Test manually: `source /opt/qobuz-proxy/bin/activate && qobuz-proxy --config /etc/qobuz-proxy/config.yaml --log-level debug`

### Device not appearing in Qobuz app

1. Ensure Pi and phone are on the same network
2. Check that mDNS/Avahi is working: `avahi-browse -a`
3. Verify port 8689 is not blocked by firewall

### Authentication errors

1. Verify email and password in config
2. Check you have an active Qobuz subscription
3. Try logging in via web browser to ensure account is active

### DLNA device not responding

1. Verify DLNA device IP is correct
2. Check DLNA device is powered on
3. Try pinging the device: `ping 192.168.1.50`
4. Some devices use different ports (Sonos: 1400, others may vary)

### High CPU usage

1. This might indicate audio decoding issues
2. Check if the DLNA device supports the audio format
3. Try reducing `max_quality` to 6 (CD quality)

## Updating QobuzProxy

```bash
# Stop service
sudo systemctl stop qobuz-proxy

# Activate venv
source /opt/qobuz-proxy/bin/activate

# Upgrade
pip install --upgrade qobuz-proxy

# Restart service
sudo systemctl start qobuz-proxy
```

## Uninstalling

```bash
# Stop and disable service
sudo systemctl stop qobuz-proxy
sudo systemctl disable qobuz-proxy
sudo rm /etc/systemd/system/qobuz-proxy.service
sudo systemctl daemon-reload

# Remove installation
sudo rm -rf /opt/qobuz-proxy
sudo rm -rf /etc/qobuz-proxy
```
