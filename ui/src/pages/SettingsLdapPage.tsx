import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { CaCertificateInfo, LdapConfig } from "../api/types";

type FormState = Omit<LdapConfig, "bind_password_set" | "ca_certificate_info"> & { bind_password: string };

const EMPTY_FORM: FormState = {
  enabled: false,
  host: "",
  port: 389,
  use_ssl: false,
  start_tls: false,
  bind_dn: "",
  user_search_base: "",
  user_search_filter: "(uid={username})",
  admin_group_dn: "",
  group_member_attribute: "memberOf",
  bind_password: "",
  ca_certificate: "",
};

function formatCertDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function SettingsLdapPage() {
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [bindPasswordSet, setBindPasswordSet] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveResult, setSaveResult] = useState<string | null>(null);

  const [testUsername, setTestUsername] = useState("");
  const [testPassword, setTestPassword] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; detail: string; would_be_admin: boolean } | null>(
    null
  );

  const [caCertInfo, setCaCertInfo] = useState<CaCertificateInfo | null>(null);
  const [caValidating, setCaValidating] = useState(false);
  const [caError, setCaError] = useState<string | null>(null);
  const caFileInputRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.ldapConfig();
      const { bind_password_set, ca_certificate_info, ...rest } = res.config;
      setForm({ ...rest, bind_password: "" });
      setBindPasswordSet(bind_password_set);
      setCaCertInfo(ca_certificate_info);
    } catch (e: any) {
      setError(e.message || "Failed to load LDAP configuration");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSaveResult(null);
    try {
      const { bind_password, ...config } = form;
      const res = await api.saveLdapConfig(config, bind_password || undefined);
      const { bind_password_set, ca_certificate_info, ...rest } = res.config;
      setForm({ ...rest, bind_password: "" });
      setBindPasswordSet(bind_password_set);
      setCaCertInfo(ca_certificate_info);
      setSaveResult("LDAP configuration saved.");
    } catch (e: any) {
      setError(e.message || "Failed to save LDAP configuration");
    } finally {
      setSaving(false);
    }
  }

  async function validateCaCertificate(pem: string) {
    setCaError(null);
    if (!pem.trim()) {
      setCaCertInfo(null);
      return;
    }
    setCaValidating(true);
    try {
      const res = await api.validateCaCertificate(pem);
      setCaCertInfo(res.info);
    } catch (e: any) {
      setCaCertInfo(null);
      setCaError(e.message || "That doesn't look like a valid PEM certificate.");
    } finally {
      setCaValidating(false);
    }
  }

  function handleCaFileChosen(file: File | undefined) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      setForm((f) => ({ ...f, ca_certificate: text }));
      validateCaCertificate(text);
    };
    reader.onerror = () => setCaError("Could not read that file.");
    reader.readAsText(file);
  }

  function handleRemoveCaCertificate() {
    setForm((f) => ({ ...f, ca_certificate: "" }));
    setCaCertInfo(null);
    setCaError(null);
    if (caFileInputRef.current) caFileInputRef.current.value = "";
  }

  async function handleTest(e: React.FormEvent) {
    e.preventDefault();
    setTesting(true);
    setTestResult(null);
    setError(null);
    try {
      const res = await api.testLdapConfig(testUsername, testPassword);
      setTestResult(res);
    } catch (e: any) {
      setError(e.message || "LDAP test failed");
    } finally {
      setTesting(false);
    }
  }

  if (loading) return <div className="loading-note">Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">LDAP Authentication</h1>
          <p className="page-subtitle">
            Let people sign in to this dashboard with their existing directory credentials instead of - or alongside
            - a local account. The bind password is encrypted at rest and is never returned by this page once saved.
          </p>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}
      {saveResult && <div className="loading-note">{saveResult}</div>}

      <form className="panel section-gap" onSubmit={handleSave} style={{ marginBottom: 24 }}>
        <div className="checkbox-row">
          <input
            type="checkbox"
            id="ldap-enabled"
            checked={form.enabled}
            onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
          />
          <label htmlFor="ldap-enabled" style={{ margin: 0, fontWeight: 600, color: "var(--text)" }}>
            Enable LDAP authentication
          </label>
        </div>
        <div className="field-hint" style={{ marginBottom: 16 }}>
          When enabled, a login that doesn't match a local account is tried against this directory. A matching local
          account always wins first, so this never overrides an existing local sign-in.
        </div>

        <div className="two-col">
          <div className="field">
            <label>Host</label>
            <input
              type="text"
              placeholder="ldap.example.com"
              value={form.host}
              onChange={(e) => setForm((f) => ({ ...f, host: e.target.value }))}
            />
          </div>
          <div className="field">
            <label>Port</label>
            <input
              type="number"
              value={form.port}
              onChange={(e) => setForm((f) => ({ ...f, port: Number(e.target.value) || 389 }))}
            />
          </div>
        </div>

        <div className="two-col">
          <div className="checkbox-row">
            <input
              type="checkbox"
              id="ldap-ssl"
              checked={form.use_ssl}
              onChange={(e) => setForm((f) => ({ ...f, use_ssl: e.target.checked }))}
            />
            <label htmlFor="ldap-ssl" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
              Use LDAPS (implicit TLS)
            </label>
          </div>
          <div className="checkbox-row">
            <input
              type="checkbox"
              id="ldap-starttls"
              checked={form.start_tls}
              onChange={(e) => setForm((f) => ({ ...f, start_tls: e.target.checked }))}
            />
            <label htmlFor="ldap-starttls" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
              Use StartTLS
            </label>
          </div>
        </div>

        <div className="field">
          <label>Corporate CA certificate (optional)</label>
          <div className="field-hint" style={{ marginBottom: 8 }}>
            If this directory's LDAPS/StartTLS certificate is signed by an internal corporate CA, install that CA's
            PEM certificate here so it can be validated instead of skipped. Leaving this blank keeps the previous
            behavior (the connection is encrypted but the certificate isn't checked against a CA) - unrelated to the
            build-time <code>scripts/setup-corporate-ca.sh</code> helper.
          </div>
          {caCertInfo && !caCertInfo.error && (
            <div className={caCertInfo.is_expired ? "error-note" : "loading-note"} style={{ marginBottom: 8 }}>
              <div>
                <strong>Subject:</strong> {caCertInfo.subject}
              </div>
              <div>
                <strong>Issuer:</strong> {caCertInfo.issuer}
              </div>
              <div>
                <strong>Valid:</strong> {formatCertDate(caCertInfo.not_valid_before)} &ndash;{" "}
                {formatCertDate(caCertInfo.not_valid_after)}
                {caCertInfo.is_expired ? " (expired)" : ""}
              </div>
            </div>
          )}
          {caError && <div className="error-note" style={{ marginBottom: 8 }}>{caError}</div>}
          <textarea
            rows={6}
            placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----"
            value={form.ca_certificate}
            onChange={(e) => setForm((f) => ({ ...f, ca_certificate: e.target.value }))}
            onBlur={(e) => validateCaCertificate(e.target.value)}
            style={{ fontFamily: "monospace", fontSize: 12, width: "100%" }}
          />
          <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
            <input
              ref={caFileInputRef}
              type="file"
              accept=".pem,.crt,.cer,.txt"
              onChange={(e) => handleCaFileChosen(e.target.files?.[0])}
              style={{ display: "none" }}
            />
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => caFileInputRef.current?.click()}
            >
              Upload certificate file
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={caValidating || !form.ca_certificate.trim()}
              onClick={() => validateCaCertificate(form.ca_certificate)}
            >
              {caValidating ? "Validating..." : "Validate"}
            </button>
            {form.ca_certificate && (
              <button type="button" className="btn btn-secondary" onClick={handleRemoveCaCertificate}>
                Remove
              </button>
            )}
          </div>
          <div className="field-hint" style={{ marginTop: 8 }}>
            Validating only parses the certificate to show its details above - it isn't saved until you click "Save
            LDAP configuration" below.
          </div>
        </div>

        <div className="two-col">
          <div className="field">
            <label>Bind DN (service account)</label>
            <input
              type="text"
              placeholder="cn=svc-aom,ou=service-accounts,dc=example,dc=com"
              value={form.bind_dn}
              onChange={(e) => setForm((f) => ({ ...f, bind_dn: e.target.value }))}
            />
          </div>
          <div className="field">
            <label>Bind password{bindPasswordSet ? " (configured - leave blank to keep it)" : ""}</label>
            <input
              type="password"
              placeholder={bindPasswordSet ? "••••••••" : ""}
              value={form.bind_password}
              onChange={(e) => setForm((f) => ({ ...f, bind_password: e.target.value }))}
            />
          </div>
        </div>

        <div className="field">
          <label>User search base</label>
          <input
            type="text"
            placeholder="ou=people,dc=example,dc=com"
            value={form.user_search_base}
            onChange={(e) => setForm((f) => ({ ...f, user_search_base: e.target.value }))}
          />
        </div>

        <div className="two-col">
          <div className="field">
            <label>User search filter</label>
            <input
              type="text"
              value={form.user_search_filter}
              onChange={(e) => setForm((f) => ({ ...f, user_search_filter: e.target.value }))}
            />
            <div className="field-hint">
              <code>{"{username}"}</code> is replaced with whatever was typed into the login form.
            </div>
          </div>
          <div className="field">
            <label>Group membership attribute</label>
            <input
              type="text"
              value={form.group_member_attribute}
              onChange={(e) => setForm((f) => ({ ...f, group_member_attribute: e.target.value }))}
            />
          </div>
        </div>

        <div className="field">
          <label>Admin group DN (optional)</label>
          <input
            type="text"
            placeholder="cn=aom-admins,ou=groups,dc=example,dc=com"
            value={form.admin_group_dn}
            onChange={(e) => setForm((f) => ({ ...f, admin_group_dn: e.target.value }))}
          />
          <div className="field-hint">
            An LDAP user whose group membership attribute includes this DN signs in with the admin role; everyone
            else gets the standard user role. Leave blank to give every LDAP user the standard role.
          </div>
        </div>

        <button className="btn btn-primary" type="submit" disabled={saving}>
          {saving ? "Saving..." : "Save LDAP configuration"}
        </button>
      </form>

      <div className="card">
        <div className="card-title">Test connection</div>
        <p className="field-hint" style={{ marginTop: 0 }}>
          Tests the <em>saved</em> configuration above (save first) against one set of real credentials - the same
          service-account bind, user search, and user bind a real login attempt would go through.
        </p>
        <form onSubmit={handleTest} className="two-col" style={{ alignItems: "flex-end" }}>
          <div className="field">
            <label>Username</label>
            <input type="text" required value={testUsername} onChange={(e) => setTestUsername(e.target.value)} />
          </div>
          <div className="field">
            <label>Password</label>
            <input
              type="password"
              required
              value={testPassword}
              onChange={(e) => setTestPassword(e.target.value)}
            />
          </div>
          <button className="btn btn-secondary" type="submit" disabled={testing}>
            {testing ? "Testing..." : "Test"}
          </button>
        </form>
        {testResult && (
          <div className={testResult.success ? "loading-note" : "error-note"} style={{ marginTop: 12 }}>
            {testResult.success
              ? `Success - would sign in as ${testResult.would_be_admin ? "admin" : "user"}.`
              : testResult.detail}
          </div>
        )}
      </div>
    </div>
  );
}
