# Security Hardening

This document tracks the security hardening work applied to the Couchbase
Agent Operations Manager appliance's own code and configuration - the
application (`operations-manager`), the dashboard
(`ui`), and the Docker Compose stack that ties them together - and maps
each change to the control families in CIS Benchmarks, NIST SP 800-53 /
SP 800-123, DISA STIG control objectives, and PCI DSS v4.0 that it
addresses.

## Summary

| Area | Change | Standards touched |
|---|---|---|
| CORS | Removed wildcard origin + credentialed access; added `CORS_ALLOWED_ORIGINS` allowlist, deny-by-default | PCI 4.0 §6.2.4, NIST SC-23 |
| HTTP security headers | HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy, CSP on both the API and the dashboard | CIS, PCI 4.0 §6.2, NIST SC-23/CM-6 |
| Password policy | Minimum length 8 → 12; added common-password and password-equals-username checks | PCI 4.0 §8.3.6, NIST IA-5 |
| Login throttling | Per-username and per-IP lockout after repeated failed logins | PCI 4.0 §8.3.4, NIST AC-7 |
| Auth audit logging | Every dashboard login attempt (success, failure, lockout, disabled account) now written to the audit log | PCI 4.0 §10.2, NIST AU-2/AU-3 |
| Default-credential warnings | Startup warnings for the default Couchbase password and an unset `AUTH_SECRET_KEY` (already existed for the latter) | PCI 4.0 §8.3.1, CIS |
| LDAP TLS verification | Warn loudly when LDAPS/StartTLS is enabled with no CA certificate configured (certificate validation is silently off in that case) | PCI 4.0 §4.2.1, NIST SC-8 |
| Non-root containers | `operations-manager` and `sample-mcp-servers` run as an unprivileged `appuser`, not root | CIS Docker Benchmark §4, NIST SP 800-190/AC-6 |
| Dropped Linux capabilities | `cap_drop: [ALL]` on every custom-built container; `ui` gets back only `NET_BIND_SERVICE` (needed to bind 443) | CIS Docker Benchmark §5, NIST AC-6 |
| `no-new-privileges` | Set on every service in the compose stack | CIS Docker Benchmark §5 |
| Network exposure | Couchbase's admin/data ports (8091-8096, 11210) and the sample MCP servers' port (8100) now bind to `127.0.0.1` only, not `0.0.0.0` | PCI 4.0 §1, NIST SC-7, CIS |
| TLS configuration | Explicit modern AEAD cipher suite, `ssl_prefer_server_ciphers on`, session tickets off, `server_tokens off` | PCI 4.0 §4.2.1, CIS |
| CSP-compatible script loading | Extracted the dashboard's one inline `<script>` into `theme-init.js` so `script-src` needs no `'unsafe-inline'` | NIST SC-23 |

Detailed rationale for each is below. Sections are organized by layer:
application, containers/infrastructure, network, and cryptography/transport.
A final section lists what was reviewed and found already compliant, and
what remains a known, documented gap.

## Application-layer hardening

### CORS and the session cookie

`operations-manager/app/main.py` previously registered
`CORSMiddleware(allow_origins=["*"], allow_credentials=True, ...)`. Combined
with the dashboard's cookie-based session (`aom_session`, httpOnly), this
is a real vulnerability: Starlette's CORS middleware, when asked for a
wildcard origin *and* credentialed access, reflects the requesting
`Origin` header back rather than sending a literal `*` (browsers forbid
`*` with credentials, so the middleware works around that) - the practical
effect is that **any** website's JavaScript could make a credentialed
request to this API and have the browser attach a logged-in admin's
session cookie to it.

The fix adds `config.CORS_ALLOWED_ORIGINS` (from the `CORS_ALLOWED_ORIGINS`
env var, comma-separated, empty by default). With nothing configured,
cross-origin credentialed access is off entirely - which changes nothing
for the bundled stack, since the dashboard is always served same-origin
(nginx proxies `/v1/*` and `/api/*` through to `operations-manager` under
the `ui` container's own origin) and agent callers authenticate with a
`Bearer` API key header, which a browser never attaches automatically the
way it does a cookie. If you have a custom browser-based integration that
genuinely needs cross-origin, credentialed access to this API, set
`CORS_ALLOWED_ORIGINS` to its exact origin(s).

### HTTP security headers

A `security_headers` middleware in `main.py` now sets, on every response:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`
- `Strict-Transport-Security` (only when the request actually arrived over HTTPS - this appliance supports a `DISABLE_TLS=true` plain-HTTP mode, and sending HSTS over plain HTTP is meaningless at best)
- `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'` (skipped on `/docs`, `/redoc`, `/openapi.json`, since FastAPI's bundled Swagger/ReDoc UI loads its assets from a CDN and a strict `default-src 'self'` there would just break the docs page, not protect anything)

The dashboard (`ui/nginx.conf.template`) gets the same header set plus a
CSP tuned for the actual React app: `script-src 'self'` (no
`'unsafe-inline'` - see below), `style-src 'self' 'unsafe-inline'` (the
dashboard sets React inline `style={{...}}` props throughout the
codebase; disallowing that would mean re-architecting styling everywhere,
not a config change, so this is a deliberate, scoped exception rather than
an oversight), `img-src`/`font-src 'self' data:'`, `connect-src 'self'`,
`object-src 'none'`, `frame-ancestors 'none'`, `form-action 'self'`.

Getting `script-src` to `'self'` with no exception required one small
source change: `ui/index.html` had one inline `<script>` (sets
`data-theme` before first paint, to avoid a flash of the wrong theme).
That logic now lives in `ui/public/theme-init.js`, loaded with a normal
`<script src="...">` tag, so it counts as same-origin under `script-src
'self'` instead of needing a CSP exception.

### Password policy

`user_auth.password_policy_error()` enforced only a minimum length of 8.
PCI DSS v4.0 Requirement 8.3.6 sets 12 characters as the floor for systems
that can support it (the 8-character floor is only for systems that
cannot, combined with compensating controls) - this one can, so it now
requires 12. It also now rejects a small set of common/predictable
passwords an administrator is likely to type in a hurry on first boot
(`changeme123!` is 12 characters and would otherwise sail through a pure
length check) and rejects a password equal to the account's own username.
This follows NIST SP 800-63B's guidance of favoring length plus concrete
weakness checks over forced character-class complexity rules.

**Known gap:** password history/reuse prevention (PCI 8.3.6's sibling
requirement, 8.3.7, wants the last 4 passwords blocked from reuse) is not
implemented - it would require persisting password hash history, which is
a larger, more deliberate schema change than the checks above.

### Login throttling and lockout

There was previously no limit on failed login attempts against
`POST /v1/auth/login`. `user_auth.py` now tracks failed attempts per
username (10 failures in a 15-minute window locks that account for 30
minutes) and per source IP (30 failures across any usernames in 5 minutes
locks that IP for 15 minutes, as defense against one source spraying
many different accounts, which the per-username counter alone never
catches on any single one of them). A locked-out attempt returns
`429 Too Many Requests` with a `Retry-After` header and never reaches the
bcrypt/LDAP check. The specific thresholds follow PCI DSS v4.0 8.3.4
("limit repeated access attempts by locking out the user ID after not
more than 10 attempts... for a minimum lockout duration of 30 minutes").

The client IP is read from `X-Forwarded-For` (set by nginx's proxy - see
`ui/nginx.conf.template`) with a fallback to the raw socket address; this
trusts exactly one hop, which matches this appliance's actual topology
(nginx is the only thing ever in front of `operations-manager` in the
bundled stack).

**Known limitation:** this state is in-memory and per-process. It resets
on a container restart and would not be shared across replicas if this
service were ever run horizontally scaled - not a concern for the current
single-instance architecture, but worth knowing if that changes.

### Authentication audit logging

Every `/v1/auth/login` outcome - success, wrong password, wrong/unknown
username, disabled account, and lockout - is now written to the existing
append-only Couchbase audit log (`store.log_access`, `action:
"dashboard_login"`), the same sink `/v1/tools/discover` and
`/v1/tools/invoke` already write to. Previously only a failed *agent*
API-key lookup (`authenticate()`) was audited; human login attempts left
no trail at all. This satisfies PCI DSS v4.0 10.2.1/10.2.4 (log
successful and failed authentication) and NIST AU-2/AU-3.

### Default-credential and weak-configuration warnings

`operations-manager` already warned at startup when `AUTH_SECRET_KEY` was
left at its insecure built-in default. The same pattern now covers
`COUCHBASE_PASSWORD` being left at the bundled demo value
(`CouchbaseDemo123!`, published in this repo's own README and
`.env.example` - anyone who has read either already knows it), per PCI
DSS v4.0 8.3.1 ("do not use vendor-supplied defaults"). Separately,
`save_ldap_config`/`load_ldap_config` now log a warning whenever LDAPS or
StartTLS is enabled with no corporate CA certificate configured - in that
state `ldap3` does not validate the directory server's certificate at
all, which is functionally equivalent to skipping TLS verification and
leaves the bind open to an on-path attacker presenting any certificate.
This was not escalated to a hard failure, since doing so would break any
existing deployment that has this working today for reasons an operator
never had cause to examine; the warning points at Settings → LDAP
Authentication, where a CA certificate can be uploaded.

### Reviewed and already compliant

A few things worth stating explicitly, since "reviewed and found fine" is
easy to mistake for "not reviewed":

- **Couchbase queries (N1QL)** already use `QueryOptions(named_parameters=...)`
  for every value that comes from a caller; the only string-interpolated
  parts of any query are internal bucket/scope/collection names from
  `config.py`, never user input. No injection risk (PCI 6.2.4).
- **LDAP search filters** already escape user-supplied usernames with
  `ldap3.utils.conv.escape_filter_chars` before building the filter
  string. No LDAP injection risk.
- **Passwords** are already hashed with bcrypt (NIST 800-63B §5.1.1.2,
  PCI 8.3.2), never stored or logged in plaintext.
- **Session tokens** are JWTs that already pin the algorithm explicitly
  on both encode and decode (`HS256`), which rules out the classic
  algorithm-confusion attack against libraries that trust an
  attacker-supplied `alg` header.
- **Session cookies** already set `httponly=True`, `samesite="lax"`, and
  `secure=` (true whenever the request arrived over HTTPS, which is the
  default transport for this appliance).
- **Transport encryption** for both the dashboard and the API was already
  addressed in a previous pass of this project (self-signed-by-default
  HTTPS everywhere, a Settings → HTTPS Certificate page for installing a
  real certificate, TLS restricted to 1.2/1.3 only in nginx).
- **No debug/reload flags** are enabled in any entrypoint (`uvicorn` is
  invoked without `--reload`; no `debug=True` anywhere).
- **Dependencies are exactly pinned** (`==` versions throughout
  `requirements.txt`), and a prior audit of this codebase (see git
  history / conversation preceding this document) confirmed every pinned
  package is actually imported and used somewhere in the app - nothing
  unused inflating the attack surface.

## Container and infrastructure hardening

### Non-root containers

`operations-manager/Dockerfile` and `sample-mcp-servers/Dockerfile`
previously ran their application process as root (the base
`python:3.11-slim` image's default). Both now create a dedicated
unprivileged system user (`appuser`, uid/gid 10001) after all
build-time-only root work (package installation, corporate-CA trust
bootstrap, self-signed certificate generation) is done, `chown` the
directories the runtime process actually needs to write
(`/app` for `operations-manager`, including the `/app/tls` certificate
directory; `/app` for `sample-mcp-servers`), and switch to that user with
`USER appuser` before `CMD` runs. Neither service binds a port below
1024, so nothing about running unprivileged changes their runtime
behavior. This addresses CIS Docker Benchmark §4.1 and the "run as
non-root" recommendation in NIST SP 800-190 (Application Container
Security Guide) / the least-privilege intent of NIST AC-6.

One follow-on change: `operations-manager`'s Hugging Face model cache
volume (`hf-cache`, used by `sentence-transformers`) was mounted at
`/root/.cache/huggingface`, which only made sense while the process ran
as root. It now mounts at `/home/appuser/.cache/huggingface`, and the
container's `HOME` environment variable is set to match, so
`sentence-transformers`/`huggingface_hub` resolve their cache to the same
place. Because Docker named volumes are content-addressed by volume name
rather than by mount path, the existing volume's cached model (if any)
is preserved across this change - it just becomes visible at the new
path instead of the old one.

`ui`'s nginx container was deliberately left alone here: the official
`nginx:1.27-alpine` image already follows the standard, secure-by-default
nginx pattern of running the master process as root (needed only to bind
ports 80/443 and manage worker processes) while every worker process -
the thing actually parsing and serving untrusted HTTP requests - drops to
an unprivileged `nginx` user automatically, per the base image's own
`nginx.conf`. Forcing the master process itself non-root would require
either running on unprivileged ports (changing the appliance's port
story) or granting `CAP_NET_BIND_SERVICE` explicitly - which is exactly
what the compose-level capability change below does instead, without
needing an image change.

### Linux capabilities and privilege escalation

`docker-compose.yml` now sets `security_opt: [no-new-privileges:true]` on
every service (CIS Docker Benchmark §5.3 - blocks a process from gaining
additional privileges via a setuid/setgid binary, even accidentally
introduced by a future dependency). The three custom-built application
images (`operations-manager`, `sample-mcp-servers`, `ui`) additionally get
`cap_drop: [ALL]` (CIS Docker Benchmark §5.4-family - a container that
never needs `CAP_NET_RAW`, `CAP_SYS_ADMIN`, etc. shouldn't have them
available to a compromised process). `ui` adds back exactly one
capability, `CAP_NET_BIND_SERVICE`, since nginx's root master process
needs it to bind ports 80/443. `couchbase` and `couchbase-init` are left
with their default capability set - they run Couchbase Server's own
vendor-shipped Enterprise Edition image, whose internal privilege
requirements are not something this project controls or has fully
enumerated, and getting this wrong for a stateful database is a much
worse failure mode than for a small stateless FastAPI/nginx service. They
still get `no-new-privileges`, which is safe unconditionally.

### Resource limits and read-only filesystems (reviewed, not applied)

CIS Docker Benchmark §5.10/5.11 recommend capping container memory and
CPU, and a read-only root filesystem is a common further hardening step.
Neither is applied here: `operations-manager` loads a `sentence-transformers`
embedding model and a CPU build of `torch` at startup, and guessing a
memory ceiling wrong would silently OOM-kill the appliance on a
resource-constrained host - the right number depends on the deployment
environment, not something this codebase can respond safely on its own.
A read-only root filesystem is a good target but needs auditing every
write path first (temp files, the Hugging Face cache, the TLS
certificate directory) to avoid breaking something that isn't obviously
a "container filesystem write" until it fails at runtime. Both are
recorded here as recommended follow-ups for whoever sizes a specific
deployment, not implemented blind.

## Network exposure

`docker-compose.yml` previously published Couchbase's admin console and
data ports (`8091-8096`, `11210`) and the sample MCP servers' port
(`8100`) on `0.0.0.0` - reachable from any other host on the network.
None of them need to be: `operations-manager` reaches both over the
private Compose network (`couchbase://couchbase`,
`http://sample-mcp-servers:8100`), never through these published host
ports. They now bind to `127.0.0.1` only, which still lets you open the
Couchbase Web Console at `https://localhost:8091` from the machine
running Docker, but no longer exposes it (or the sample servers) to the
rest of the network. `operations-manager`'s own port (`8090`) and `ui`'s
(`443`) are left published broadly, since those are the appliance's
actual, intended entry points. This follows PCI DSS v4.0 Requirement 1
(network security controls limiting exposure to what's needed) and NIST
SC-7 (Boundary Protection).

## Cryptography and transport

`ui/nginx.conf.template` already restricted TLS to 1.2/1.3 (from an
earlier pass of this project). This pass adds an explicit cipher suite -
AEAD-only, forward-secret (ECDHE) suites, no static-RSA key exchange, no
CBC-mode TLS 1.2 suites, no RC4/3DES - plus `ssl_prefer_server_ciphers
on`, disables TLS session tickets (`ssl_session_tickets off`, appropriate
for a single nginx instance with no shared ticket-key rotation across
replicas; session resumption still works via the session cache), and
turns off `server_tokens` so error pages and response headers stop
naming the exact nginx version. This addresses PCI DSS v4.0 Requirement
4.2.1 ("strong cryptography... for transmission") and the general CIS
guidance against advertising software version information.

## Verifying these changes

A few concrete ways to confirm the changes above took effect after
`docker compose up --build`:

```bash
# Security headers on the dashboard
curl -kI https://localhost/ | grep -iE "strict-transport|x-frame|x-content-type|content-security|referrer-policy|permissions-policy"

# Security headers on the API
curl -kI https://localhost:8090/api/health | grep -iE "strict-transport|x-frame|x-content-type|content-security"

# Non-root process inside the operations-manager container
docker compose exec operations-manager id
# -> uid=10001(appuser) gid=10001(appuser)

# Couchbase admin console is no longer reachable from another host,
# only from this machine
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8091/pools   # works locally
# from another machine on the network, this now fails to connect at all

# Login lockout (11 consecutive bad attempts against one username from
# a fresh session should return 429 on the last one or two)
for i in $(seq 1 11); do
  curl -sk -o /dev/null -w "%{http_code}\n" https://localhost/v1/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"wrong-password"}'
done
```


## Standards referenced

- **CIS Benchmarks** - CIS Docker Benchmark (container image and runtime
  configuration).
- **NIST SP 800-53 Rev. 5** - control families AC (Access Control), AU
  (Audit and Accountability), CM (Configuration Management), IA
  (Identification and Authentication), SC (System and Communications
  Protection), SI (System and Information Integrity).
- **NIST SP 800-123** - Guide to General Server Security (baseline
  hardening themes: minimize exposed services, secure configuration,
  logging).
- **NIST SP 800-190** - Application Container Security Guide (non-root
  containers, capability minimization).
- **DISA STIGs** - control-objective alignment only (see "Known gaps"
  above for the caveat on what this does and doesn't certify).
- **PCI DSS v4.0** - Requirements 1, 4.2, 6.2, 7, 8.3, 8.4, and 10.2/10.5.
