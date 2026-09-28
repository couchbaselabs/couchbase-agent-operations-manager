# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

# Bootstrap: append the corporate CA directly onto the trust bundle that's
# already baked into the base image *before* touching the network - see
# operations-manager/Dockerfile for why we don't rely on
# `apt-get install ca-certificates` succeeding first.
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

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Cache mount persists downloaded wheels across builds/services (see
# operations-manager/Dockerfile) so rebuilds don't re-download this over
# and over through the corporate proxy.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --default-timeout=100 --retries 5 -r requirements.txt

COPY app ./app

# Run as an unprivileged user (CIS Docker Benchmark 4.1 / NIST SP 800-190).
# uvicorn binds :8100, an unprivileged port, so nothing at runtime needs
# root - apt-get and pip install already happened above as root, at build
# time.
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --home-dir /home/appuser --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8100

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=15 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/healthz')" || exit 1

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
