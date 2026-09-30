import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ServerCertificateInfo } from "../api/types";

function formatCertDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function SettingsHttpsCertificatePage() {
  const [info, setInfo] = useState<ServerCertificateInfo | null>(null);
  const [canRevert, setCanRevert] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [certPem, setCertPem] = useState("");
  const [keyPem, setKeyPem] = useState("");
  const [previewInfo, setPreviewInfo] = useState<ServerCertificateInfo | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [reverting, setReverting] = useState(false);

  const certFileInputRef = useRef<HTMLInputElement | null>(null);
  const keyFileInputRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.tlsCert();
      setInfo(res.info);
      setCanRevert(res.can_revert);
    } catch (e: any) {
      setError(e.message || "Failed to load the current HTTPS certificate");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleValidate() {
    setPreviewError(null);
    setPreviewInfo(null);
    if (!certPem.trim() || !keyPem.trim()) {
      setPreviewError("Paste or upload both the certificate and the private key first.");
      return;
    }
    setValidating(true);
    try {
      const res = await api.validateTlsCert(certPem, keyPem);
      setPreviewInfo(res.info);
    } catch (e: any) {
      setPreviewError(e.message || "That certificate/key pair didn't validate.");
    } finally {
      setValidating(false);
    }
  }

  async function handleInstall() {
    setError(null);
    setNotice(null);
    setPreviewError(null);
    setInstalling(true);
    try {
      const res = await api.installTlsCert(certPem, keyPem);
      setInfo(res.info);
      setCanRevert(res.can_revert);
      setPreviewInfo(null);
      setCertPem("");
      setKeyPem("");
      setNotice(
        "Certificate installed. It won't take effect until you restart both containers: " +
          "docker compose restart operations-manager ui"
      );
    } catch (e: any) {
      setError(e.message || "Failed to install the certificate");
    } finally {
      setInstalling(false);
    }
  }

  async function handleRevert() {
    setError(null);
    setNotice(null);
    setReverting(true);
    try {
      const res = await api.revertTlsCert();
      setInfo(res.info);
      setCanRevert(res.can_revert);
      setNotice(
        "Reverted to the default self-signed certificate. Restart both containers to apply: " +
          "docker compose restart operations-manager ui"
      );
    } catch (e: any) {
      setError(e.message || "Failed to revert the certificate");
    } finally {
      setReverting(false);
    }
  }

  function readFileInto(file: File | undefined, setter: (text: string) => void) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setter(String(reader.result || ""));
    reader.onerror = () => setPreviewError("Could not read that file.");
    reader.readAsText(file);
  }

  if (loading) return <div className="loading-note">Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">HTTPS Certificate</h1>
          <p className="page-subtitle">
            Install a real certificate for the dashboard and API's own HTTPS listeners, replacing the self-signed
            fallback every fresh install starts with. This is separate from the corporate CA under LDAP
            Authentication - that one is what this appliance trusts when connecting <em>out</em> to a directory
            server; this is the certificate it presents to your browser.
          </p>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="loading-note">{notice}</div>}

      <div className="card section-gap" style={{ marginBottom: 24 }}>
        <div className="card-title">Current certificate</div>
        {info ? (
          <div>
            <div>
              <strong>Subject:</strong> {info.subject}
            </div>
            <div>
              <strong>Issuer:</strong> {info.issuer}
            </div>
            <div>
              <strong>Valid:</strong> {formatCertDate(info.not_valid_before)} &ndash;{" "}
              {formatCertDate(info.not_valid_after)}
              {info.is_expired ? " (expired)" : ""}
            </div>
            {info.subject_alt_names.length > 0 && (
              <div>
                <strong>Covers:</strong> {info.subject_alt_names.join(", ")}
              </div>
            )}
            <div className="field-hint" style={{ marginTop: 8 }}>
              {info.is_self_signed
                ? "This is the default self-signed certificate - browsers will warn about it until you install a real one below."
                : "A custom certificate is installed."}
            </div>
          </div>
        ) : (
          <div className="field-hint">No certificate could be read from disk.</div>
        )}
        {canRevert && (
          <button
            type="button"
            className="btn btn-secondary"
            style={{ marginTop: 12 }}
            disabled={reverting}
            onClick={handleRevert}
          >
            {reverting ? "Reverting..." : "Revert to default self-signed certificate"}
          </button>
        )}
      </div>

      <div className="panel section-gap">
        <div className="card-title">Install a new certificate</div>
        <p className="field-hint" style={{ marginTop: 0 }}>
          Paste or upload a PEM certificate (a leaf certificate, or a leaf + intermediate chain concatenated) and its
          matching unencrypted private key. The pair is validated - and checked against each other - before
          anything is written to disk.
        </p>

        <div className="two-col">
          <div className="field">
            <label>Certificate (PEM)</label>
            <textarea
              rows={8}
              placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----"
              value={certPem}
              onChange={(e) => setCertPem(e.target.value)}
              style={{ fontFamily: "monospace", fontSize: 12, width: "100%" }}
            />
            <input
              ref={certFileInputRef}
              type="file"
              accept=".pem,.crt,.cer,.txt"
              onChange={(e) => readFileInto(e.target.files?.[0], setCertPem)}
              style={{ display: "none" }}
            />
            <button
              type="button"
              className="btn btn-secondary"
              style={{ marginTop: 8 }}
              onClick={() => certFileInputRef.current?.click()}
            >
              Upload certificate file
            </button>
          </div>
          <div className="field">
            <label>Private key (PEM, unencrypted)</label>
            <textarea
              rows={8}
              placeholder="-----BEGIN PRIVATE KEY-----&#10;...&#10;-----END PRIVATE KEY-----"
              value={keyPem}
              onChange={(e) => setKeyPem(e.target.value)}
              style={{ fontFamily: "monospace", fontSize: 12, width: "100%" }}
            />
            <input
              ref={keyFileInputRef}
              type="file"
              accept=".pem,.key,.txt"
              onChange={(e) => readFileInto(e.target.files?.[0], setKeyPem)}
              style={{ display: "none" }}
            />
            <button
              type="button"
              className="btn btn-secondary"
              style={{ marginTop: 8 }}
              onClick={() => keyFileInputRef.current?.click()}
            >
              Upload key file
            </button>
          </div>
        </div>

        {previewError && <div className="error-note" style={{ marginTop: 12 }}>{previewError}</div>}
        {previewInfo && (
          <div className="loading-note" style={{ marginTop: 12 }}>
            <div>
              <strong>Subject:</strong> {previewInfo.subject}
            </div>
            <div>
              <strong>Issuer:</strong> {previewInfo.issuer}
            </div>
            <div>
              <strong>Valid:</strong> {formatCertDate(previewInfo.not_valid_before)} &ndash;{" "}
              {formatCertDate(previewInfo.not_valid_after)}
              {previewInfo.is_expired ? " (expired)" : ""}
            </div>
            {previewInfo.subject_alt_names.length > 0 && (
              <div>
                <strong>Covers:</strong> {previewInfo.subject_alt_names.join(", ")}
              </div>
            )}
            <div>This certificate and key match and are ready to install.</div>
          </div>
        )}

        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button type="button" className="btn btn-secondary" disabled={validating} onClick={handleValidate}>
            {validating ? "Validating..." : "Validate"}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={installing || !certPem.trim() || !keyPem.trim()}
            onClick={handleInstall}
          >
            {installing ? "Installing..." : "Install certificate"}
          </button>
        </div>
        <div className="field-hint" style={{ marginTop: 8 }}>
          Installing writes the files immediately, but neither the dashboard nor the API picks up a changed
          certificate without a restart - run <code>docker compose restart operations-manager ui</code> afterward.
        </div>
      </div>
    </div>
  );
}
