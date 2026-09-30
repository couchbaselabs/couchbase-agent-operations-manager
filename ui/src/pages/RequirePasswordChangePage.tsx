import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";

const MIN_PASSWORD_LENGTH = 8;

/** Blocks the rest of the app until an account with must_change_password
 * set (e.g. one an admin just reset from Settings -> Accounts & Roles)
 * picks a new password. Rendered by App.tsx in place of the normal
 * Sidebar+Routes layout - see the gating logic there. */
export function RequirePasswordChangePage() {
  const { user, setUser, logout } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    try {
      const res = await api.authChangePassword(currentPassword, newPassword);
      setUser(res.user);
    } catch (e: any) {
      setError(e.message || "Could not change the password.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        <form onSubmit={handleSubmit}>
          <h1 className="login-title">Choose a new password</h1>
          <p className="login-subtitle">
            {user?.username}, your password was reset. Set a new one to continue to the dashboard.
          </p>
          {error && <div className="error-note">{error}</div>}
          <div className="field">
            <label>Temporary password</label>
            <input
              type="password"
              required
              autoFocus
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
            />
          </div>
          <div className="field">
            <label>New password</label>
            <input type="password" required value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
          </div>
          <div className="field">
            <label>Confirm new password</label>
            <input
              type="password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
            />
          </div>
          <div className="flex-row">
            <button className="btn btn-primary login-submit" type="submit" disabled={submitting}>
              {submitting ? "Saving..." : "Save & continue"}
            </button>
            <button className="btn btn-secondary" type="button" onClick={logout}>
              Sign out
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
