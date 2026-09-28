# syntax=docker/dockerfile:1.7
# --- build stage -----------------------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app

# Bootstrap: append the corporate CA directly onto the trust bundle that's
# already baked into the base image *before* touching the network. Without
# this, `apk add` itself fails - it fetches the Alpine package index over
# HTTPS from dl-cdn.alpinelinux.org, and a TLS-inspecting proxy's cert isn't
# trusted yet, so apk can't even install the ca-certificates package that
# would normally fix that. Once the bundle already includes the corp CA,
# `apk add`/`update-ca-certificates` (and npm afterwards) all work normally.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN if find /usr/local/share/ca-certificates/extra -name '*.crt' 2>/dev/null | grep -q .; then \
      cat /usr/local/share/ca-certificates/extra/*.crt >> /etc/ssl/certs/ca-certificates.crt && \
      apk add --no-cache ca-certificates && \
      update-ca-certificates; \
    fi
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# package-lock.json travels with package.json so npm can install from the
# lockfile directly (npm ci) instead of re-resolving the whole dependency
# graph against the registry - fewer registry round trips, which matters a
# lot when every one of them goes through a TLS-inspecting corporate proxy.
# The cache mount persists downloaded tarballs across builds so a rebuild
# (e.g. after a source change that doesn't touch package.json) doesn't
# re-download packages it already has.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . .
RUN npm run build

# --- certgen stage -----------------------------------------------------------
# Generates the fallback self-signed TLS cert on Debian/apt (python:3.11-slim,
# same base + corp-CA bootstrap pattern already proven to work for
# operations-manager's own cert generation under a TLS-inspecting corporate
# proxy) instead of on Alpine/apk. Alpine's `apk` fetches its package index
# over HTTPS and, on at least one tested corporate proxy, did not pick up
# the corp CA trust update the same way apt/npm/curl do here - so rather
# than debug apk's own trust store, this stage sidesteps it: no `apk add`
# happens anywhere in the final nginx image below.
FROM python:3.11-slim AS certgen
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN if find /usr/local/share/ca-certificates/extra -name '*.crt' 2>/dev/null | grep -q .; then \
      cat /usr/local/share/ca-certificates/extra/*.crt >> /etc/ssl/certs/ca-certificates.crt && \
      apt-get update && \
      apt-get install -y --no-install-recommends ca-certificates && \
      update-ca-certificates && \
      rm -rf /var/lib/apt/lists/*; \
    fi
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /tls && \
    openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
      -keyout /tls/server.key -out /tls/server.crt \
      -subj "/CN=localhost" \
      -addext "subjectAltName=DNS:localhost,DNS:ui,DNS:agent-operations-ui,IP:127.0.0.1" \
      2>/dev/null && \
    chmod 600 /tls/server.key

# --- serve stage -------------------------------------------------------------
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Fallback self-signed TLS server certificate so this container serves
# HTTPS out of the box with zero configuration (see nginx.conf.template) -
# generated in the certgen stage above, not here (see that stage's comment
# for why). For a real deployment, mount your own certificate over these
# same two paths (a docker-compose volume mount replaces these files, no
# image rebuild or code change needed) - see docker-compose.yml.
RUN mkdir -p /etc/nginx/tls
COPY --from=certgen /tls/server.crt /etc/nginx/tls/server.crt
COPY --from=certgen /tls/server.key /etc/nginx/tls/server.key
RUN chmod 600 /etc/nginx/tls/server.key

ENV OPERATIONS_MANAGER_UPSTREAM=operations-manager:8090 \
    OPERATIONS_MANAGER_SCHEME=https

EXPOSE 80 443

# wget, not curl: it's already part of Alpine's base busybox install, so
# this healthcheck needs no `apk add` (and thus no dependency on apk's own
# HTTPS trust store - see the certgen stage comment above).
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=15 \
    CMD wget --no-check-certificate -q -O /dev/null https://localhost/ || exit 1
