#!/bin/sh
# Convenience wrapper around `docker compose up --build` that prints the
# multi-container startup sequence a bit more legibly than raw compose
# output does, and reminds you where everything ends up once it's ready.
set -e

echo "=================================================================="
echo " Couchbase Agent Operations Manager - starting the appliance"
echo "=================================================================="
echo
echo "This brings up five containers:"
echo "  1. couchbase            - Couchbase Server Enterprise Edition"
echo "  2. couchbase-init       - one-shot bucket/scope/collection setup"
echo "  3. sample-mcp-servers   - bundled sample MCP tool servers"
echo "  4. operations-manager   - the operations manager itself (RBAC + vector"
echo "                            search pre-filtering, audit log, API)"
echo "  5. ui                   - the admin dashboard"
echo
echo "First boot downloads the Couchbase Enterprise image and an embedding"
echo "model, and can take a few minutes. Subsequent runs are much faster."
echo

if [ ! -f .env ]; then
  echo "No .env found - copying .env.example to .env with default values."
  cp .env.example .env
  echo

  # AUTH_SECRET_KEY signs dashboard login sessions and encrypts the LDAP
  # bind password at rest (see operations-manager/config.py) - a fresh
  # install should never run on config.py's hardcoded dev-only fallback.
  GENERATED_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32 2>/dev/null || true)
  if [ -n "$GENERATED_SECRET" ]; then
    if grep -q "^AUTH_SECRET_KEY=" .env; then
      sed -i.bak "s|^AUTH_SECRET_KEY=.*|AUTH_SECRET_KEY=${GENERATED_SECRET}|" .env && rm -f .env.bak
    else
      echo "AUTH_SECRET_KEY=${GENERATED_SECRET}" >> .env
    fi
    echo "Generated a random AUTH_SECRET_KEY for local login sessions."
    echo
  fi
fi

docker compose up --build

echo
echo "=================================================================="
echo " Ready. Open:"
echo "   Dashboard             -> https://localhost  (self-signed cert - your browser will warn)"
echo "   Operations Manager API -> https://localhost:8090  (see /docs; curl needs -k, SDK needs verify=False)"
echo "   Couchbase Web Console -> http://localhost:8091  (not covered by this appliance's TLS setup)"
echo "     (Administrator / CouchbaseDemo123! by default)"
echo "=================================================================="
