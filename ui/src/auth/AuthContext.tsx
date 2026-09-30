import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { AuthUser } from "../api/types";

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  refresh: () => Promise<void>;
  setUser: (u: AuthUser | null) => void;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// Wraps the whole app (see main.tsx). Everything downstream - App's
// login/setup/forced-password-change gating, the Sidebar's Settings
// section visibility, the Settings pages' admin-only actions - reads
// `user` from here rather than re-fetching /v1/auth/me itself.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const res = await api.authMe();
      setUser(res.user);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // A session cookie can expire on any call, not just the auth ones -
    // this drops the whole app back to the login page the moment any
    // request comes back 401.
    api.setUnauthorizedHandler(() => setUser(null));
    refresh();
    return () => api.setUnauthorizedHandler(null);
  }, [refresh]);

  const logout = useCallback(async () => {
    try {
      await api.authLogout();
    } catch {
      // Clearing local state below is what actually matters for the UI.
    }
    setUser(null);
  }, []);

  const value = useMemo(() => ({ user, loading, refresh, setUser, logout }), [user, loading, refresh, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
