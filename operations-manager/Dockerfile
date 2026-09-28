# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

# Bootstrap: append the corporate CA directly onto the trust bundle that's
# already baked into the base image *before* touching the network. On some
# proxies apt's own HTTPS mirror fetch fails the same way apk's does, so we
# don't rely on `apt-get install ca-certificates` succeeding first - the
# bundle already trusts the proxy by the time apt (or pip) needs it.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN if find /usr/local/share/ca-certificates/extra -name '*.crt' 2>/dev/null | grep -q .; then \
      cat /usr/local/share/ca-certificates/extra/*.crt >> /etc/ssl/certs/ca-certificates.crt && \
      apt-get update && \
      apt-get install -y --no-install-recommends ca-certificates && \
      update-ca-certificates && \
      rm -rf /var/lib/apt/lists/*; \
    fi
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential openssl \
    && rm -rf /var/lib/apt/lists/*

# Fallback self-signed TLS server certificate so this container serves
# HTTPS out of the box with zero configuration (see docker-entrypoint.sh).
# For a real deployment, mount your own certificate over these same two
# paths (a docker-compose volume mount replaces these files, no image
# rebuild or code change needed) - see docker-compose.yml.
RUN mkdir -p /app/tls && \
    openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
      -keyout /app/tls/server.key -out /app/tls/server.crt \
      -subj "/CN=localhost" \
      -addext "subjectAltName=DNS:localhost,DNS:operations-manager,DNS:agent-operations-manager,IP:127.0.0.1" \
      2>/dev/null && \
    chmod 640 /app/tls/server.key

# Install the CPU-only build of torch first so pip doesn't pull in the
# default CUDA/cuDNN/Triton-enabled build when resolving
# sentence-transformers' dependency on torch - this container only runs a
# small embedding model on CPU.
# --default-timeout/--retries: this stack pulls a few large wheels
# (torch, numpy, sentence-transformers deps) and pip's default 15s
# read-timeout can trip on a slow or corporate-proxy-inspected
# connection mid-download, well before the transfer has actually
# stalled - it just retries silently instead of failing the build.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --default-timeout=100 --retries 5 \
    torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --default-timeout=100 --retries 5 -r requirements.txt

COPY config.py .
COPY app ./app
COPY sdk ./sdk
COPY skills ./skills

ENV TOKENIZERS_PARALLELISM=false

COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Run as an unprivileged user (CIS Docker Benchmark 4.1 / NIST SP 800-190 -
# containers shouldn't run application processes as root). uvicorn binds
# :8090, an unprivileged port, so nothing here needs root at runtime -
# everything that did (apt-get, the corp-CA trust bootstrap, cert
# generation, pip install) already happened above as root, at build time.
# Home dir matches HOME below so huggingface_hub/sentence-transformers
# resolve their model cache to the same path the hf-cache volume in
# docker-compose.yml mounts (.../home/appuser/.cache/huggingface, not
# /root/...) - keep those two in sync if either one ever moves.
# appuser is added as a *supplementary* member of the root group (gid 0,
# not primary - its primary group stays appuser/10001) purely so it can
# hand out group-read access on the TLS private key to a group it
# belongs to (see _grant_group_root_read() in app/user_auth.py) without
# needing root privileges itself. This is what lets the ui container's
# nginx (root master process, before it setuid()s to the unprivileged
# nginx user) read a key file it doesn't own, under ui's cap_drop: [ALL]
# hardening in docker-compose.yml - without that, either nginx couldn't
# load the key at all, or the key would have to be world-readable.
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --home-dir /home/appuser --create-home --shell /usr/sbin/nologin appuser \
    && usermod -aG root appuser \
    && mkdir -p /home/appuser/.cache/huggingface \
    && chown -R appuser:appuser /app /home/appuser \
    && chown appuser:root /app/tls/server.key \
    && chmod 640 /app/tls/server.key
ENV HOME=/home/appuser
USER appuser

EXPOSE 8090

# Tries HTTPS first (the default - see docker-entrypoint.sh), then falls
# back to plain HTTP, so this one healthcheck works whether or not
# DISABLE_TLS is set. -k skips verifying the (self-signed, by default)
# server certificate - this is a loopback healthcheck, not a client
# validating the server's identity over an untrusted network.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=20 \
    CMD curl -kfs https://localhost:8090/api/health || curl -fs http://localhost:8090/api/health || exit 1

CMD ["./docker-entrypoint.sh"]
