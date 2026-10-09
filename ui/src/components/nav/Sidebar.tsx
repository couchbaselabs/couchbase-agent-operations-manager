import { NavLink } from "react-router-dom";
import { useCallback, useState } from "react";
import { api } from "../../api/client";
import { usePoll } from "../../hooks/usePoll";
import type { HealthResponse } from "../../api/types";
import { ThemeToggle } from "../common/ThemeToggle";
import { CouchbaseGlyph } from "../common/CouchbaseLogo";
import { useAuth } from "../../auth/AuthContext";

type NavSection = {
  label: string;
  items: Array<{
    to: string;
    icon: string;
    text: string;
    exact?: boolean;
    // True for a plain server-rendered page this app proxies rather than
    // renders itself (currently just /docs - see ui/nginx.conf.template
    // and operations-manager/app/main.py) - a react-router NavLink would
    // try to match it against <Routes> in App.tsx and fail, so this
    // renders as an ordinary <a> full-navigation link instead, opened in
    // a new tab so it doesn't lose the caller's place in the dashboard.
    external?: boolean;
  }>;
};

const NAV_SECTIONS: NavSection[] = [
  {
    label: "Overview",
    items: [
      { to: "/", icon: "▦", text: "Dashboard", exact: true },
      { to: "/topology", icon: "⛓", text: "Live Topology" },
      { to: "/traces", icon: "⧗", text: "Traces" },
    ],
  },
  {
    label: "Registry",
    items: [
      { to: "/memory", icon: "◉", text: "Agent Memory" },
      { to: "/servers", icon: "⌘", text: "MCP Servers" },
      { to: "/catalog", icon: "≡", text: "Tool Catalog" },
      { to: "/knowledge", icon: "❐", text: "Knowledge Base" },
      { to: "/rag-applications", icon: "❖", text: "RAG Applications" },
    ],
  },
  {
    label: "LLM Caching",
    items: [
      { to: "/llm-caching", icon: "◈", text: "LLM Cache", exact: true },
      { to: "/context-cache", icon: "⬒", text: "Context Cache", exact: true },
      { to: "/llm-caching/settings", icon: "⚙", text: "Providers & Policy" },
    ],
  },
  {
    label: "Security",
    items: [
      { to: "/roles", icon: "⛨", text: "Roles & RBAC" },
      { to: "/threat-detection", icon: "☣", text: "Threat Detection" },
      { to: "/approvals", icon: "⎔", text: "Approvals" },
      { to: "/insights", icon: "✳", text: "Insights" },
      { to: "/audit-log", icon: "☷", text: "Audit Log" },
      { to: "/evals", icon: "◎", text: "Evaluations" },
    ],
  },
  {
    label: "Tools",
    items: [
      { to: "/developer-sdk/agent-code", icon: "⌥", text: "Agent Code" },
      { to: "/developer-sdk", icon: "⤓", text: "Developer SDK", exact: true },
      // Same-origin Swagger UI, proxied from operations-manager - see the
      // comment above the /docs location in ui/nginx.conf.template.
      { to: "/docs", icon: "▤", text: "API Documentation", external: true },
      { to: "/agent-tool-audit", icon: "▸", text: "Agent Tool Audit" },
    ],
  },
];

// Rendered as its own section below Tools, and only for admins (see
// Sidebar() below) - Settings is where local accounts, their roles, and
// LDAP authentication get managed, none of which a non-admin should even
// see exists.
const SETTINGS_SECTION: NavSection = {
  label: "Settings",
  items: [
    { to: "/settings/accounts", icon: "◍", text: "Accounts & Roles" },
    { to: "/settings/agents", icon: "⚿", text: "Agent Identities" },
    { to: "/settings/ldap", icon: "⌁", text: "LDAP Authentication" },
    { to: "/settings/limits", icon: "◔", text: "Limits & Budgets" },
    { to: "/settings/guardrails", icon: "⊘", text: "Guardrails & PII" },
    { to: "/settings/https-cert", icon: "⚿", text: "HTTPS Certificate" },
  ],
};

export function Sidebar() {
  const { user, logout } = useAuth();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [failed, setFailed] = useState(false);

  // Shown on every page, so this is the one poll that is always running.
  // /api/health is deliberately cheap (it reads in-memory flags and touches
  // Couchbase not at all), but it still goes through the same request path
  // as everything else - so it skips a tick while one is outstanding like
  // the rest of them.
  const poll = useCallback(async () => {
    try {
      const h = await api.health();
      setHealth(h);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  usePoll(poll, 15000);

  const connected = !!health?.couchbase_connected && !failed;
  // The appliance answered, but told us it came up with failed startup steps.
  // Worth its own state: "connected" would be true and green here, which is
  // exactly the reassurance an operator should not be given while the tool
  // catalog or the eval datasets are silently missing.
  const failures = (!failed && health?.startup_failures) || [];
  const degraded = failures.length > 0;
  const statusText = failed
    ? "Operations manager unreachable"
    : degraded
      ? `Degraded - ${failures.length} startup step${failures.length === 1 ? "" : "s"} failed`
      : connected
        ? "Couchbase connected"
        : "Starting...";

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <CouchbaseGlyph />
        </div>
        <div className="brand-name">
          Couchbase
          <br />
          Agent Operations Manager
        </div>
        <ThemeToggle />
      </div>

      {[...NAV_SECTIONS, ...(user?.role === "admin" ? [SETTINGS_SECTION] : [])].map((section) => (
        <div className="nav-section" key={section.label}>
          <div className="nav-section-label">{section.label}</div>
          {section.items.map((item) =>
            item.external ? (
              <a key={item.to} href={item.to} target="_blank" rel="noopener noreferrer" className="nav-item">
                <span className="nav-icon">{item.icon}</span>
                <span>{item.text}</span>
              </a>
            ) : (
              <NavLink
                key={item.to}
                to={item.to}
                end={!!item.exact}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
              >
                <span className="nav-icon">{item.icon}</span>
                <span>{item.text}</span>
              </NavLink>
            ),
          )}
        </div>
      ))}

      <div className="sidebar-footer">
        <span className="mode-pill">GATEWAY ACTIVE</span>
        <div className="mode-note">
          Every discover/invoke is re-checked against Couchbase before it's allowed - even if a caller
          skips discovery.
        </div>
        <div
          className="status-pill"
          style={{ marginTop: 10 }}
          title={degraded ? failures.join("\n") : undefined}
        >
          <span className={`status-dot${connected && !degraded ? "" : " down"}`} />
          {statusText}
        </div>
        {user && (
          <div className="session-row">
            <div>
              <div className="session-user">{user.username}</div>
              <div className="session-role">{user.role}{user.source === "ldap" ? " · LDAP" : ""}</div>
            </div>
            <button className="btn btn-secondary btn-sm" onClick={logout}>
              Sign out
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
