# QobuzProxy Docker Image
# Headless Qobuz music player service with DLNA support

FROM python:3.11-slim

# Labels
LABEL org.opencontainers.image.title="QobuzProxy"
LABEL org.opencontainers.image.description="Headless Qobuz music player with DLNA support"

# Install system dependencies
# - curl: for health check
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN useradd --create-home --shell /bin/bash qobuzproxy

# Set working directory
WORKDIR /app

# Copy package files
COPY pyproject.toml README.md ./
COPY qobuz_proxy/ ./qobuz_proxy/
COPY protos/ ./protos/

# Install package
RUN pip install --no-cache-dir .

# Switch to non-root user
USER qobuzproxy

# Expose ports (documentation only - host networking bypasses this)
# 8689: HTTP server for mDNS discovery endpoints
# 7120: Audio proxy server for DLNA streaming
EXPOSE 8689 7120

# Health check - verify HTTP server is responding
# Uses QOBUZPROXY_HTTP_PORT env var if set, otherwise defaults to 8689
# Note: With host networking, this checks localhost
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${QOBUZPROXY_HTTP_PORT:-8689}/streamcore/get-display-info || exit 1

# Default command
CMD ["qobuz-proxy"]
