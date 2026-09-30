"""
Local dashboard login for the Couchbase Agent Operations Manager.

This is a separate concern from the agent RBAC in app/rbac_policy.py and
the `identities` collection in app/couchbase_client.py: that system
authenticates *agents* calling /v1/tools/discover and /v1/tools/invoke
with a bearer API key. This module authenticates *people* opening the
dashboard in a browser - the Servers, Catalog, Roles, Threat Detection,
Insights, Audit Log, LLM Caching, and Settings pages - via a login page
and a signed session cookie.

Three things live here:

  1. Password hashing (bcrypt) and session tokens (JWT, HS256) for local
     accounts stored in Couchbase's `users` collection.
  2. Symmetric encryption (Fernet, keyed off config.AUTH_SECRET_KEY) for
     the one dashboard secret that has to be stored *reversibly* rather
     than hashed: the LDAP bind password, which the appliance needs to
     hand back to the directory on every login attempt.
  3. The LDAP authentication flow itself, via ldap3: bind as the service
     account, search for the user, then bind again as that user's own DN
     with the password they typed in to actually verify it.

UI_ROLES stays code-defined for the same reason ROLES does in
rbac_policy.py - it's the kind of thing that gets reviewed, not something
end users type into a text box. "admin" gets the Settings section (local
accounts, roles, LDAP) plus every other page; "user" gets everything
except Settings.
"""
import asyncio
import base64
import hashlib
import logging
import os
import shutil
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import jwt
import ldap3
from ldap3.utils.conv import escape_filter_chars
from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization

from config import (
    AUTH_SECRET_KEY,
    AUTH_SESSION_TTL_HOURS,
    TLS_CERT_DEFAULT_BACKUP,
    TLS_CERT_FILE,
    TLS_KEY_DEFAULT_BACKUP,
    TLS_KEY_FILE,
)

logger = logging.getLogger("operations-manager.user_auth")

# ---------------------------------------------------------------------------
# UI roles (distinct from the agent RBAC roles in rbac_policy.ROLES)
# ---------------------------------------------------------------------------
UI_ROLES = {
    "admin": "Full access, including Settings (local accounts, roles, LDAP).",
    "user": "Every dashboard page except Settings.",
}
DEFAULT_LOCAL_ROLE = "user"
# PCI DSS v4.0 8.3.6 sets 12 characters as the minimum for systems that
# support it (versus the 8-character floor allowed only when a system
# cannot support 12 and compensating controls are in place - this one can,
# so it doesn't get the exception). NIST SP 800-63B's guidance is length
# over forced character-class complexity, which is why this checks length
# plus a small set of concrete weaknesses (common/breached passwords,
# password == username) rather than requiring e.g. "one symbol, one digit".
MIN_PASSWORD_LENGTH = 12

# Not an exhaustive breached-password list (that would mean shipping/
# fetching a real corpus, e.g. Have I Been Pwned's list, which is a
# deployment-time decision for an operator to make, not a hardcoded
# default) - this catches the specific passwords an admin is most likely
# to type in a hurry on first boot, which password_policy_error's length
# check alone would happily accept (e.g. "changeme123!" is 12 characters).
_COMMON_WEAK_PASSWORDS = {
    "password", "password1", "password123", "changeme", "changeme123",
    "changeit", "letmein", "welcome1", "admin1234", "administrator",
    "qwerty123", "abc123456", "iloveyou1", "couchbase", "couchbasedemo123",
}


def password_policy_error(password: str, username: str | None = None) -> str | None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    normalized = password.strip().lower()
    if normalized in _COMMON_WEAK_PASSWORDS:
        return "That password is too common - choose something less guessable."
    if username and normalized == username.strip().lower():
        return "Password cannot be the same as the username."
    return None


# ---------------------------------------------------------------------------
# Passwords - bcrypt, one-way. Nothing that verifies a password is ever
# capable of recovering it - that's the point of a hash over encryption
# for this particular secret.
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # A malformed/legacy hash should fail closed, not raise.
        logger.warning("verify_password: malformed hash")
        return False


# ---------------------------------------------------------------------------
# Sessions - a signed JWT carried in an httpOnly cookie (see main.py). It's
# stateless on purpose: nothing to look up in Couchbase on every request,
# and every issued session invalidates itself simply by expiring or by
# AUTH_SECRET_KEY changing.
# ---------------------------------------------------------------------------
SESSION_COOKIE_NAME = "aom_session"
_JWT_ALGORITHM = "HS256"


def create_session_token(username: str, role: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + AUTH_SESSION_TTL_HOURS * 3600,
    }
    return jwt.encode(payload, AUTH_SECRET_KEY, algorithm=_JWT_ALGORITHM)


def decode_session_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=[_JWT_ALGORITHM])
        username = payload.get("sub")
        role = payload.get("role")
        if not username or not role:
            return None
        return {"username": username, "role": role}
    except jwt.PyJWTError as exc:
        logger.debug("decode_session_token failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Encryption at rest for the LDAP bind password. Fernet needs a 32-byte
# urlsafe-base64 key; AUTH_SECRET_KEY is an arbitrary operator-supplied
# string (typically a hex token generated by start.sh), so it's stretched
# into a Fernet key with SHA-256 rather than requiring a second secret to
# be generated and managed.
# ---------------------------------------------------------------------------
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        digest = hashlib.sha256(AUTH_SECRET_KEY.encode("utf-8")).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning("decrypt_secret: could not decrypt stored value (did AUTH_SECRET_KEY change?)")
        return ""


# ---------------------------------------------------------------------------
# LDAP configuration - stored in Couchbase at settings::ldap (see
# config.LDAP_SETTINGS_DOC), the same settings::<name> convention as
# settings::llm_cache. The bind password is the only field encrypted at
# rest here; everything else is operational config, not a secret.
# ---------------------------------------------------------------------------
DEFAULT_LDAP_CONFIG = {
    "enabled": False,
    "host": "",
    "port": 389,
    "use_ssl": False,
    "start_tls": False,
    "bind_dn": "",
    "bind_password_encrypted": "",
    "user_search_base": "",
    "user_search_filter": "(uid={username})",
    "admin_group_dn": "",
    "group_member_attribute": "memberOf",
    # PEM-encoded corporate CA certificate (or chain) used to validate the
    # directory's LDAPS/StartTLS certificate. Not a secret - a CA cert is
    # public by design - so unlike bind_password_encrypted it is stored (and
    # may be returned over the API) as plain text. Empty means "use ldap3's
    # default TLS behavior", which is the same permissive (no verification)
    # mode this appliance has always used for LDAPS/StartTLS, so leaving
    # this unset is not a regression for existing deployments.
    "ca_certificate": "",
}


def normalize_ldap_config(cfg: dict | None) -> dict:
    """Merge partial input over the defaults and coerce types - the same
    role llm_cache.normalize_config plays for the LLM Caching page."""
    merged = {**DEFAULT_LDAP_CONFIG, **(cfg or {})}
    return {
        "enabled": bool(merged.get("enabled", False)),
        "host": str(merged.get("host") or "").strip(),
        "port": int(merged.get("port") or 389),
        "use_ssl": bool(merged.get("use_ssl", False)),
        "start_tls": bool(merged.get("start_tls", False)),
        "bind_dn": str(merged.get("bind_dn") or "").strip(),
        "bind_password_encrypted": str(merged.get("bind_password_encrypted") or ""),
        "user_search_base": str(merged.get("user_search_base") or "").strip(),
        "user_search_filter": str(merged.get("user_search_filter") or "(uid={username})").strip(),
        "admin_group_dn": str(merged.get("admin_group_dn") or "").strip(),
        "group_member_attribute": str(merged.get("group_member_attribute") or "memberOf").strip(),
        "ca_certificate": str(merged.get("ca_certificate") or "").strip(),
    }


def parse_ca_certificate(pem_text: str) -> dict:
    """Validate a PEM-encoded certificate (or chain - only the first cert is
    inspected for display) and return the metadata the Settings UI shows so
    an admin can confirm they installed the right one before saving.

    Raises ValueError with a human-readable message on anything that isn't
    a parseable X.509 certificate; callers should turn that into a 400.
    """
    text = (pem_text or "").strip()
    if not text:
        raise ValueError("No certificate provided.")
    try:
        cert = x509.load_pem_x509_certificate(text.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not parse as a PEM X.509 certificate: {exc}") from exc

    def _name(name: x509.Name) -> str:
        try:
            return name.rfc4514_string()
        except Exception:  # noqa: BLE001
            return str(name)

    return {
        "subject": _name(cert.subject),
        "issuer": _name(cert.issuer),
        "not_valid_before": cert.not_valid_before_utc.isoformat(),
        "not_valid_after": cert.not_valid_after_utc.isoformat(),
        "is_expired": cert.not_valid_after_utc <= datetime.now(timezone.utc),
    }


def public_ldap_config(cfg: dict | None) -> dict:
    """The GET-safe view of the LDAP config: never return the bind
    password (encrypted or not) over the API, only whether one is set."""
    normalized = normalize_ldap_config(cfg)
    public = {k: v for k, v in normalized.items() if k != "bind_password_encrypted"}
    public["bind_password_set"] = bool(normalized.get("bind_password_encrypted"))
    ca_certificate = normalized.get("ca_certificate") or ""
    if ca_certificate:
        try:
            public["ca_certificate_info"] = parse_ca_certificate(ca_certificate)
        except ValueError as exc:
            # Shouldn't happen for anything saved via put_ldap_config (which
            # validates first), but don't let a bad stored value break the
            # settings page from loading.
            public["ca_certificate_info"] = {"error": str(exc)}
    else:
        public["ca_certificate_info"] = None
    return public


async def ldap_authenticate(cfg: dict, username: str, password: str) -> tuple[bool, str, bool]:
    """Try to authenticate `username`/`password` against the directory
    described by `cfg` (already normalize_ldap_config'd).

    Returns (success, detail, is_admin). Runs the blocking ldap3 calls in a
    worker thread so this coroutine never stalls the event loop.
    """

    def _run() -> tuple[bool, str, bool]:
        if not cfg.get("host") or not cfg.get("user_search_base"):
            return False, "LDAP is not fully configured (host/user search base missing).", False

        tls = None
        ca_certificate = (cfg.get("ca_certificate") or "").strip()
        if ca_certificate:
            # Explicit corporate CA installed: require and verify against
            # it. Without this, ldap3's default Tls (no explicit `tls=`
            # passed to Server) does not validate the server certificate at
            # all - so this only ever makes LDAPS/StartTLS *stricter* than
            # the appliance's previous behavior, never looser.
            try:
                tls = ldap3.Tls(
                    ca_certs_data=ca_certificate.encode("utf-8"),
                    validate=ssl.CERT_REQUIRED,
                    version=ssl.PROTOCOL_TLS_CLIENT,
                )
            except Exception as exc:  # noqa: BLE001
                return False, f"Could not load configured corporate CA certificate: {exc}", False

        server = ldap3.Server(cfg["host"], port=cfg["port"], use_ssl=cfg["use_ssl"], get_info=ldap3.NONE, tls=tls)
        bind_password = decrypt_secret(cfg.get("bind_password_encrypted", ""))

        try:
            service_conn = ldap3.Connection(
                server,
                user=cfg.get("bind_dn") or None,
                password=bind_password or None,
                auto_bind=False,
            )
            if cfg.get("start_tls"):
                service_conn.open()
                service_conn.start_tls()
            if not service_conn.bind():
                return False, f"Service-account bind failed: {service_conn.result}", False
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not connect to LDAP host: {exc}", False

        group_attr = cfg.get("group_member_attribute") or "memberOf"
        try:
            search_filter = cfg["user_search_filter"].format(username=escape_filter_chars(username))
        except Exception:  # noqa: BLE001
            search_filter = f"({escape_filter_chars('uid')}={escape_filter_chars(username)})"

        service_conn.search(search_base=cfg["user_search_base"], search_filter=search_filter, attributes=[group_attr])
        if not service_conn.entries:
            service_conn.unbind()
            return False, "No matching user found in the directory.", False

        entry = service_conn.entries[0]
        user_dn = entry.entry_dn
        member_of: list[str] = []
        try:
            if group_attr in entry:
                member_of = [str(v) for v in entry[group_attr].values]
        except Exception:  # noqa: BLE001
            member_of = []
        service_conn.unbind()

        try:
            user_conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=False)
            if cfg.get("start_tls"):
                user_conn.open()
                user_conn.start_tls()
            if not user_conn.bind():
                return False, "Invalid username or password.", False
            user_conn.unbind()
        except Exception as exc:  # noqa: BLE001
            return False, f"Bind as user failed: {exc}", False

        admin_group = cfg.get("admin_group_dn")
        is_admin = bool(admin_group) and any(admin_group.lower() == m.lower() for m in member_of)
        return True, "Authenticated.", is_admin

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# HTTPS server certificate - Settings -> HTTPS Certificate. Separate from the
# LDAP corporate CA above: that one is a CA the *outbound* LDAPS/StartTLS
# client trusts; this is the leaf certificate + private key nginx and
# uvicorn present *to browsers*. Reads/writes config.TLS_CERT_FILE /
# TLS_KEY_FILE directly - the same two files docker-entrypoint.sh launches
# uvicorn with and the `ui` service's nginx serves from (shared via a Docker
# volume - see docker-compose.yml) - so this page always reflects what's
# actually being served, and neither server picks up a change until it's
# restarted (TLS listeners don't hot-reload a swapped-out cert file).
# ---------------------------------------------------------------------------
def parse_server_certificate(pem_text: str) -> dict:
    """Validate a PEM-encoded leaf certificate (a chain is fine too - only
    the first/leaf cert is inspected here) and return the metadata the
    Settings UI shows. Raises ValueError with a human-readable message on
    anything that isn't a parseable X.509 certificate."""
    text = (pem_text or "").strip()
    if not text:
        raise ValueError("No certificate provided.")
    try:
        cert = x509.load_pem_x509_certificate(text.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not parse as a PEM X.509 certificate: {exc}") from exc

    def _name(name: x509.Name) -> str:
        try:
            return name.rfc4514_string()
        except Exception:  # noqa: BLE001
            return str(name)

    san_names: list[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san_names = [str(entry.value) for entry in san_ext.value]
    except x509.ExtensionNotFound:
        san_names = []
    except Exception:  # noqa: BLE001
        san_names = []

    return {
        "subject": _name(cert.subject),
        "issuer": _name(cert.issuer),
        "not_valid_before": cert.not_valid_before_utc.isoformat(),
        "not_valid_after": cert.not_valid_after_utc.isoformat(),
        "is_expired": cert.not_valid_after_utc <= datetime.now(timezone.utc),
        "is_self_signed": cert.issuer == cert.subject,
        "subject_alt_names": san_names,
    }


def _public_key_bytes(pub) -> bytes:
    return pub.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def validate_server_key_pair(cert_pem: str, key_pem: str) -> dict:
    """Parse and cross-check a certificate/private-key pair before they're
    ever written to disk. Raises ValueError (400-worthy) for: unparseable
    PEM, a password-protected private key (not supported - neither uvicorn
    nor nginx have a way to be handed a passphrase), or a key that doesn't
    actually match the certificate's public key - the single most common
    mistake when pasting these in by hand."""
    info = parse_server_certificate(cert_pem)

    key_text = (key_pem or "").strip()
    if not key_text:
        raise ValueError("No private key provided.")
    try:
        private_key = serialization.load_pem_private_key(key_text.encode("utf-8"), password=None)
    except TypeError as exc:
        raise ValueError(
            "This private key is password-protected - remove the passphrase before uploading "
            "(uvicorn and nginx have no way to be given one)."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not parse as a PEM private key: {exc}") from exc

    cert = x509.load_pem_x509_certificate(cert_pem.strip().encode("utf-8"))
    if _public_key_bytes(cert.public_key()) != _public_key_bytes(private_key.public_key()):
        raise ValueError("This private key does not match the certificate's public key.")

    return info


def _write_file(path: str, text: str, mode: int) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    os.chmod(path, mode)


def _grant_group_root_read(path: str) -> None:
    """Let the ui container's nginx (root master process, pre-setuid) read
    the private key without making it world-readable. nginx and
    operations-manager run as different unprivileged users in different
    containers (uid 101 "nginx" vs. uid 10001 "appuser"), and ui's
    cap_drop: [ALL] (see docker-compose.yml) intentionally removed
    CAP_DAC_OVERRIDE, so a 0600 file owned by appuser is unreadable to
    nginx's root process no matter which user owns it. appuser is a
    supplementary member of the root group (gid 0) for exactly this
    reason (see operations-manager/Dockerfile), so it can hand out
    group-read access to a group it belongs to without needing full root
    privileges. Group-owning the key by root and setting mode 0640 keeps
    it unreadable to any other unprivileged user/service while still
    letting nginx's root process load it. Best-effort: swallow failures
    so this still works when run outside the hardened container (e.g.
    local dev, tests) where the root group membership doesn't exist.
    """
    try:
        os.chown(path, -1, 0)
        os.chmod(path, 0o640)
    except OSError as exc:  # noqa: BLE001
        logger.warning("Could not grant group-root read access to %s: %s", path, exc)


def install_server_certificate(cert_pem: str, key_pem: str) -> dict:
    """Validate then install a real HTTPS certificate/key pair, replacing
    the self-signed fallback for both the dashboard and this API. Only
    takes effect after operations-manager and ui are restarted."""
    info = validate_server_key_pair(cert_pem, key_pem)

    # Preserve the original baked-in self-signed cert exactly once, the
    # first time a real certificate is ever installed, so "revert to
    # default" has something to restore instead of just failing.
    if os.path.exists(TLS_CERT_FILE) and not os.path.exists(TLS_CERT_DEFAULT_BACKUP):
        try:
            shutil.copyfile(TLS_CERT_FILE, TLS_CERT_DEFAULT_BACKUP)
            if os.path.exists(TLS_KEY_FILE):
                shutil.copyfile(TLS_KEY_FILE, TLS_KEY_DEFAULT_BACKUP)
                os.chmod(TLS_KEY_DEFAULT_BACKUP, 0o600)
        except OSError as exc:  # noqa: BLE001
            logger.warning("Could not back up the default self-signed certificate: %s", exc)

    _write_file(TLS_CERT_FILE, cert_pem.strip() + "\n", 0o644)
    _write_file(TLS_KEY_FILE, key_pem.strip() + "\n", 0o600)
    _grant_group_root_read(TLS_KEY_FILE)
    logger.info("Installed a new HTTPS server certificate (subject=%s) - restart to apply.", info["subject"])
    return info


def current_server_certificate_info() -> dict | None:
    """Parse whatever certificate is actually on disk right now (never a
    cached copy), so this always reflects the real state after a restart.
    None only if the file is missing or unreadable/unparseable."""
    try:
        with open(TLS_CERT_FILE, "r") as f:
            pem = f.read()
    except OSError:
        return None
    try:
        return parse_server_certificate(pem)
    except ValueError:
        return None


def can_revert_server_certificate() -> bool:
    return os.path.exists(TLS_CERT_DEFAULT_BACKUP) and os.path.exists(TLS_KEY_DEFAULT_BACKUP)


def revert_server_certificate() -> dict:
    """Restore the original baked-in self-signed certificate saved by the
    first install_server_certificate() call. Raises ValueError if no
    custom certificate was ever installed (nothing to revert from)."""
    if not can_revert_server_certificate():
        raise ValueError("No custom certificate has been installed, so there's nothing to revert.")
    shutil.copyfile(TLS_CERT_DEFAULT_BACKUP, TLS_CERT_FILE)
    shutil.copyfile(TLS_KEY_DEFAULT_BACKUP, TLS_KEY_FILE)
    os.chmod(TLS_KEY_FILE, 0o600)
    _grant_group_root_read(TLS_KEY_FILE)
    logger.info("Reverted to the default self-signed HTTPS certificate - restart to apply.")
    info = current_server_certificate_info()
    if info is None:
        raise ValueError("Reverted the certificate files, but the restored default certificate could not be parsed.")
    return info


# ---------------------------------------------------------------------------
# Login throttling / account lockout - PCI DSS v4.0 8.3.4 ("limit repeated
# access attempts by locking out the user ID after not more than 10
# attempts, for a minimum lockout duration of 30 minutes") and NIST SP
# 800-53 AC-7 (Unsuccessful Logon Attempts). In-memory and per-process by
# design, matching this appliance's current single-instance architecture
# (see docs/aom-distribution-architecture in the project notes) - state
# resets on restart and is not shared across replicas if this is ever run
# horizontally scaled, which is a documented limitation, not an oversight.
#
# Two independent counters:
#   - per-username: the PCI 8.3.4 control itself, protects one account
#     against being brute-forced regardless of source IP.
#   - per-IP: defense in depth against a single source spraying many
#     different usernames, which the per-username counter alone never
#     trips on any one of them.
# ---------------------------------------------------------------------------
LOGIN_LOCKOUT_THRESHOLD = 10
LOGIN_LOCKOUT_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_DURATION_SECONDS = 30 * 60
IP_LOCKOUT_THRESHOLD = 30
IP_LOCKOUT_WINDOW_SECONDS = 5 * 60
IP_LOCKOUT_DURATION_SECONDS = 15 * 60

_failed_by_username: dict[str, list[float]] = {}
_failed_by_ip: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def _prune(timestamps: list[float], window_seconds: int, now: float) -> list[float]:
    return [t for t in timestamps if now - t < window_seconds]


def login_lockout_status(username: str, ip: str) -> tuple[bool, int]:
    """Returns (locked, retry_after_seconds). Call before checking the
    password so a locked-out account never even reaches bcrypt/LDAP."""
    now = time.time()
    for key in (f"user:{username.strip().lower()}", f"ip:{ip}"):
        until = _locked_until.get(key)
        if until and until > now:
            return True, int(until - now) + 1
        if until:
            del _locked_until[key]
    return False, 0


_MAX_TRACKED_LOGIN_KEYS = 10000


def _prune_tracking(now: float) -> None:
    """Drop counters and lockouts that can no longer matter. Every distinct
    username ever tried gets an entry, so a password spray with random
    usernames would otherwise grow these for the life of the process."""
    for store, window in ((_failed_by_username, LOGIN_LOCKOUT_WINDOW_SECONDS), (_failed_by_ip, IP_LOCKOUT_WINDOW_SECONDS)):
        for key in [k for k, v in store.items() if not v or now - v[-1] >= window]:
            store.pop(key, None)
    for key in [k for k, until in _locked_until.items() if until <= now]:
        _locked_until.pop(key, None)


def record_failed_login(username: str, ip: str) -> None:
    now = time.time()
    if len(_failed_by_username) + len(_failed_by_ip) > _MAX_TRACKED_LOGIN_KEYS:
        _prune_tracking(now)
    ukey = username.strip().lower()
    attempts = _prune(_failed_by_username.get(ukey, []), LOGIN_LOCKOUT_WINDOW_SECONDS, now)
    attempts.append(now)
    _failed_by_username[ukey] = attempts
    if len(attempts) >= LOGIN_LOCKOUT_THRESHOLD:
        _locked_until[f"user:{ukey}"] = now + LOGIN_LOCKOUT_DURATION_SECONDS
        logger.warning("Account '%s' locked out for %ds after %d failed login attempts.", ukey, LOGIN_LOCKOUT_DURATION_SECONDS, len(attempts))

    ip_attempts = _prune(_failed_by_ip.get(ip, []), IP_LOCKOUT_WINDOW_SECONDS, now)
    ip_attempts.append(now)
    _failed_by_ip[ip] = ip_attempts
    if len(ip_attempts) >= IP_LOCKOUT_THRESHOLD:
        _locked_until[f"ip:{ip}"] = now + IP_LOCKOUT_DURATION_SECONDS
        logger.warning("Source IP %s locked out for %ds after %d failed login attempts across accounts.", ip, IP_LOCKOUT_DURATION_SECONDS, len(ip_attempts))


def record_successful_login(username: str, ip: str) -> None:
    """A successful login clears that username's failed-attempt counter
    (not the per-IP one - a spraying attacker who eventually guesses one
    account's password shouldn't get the other accounts' protection reset)."""
    _failed_by_username.pop(username.strip().lower(), None)
    _locked_until.pop(f"user:{username.strip().lower()}", None)


def new_local_user_doc(
    role: str,
    password_hash: str | None = None,
    source: str = "local",
    must_change_password: bool = False,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "role": role,
        "source": source,
        "password_hash": password_hash,
        "must_change_password": must_change_password,
        "active": True,
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
    }


def public_user(username: str, doc: dict) -> dict:
    """Strip password_hash before this ever reaches the API layer."""
    return {
        "username": username,
        "role": doc.get("role", DEFAULT_LOCAL_ROLE),
        "source": doc.get("source", "local"),
        "active": bool(doc.get("active", True)),
        "must_change_password": bool(doc.get("must_change_password", False)),
        "has_password": bool(doc.get("password_hash")),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "last_login_at": doc.get("last_login_at"),
    }
