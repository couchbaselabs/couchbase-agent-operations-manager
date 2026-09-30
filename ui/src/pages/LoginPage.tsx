import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { CouchbaseGlyph } from "../components/common/CouchbaseLogo";

const MIN_PASSWORD_LENGTH = 8;

export function LoginPage() {
  const { setUser } = useAuth();
  const [checking, setChecking] = useState(true);
  const [needsSetup, setNeedsSetup] = useState(false);
  const [adminUsername, setAdminUsername] = useState("admin");

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const status = await api.authBootstrapStatus();
        setNeedsSetup(status.needs_setup);
        setAdminUsername(status.username);
        setUsername(status.username);
      } catch {
        // Fall through to a normal login form - the operations manager may
        // still be starting up, or Couchbase may be briefly unreachable.
      } finally {
        setChecking(false);
      }
    })();
  }, []);

  async function handleSetup(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    try {
      const res = await api.authBootstrap(password);
      setUser(res.user);
    } catch (e: any) {
      setError(e.message || "Could not set the admin password.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleLogin(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await api.authLogin(username, password);
      setUser(res.user);
    } catch (e: any) {
      setError(e.message || "Invalid username or password.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="login-brand">
          <div className="brand-mark">
            <CouchbaseGlyph />
          </div>
          <div className="login-brand-name">
            Couchbase
            <br />
            Agent Operations Manager
          </div>
        </div>

        {checking ? (
          <div className="loading-note">Checking...</div>
        ) : needsSetup ? (
          <form onSubmit={handleSetup}>
            <h1 className="login-title">Set the admin password</h1>
            <p className="login-subtitle">
              This is the first time anyone has signed in as <strong>{adminUsername}</strong>. Choose a password to
              finish setting up this account.
            </p>
            {error && <div className="error-note">{error}</div>}
            <div className="field">
              <label>New password</label>
              <input
                type="password"
                required
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <div className="field-hint">At least {MIN_PASSWORD_LENGTH} characters.</div>
            </div>
            <div className="field">
              <label>Confirm password</label>
              <input
                type="password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
            </div>
            <button className="btn btn-primary login-submit" type="submit" disabled={submitting}>
              {submitting ? "Setting password..." : "Set password & sign in"}
            </button>
          </form>
        ) : (
          <form onSubmit={handleLogin}>
            <h1 className="login-title">Sign in</h1>
            <p className="login-subtitle">Use a local account, or your organization's LDAP credentials.</p>
            {error && <div className="error-note">{error}</div>}
            <div className="field">
              <label>Username</label>
              <input type="text" required autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
            </div>
            <div className="field">
              <label>Password</label>
              <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            <button className="btn btn-primary login-submit" type="submit" disabled={submitting}>
              {submitting ? "Signing in..." : "Sign in"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
