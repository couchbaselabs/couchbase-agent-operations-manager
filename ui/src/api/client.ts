import type {
  AuditLogEntry,
  AuthRole,
  AuthUser,
  DashboardResponse,
  DownstreamAuth,
  EvalDataset,
  EvalRun,
  EvalsResponse,
  Finding,
  GovernanceConfig,
  GovernanceConfigResponse,
  GuardrailsConfig,
  GuardrailsConfigResponse,
  GuardrailsTestResponse,
  Approval,
  ApprovalConfig,
  ApprovalConfigResponse,
  ApprovalsResponse,
  KnowledgeChunkResult,
  KnowledgeDocument,
  KnowledgeResponse,
  ConsolidationReport,
  MemoryConfig,
  MemoryEntry,
  MemoryUsersResponse,
  AgentIdentity,
  AgentOidcConfig,
  AgentOidcConfigResponse,
  AgentsResponse,
  HealthResponse,
  LdapConfig,
  CaCertificateInfo,
  ServerCertificateInfo,
  LLMCacheConfig,
  LLMCacheEntry,
  LLMCompleteResponse,
  LLMConfigResponse,
  LLMDashboardResponse,
  LLMProvider,
  ContextCacheConfig,
  ContextCacheEntry,
  ContextConfigResponse,
  ContextDashboardResponse,
  Role,
  SdkInfo,
  ServerDoc,
  SiemConfigResponse,
  SkillInfo,
  ThreatDetectionResponse,
  ToolDoc,
  TopologyResponse,
  TraceDetailResponse,
  TracesResponse,
} from "./types";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// AuthProvider registers a handler here so any 401 from any call - not
// just the /v1/auth/* ones - drops the app back to the login page (a
// session cookie can expire mid-session on any page, not just on the
// endpoints that issue it).
let onUnauthorized: (() => void) | null = null;

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      // ignore
    }
    if (res.status === 401 && !path.startsWith("/v1/auth/login") && !path.startsWith("/v1/auth/bootstrap")) {
      onUnauthorized?.();
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  setUnauthorizedHandler: (fn: (() => void) | null) => {
    onUnauthorized = fn;
  },

  health: () => request<HealthResponse>("/api/health"),

  roles: () => request<{ roles: Role[] }>("/v1/roles"),

  // -- Local dashboard login ------------------------------------------
  authBootstrapStatus: () => request<{ needs_setup: boolean; username: string }>("/v1/auth/bootstrap-status"),
  authBootstrap: (password: string) =>
    request<{ user: AuthUser }>("/v1/auth/bootstrap", { method: "POST", body: JSON.stringify({ password }) }),
  authLogin: (username: string, password: string) =>
    request<{ user: AuthUser }>("/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  authLogout: () => request<{ logged_out: boolean }>("/v1/auth/logout", { method: "POST" }),
  authMe: () => request<{ user: AuthUser }>("/v1/auth/me"),
  authChangePassword: (currentPassword: string, newPassword: string) =>
    request<{ user: AuthUser }>("/v1/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),
  authRoles: () => request<{ roles: AuthRole[] }>("/v1/auth/roles"),

  authUsers: () => request<{ users: AuthUser[] }>("/v1/auth/users"),
  createAuthUser: (payload: { username: string; password: string; role: string; must_change_password: boolean }) =>
    request<{ user: AuthUser }>("/v1/auth/users", { method: "POST", body: JSON.stringify(payload) }),
  updateAuthUser: (
    username: string,
    patch: { role?: string; active?: boolean; password?: string; must_change_password?: boolean }
  ) =>
    request<{ user: AuthUser }>(`/v1/auth/users/${encodeURIComponent(username)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  deleteAuthUser: (username: string) =>
    request<{ deleted: boolean; username: string }>(`/v1/auth/users/${encodeURIComponent(username)}`, {
      method: "DELETE",
    }),

  ldapConfig: () => request<{ config: LdapConfig }>("/v1/auth/ldap-config"),
  saveLdapConfig: (config: Record<string, unknown>, bindPassword?: string) =>
    request<{ config: LdapConfig }>("/v1/auth/ldap-config", {
      method: "PUT",
      body: JSON.stringify({ config, bind_password: bindPassword || undefined }),
    }),
  testLdapConfig: (username: string, password: string) =>
    request<{ success: boolean; detail: string; would_be_admin: boolean }>("/v1/auth/ldap-config/test", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  validateCaCertificate: (caCertificate: string) =>
    request<{ valid: boolean; info: CaCertificateInfo }>("/v1/auth/ldap-config/validate-ca", {
      method: "POST",
      body: JSON.stringify({ ca_certificate: caCertificate }),
    }),

  siemConfig: () => request<SiemConfigResponse>("/v1/siem/config"),
  saveSiemConfig: (vendor: string, config: Record<string, unknown>) =>
    request<{ config: SiemConfigResponse["config"] }>(`/v1/siem/config/${vendor}`, {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  testSiemConfig: (vendor: string, config?: Record<string, unknown>) =>
    request<{ success: boolean; detail: string }>(`/v1/siem/test/${vendor}`, {
      method: "POST",
      body: JSON.stringify({ config }),
    }),

  tlsCert: () => request<{ info: ServerCertificateInfo | null; can_revert: boolean }>("/v1/auth/tls-cert"),
  validateTlsCert: (certPem: string, keyPem: string) =>
    request<{ valid: boolean; info: ServerCertificateInfo }>("/v1/auth/tls-cert/validate", {
      method: "POST",
      body: JSON.stringify({ cert_pem: certPem, key_pem: keyPem }),
    }),
  installTlsCert: (certPem: string, keyPem: string) =>
    request<{ info: ServerCertificateInfo; can_revert: boolean; restart_required: boolean }>("/v1/auth/tls-cert", {
      method: "PUT",
      body: JSON.stringify({ cert_pem: certPem, key_pem: keyPem }),
    }),
  revertTlsCert: () =>
    request<{ info: ServerCertificateInfo; can_revert: boolean; restart_required: boolean }>(
      "/v1/auth/tls-cert/revert",
      { method: "POST" }
    ),

  sdkInfo: () => request<SdkInfo>("/v1/sdk/info"),
  skillInfo: (platform: string) => request<SkillInfo>(`/v1/skills/${platform}/info`),

  servers: () => request<{ servers: ServerDoc[] }>("/v1/servers"),
  registerServer: (payload: {
    server_id: string;
    label: string;
    owner: string;
    mcp_url: string;
    trust_status: "trusted" | "untrusted";
    default_allowed_roles: string[];
    auth?: Partial<DownstreamAuth>;
    auth_secret?: string;
  }) =>
    request<{
      server: ServerDoc;
      ingested_tools: number;
      drifted_tools: number;
      ingest_error: string | null;
    }>("/v1/servers", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  saveServerAuth: (serverId: string, auth: Partial<DownstreamAuth>, authSecret?: string, clearSecret = false) =>
    request<{ server: ServerDoc }>(`/v1/servers/${encodeURIComponent(serverId)}/auth`, {
      method: "PUT",
      body: JSON.stringify({ auth, auth_secret: authSecret || undefined, clear_secret: clearSecret }),
    }),
  reingestServer: (serverId: string) =>
    request<{ server_id: string; ingested_tools: number; quarantined_tools: number; drifted_tools: number }>(
      `/v1/servers/${encodeURIComponent(serverId)}/reingest`,
      { method: "POST" }
    ),
  deleteServer: (serverId: string) =>
    request<{ deleted: boolean; server_id: string; tools_removed: number }>(
      `/v1/servers/${encodeURIComponent(serverId)}`,
      { method: "DELETE" }
    ),

  catalog: () => request<{ tools: ToolDoc[] }>("/v1/catalog"),

  auditLog: (limit = 100) => request<{ entries: AuditLogEntry[] }>(`/v1/audit-log?limit=${limit}`),

  dashboard: () => request<DashboardResponse>("/v1/dashboard"),
  topology: (windowHours = 24) => request<TopologyResponse>(`/v1/topology?window_hours=${windowHours}`),
  insights: () => request<{ findings: Finding[] }>("/v1/insights"),

  discover: (apiKey: string, query: string, topK: number) =>
    request<{ role: string; tools: import("./types").DiscoveredTool[]; latency_ms: number }>("/v1/tools/discover", {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({ query, top_k: topK }),
    }),

  invoke: (apiKey: string, toolId: string, args: Record<string, unknown>) =>
    request<{
      role: string;
      tool_id: string;
      result: unknown;
      latency_ms: number;
      hijack_warning: import("./types").HijackScanResult | null;
    }>("/v1/tools/invoke", {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({ tool_id: toolId, arguments: args }),
    }),

  // -- LLM response caching --------------------------------------------
  llmProviders: () => request<{ providers: LLMProvider[] }>("/v1/llm/providers"),
  llmConfig: () => request<LLMConfigResponse>("/v1/llm/config"),
  saveLlmConfig: (config: LLMCacheConfig) =>
    request<{ config: LLMCacheConfig; config_version: string; entries_invalidated: number }>("/v1/llm/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  llmDashboard: () => request<LLMDashboardResponse>("/v1/llm/dashboard"),
  llmCacheEntries: (limit = 100) =>
    request<{ entries: LLMCacheEntry[]; count: number; total_entries: number }>(`/v1/llm/cache?limit=${limit}`),
  purgeLlmCache: (filter: { provider?: string; model?: string; namespace?: string } = {}) =>
    request<{ purged: number }>("/v1/llm/cache/purge", { method: "POST", body: JSON.stringify(filter) }),
  sweepLlmCache: () =>
    request<{ removed: number; last_sweep_at: string }>("/v1/llm/cache/sweep", { method: "POST" }),
  deleteLlmCacheEntry: (entryId: string) =>
    request<{ deleted: boolean }>(`/v1/llm/cache/${encodeURIComponent(entryId)}`, { method: "DELETE" }),
  llmComplete: (
    apiKey: string,
    payload: { prompt: string; provider?: string; model?: string; bypass_cache?: boolean }
  ) =>
    request<LLMCompleteResponse>("/v1/llm/complete", {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(payload),
    }),

  // -- Context caching (arbitrary agent-fetched data) -------------------
  contextConfig: () => request<ContextConfigResponse>("/v1/context/config"),
  saveContextConfig: (config: ContextCacheConfig) =>
    request<{ config: ContextCacheConfig }>("/v1/context/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  contextDashboard: () => request<ContextDashboardResponse>("/v1/context/dashboard"),
  contextCacheEntries: (limit = 100) =>
    request<{ entries: ContextCacheEntry[]; count: number; total_entries: number }>(
      `/v1/context/cache?limit=${limit}`
    ),
  purgeContextCache: (filter: { namespace?: string; agent?: string } = {}) =>
    request<{ purged: number }>("/v1/context/cache/purge", { method: "POST", body: JSON.stringify(filter) }),
  sweepContextCache: () =>
    request<{ removed: number; last_sweep_at: string }>("/v1/context/cache/sweep", { method: "POST" }),
  deleteContextCacheEntry: (entryId: string) =>
    request<{ deleted: boolean }>(`/v1/context/cache/${encodeURIComponent(entryId)}`, { method: "DELETE" }),
  contextGet: (apiKey: string, key: string, namespace?: string) =>
    request<{ hit: boolean; value: unknown; cache: Record<string, unknown>; latency_ms: number }>(
      "/v1/context/get",
      {
        method: "POST",
        headers: { Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({ key, namespace }),
      }
    ),
  contextSet: (
    apiKey: string,
    payload: { key: string; value: unknown; namespace?: string; ttl_seconds?: number; source_latency_ms?: number }
  ) =>
    request<{ stored: boolean; entry_id: string; ttl_seconds: number }>("/v1/context/set", {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(payload),
    }),

  threatDetection: () => request<ThreatDetectionResponse>("/v1/threat-detection"),
  releaseTool: (toolId: string) =>
    request<{ tool_id: string; trust_status: string }>(`/v1/tools/${encodeURIComponent(toolId)}/release`, {
      method: "POST",
    }),
  quarantineTool: (toolId: string) =>
    request<{ tool_id: string; trust_status: string }>(`/v1/tools/${encodeURIComponent(toolId)}/quarantine`, {
      method: "POST",
    }),
  clearOverride: (toolId: string) =>
    request<{ tool_id: string; trust_status: string; drift_status?: string }>(
      `/v1/tools/${encodeURIComponent(toolId)}/clear-override`,
      { method: "POST" }
    ),

  // -- Agent run traces -------------------------------------------------
  traces: (params: { limit?: number; role?: string; status?: string; windowHours?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.role) q.set("role", params.role);
    if (params.status) q.set("status", params.status);
    if (params.windowHours) q.set("window_hours", String(params.windowHours));
    const qs = q.toString();
    return request<TracesResponse>(`/v1/traces${qs ? `?${qs}` : ""}`);
  },
  trace: (traceId: string) => request<TraceDetailResponse>(`/v1/traces/${encodeURIComponent(traceId)}`),

  // -- Evaluation --------------------------------------------------------
  evals: () => request<EvalsResponse>("/v1/evals"),
  saveEvalDataset: (dataset: Partial<EvalDataset>) =>
    request<{ dataset: EvalDataset }>("/v1/evals/datasets", {
      method: "PUT",
      body: JSON.stringify({ dataset }),
    }),
  deleteEvalDataset: (datasetId: string) =>
    request<{ deleted: boolean; dataset_id: string }>(`/v1/evals/datasets/${encodeURIComponent(datasetId)}`, {
      method: "DELETE",
    }),
  runEvals: (datasetId?: string) =>
    request<{ runs: EvalRun[] }>("/v1/evals/run", {
      method: "POST",
      body: JSON.stringify({ dataset_id: datasetId || null }),
    }),
  evalRun: (runId: string) => request<{ run: EvalRun }>(`/v1/evals/runs/${encodeURIComponent(runId)}`),

  // -- Limits and budgets ------------------------------------------------
  governanceConfig: () => request<GovernanceConfigResponse>("/v1/governance/config"),
  saveGovernanceConfig: (config: GovernanceConfig) =>
    request<{ config: GovernanceConfig }>("/v1/governance/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),

  // -- Guardrails and PII ------------------------------------------------
  guardrailsConfig: () => request<GuardrailsConfigResponse>("/v1/guardrails/config"),
  saveGuardrailsConfig: (config: GuardrailsConfig) =>
    request<{ config: GuardrailsConfig }>("/v1/guardrails/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  testGuardrails: (text: string) =>
    request<GuardrailsTestResponse>("/v1/guardrails/test", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  // -- Human approval tier -----------------------------------------------
  approvals: (status?: string) =>
    request<ApprovalsResponse>(`/v1/approvals${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  decideApproval: (approvalId: string, approved: boolean, note?: string) =>
    request<{ approval: Approval }>(`/v1/approvals/${encodeURIComponent(approvalId)}/decide`, {
      method: "POST",
      body: JSON.stringify({ approved, note: note || null }),
    }),
  approvalsConfig: () => request<ApprovalConfigResponse>("/v1/approvals-config"),
  saveApprovalsConfig: (config: ApprovalConfig) =>
    request<{ config: ApprovalConfig }>("/v1/approvals-config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),

  // -- Knowledge base ----------------------------------------------------
  knowledge: () => request<KnowledgeResponse>("/v1/knowledge"),
  ingestKnowledge: (payload: {
    title: string;
    content: string;
    encoding?: "text" | "base64";
    filename?: string;
    source?: string;
    allowed_roles: string[];
    metadata?: Record<string, string>;
  }) =>
    request<{ document: KnowledgeDocument }>("/v1/knowledge", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteKnowledge: (documentId: string) =>
    request<{ deleted: boolean; document_id: string; documents_removed: number }>(
      `/v1/knowledge/${encodeURIComponent(documentId)}`,
      { method: "DELETE" }
    ),
  // -- Inbound agent identity --------------------------------------------
  agents: () => request<AgentsResponse>("/v1/agents"),
  createAgent: (payload: {
    name: string;
    role: string;
    owner?: string;
    description?: string;
    allowed_tools?: string[];
    expires_at?: string | null;
  }) =>
    request<{ agent: AgentIdentity; api_key: string; notice: string }>("/v1/agents", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateAgent: (agentId: string, patch: Partial<AgentIdentity> & { allowed_tools?: string[] }) =>
    request<{ agent: AgentIdentity }>(`/v1/agents/${encodeURIComponent(agentId)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  rotateAgentKey: (agentId: string, revokeImmediately = false) =>
    request<{ agent: AgentIdentity; api_key: string; previous_key_valid_until: string | null; notice: string }>(
      `/v1/agents/${encodeURIComponent(agentId)}/rotate`,
      { method: "POST", body: JSON.stringify({ revoke_immediately: revokeImmediately }) }
    ),
  revokeAgent: (agentId: string) =>
    request<{ agent: AgentIdentity }>(`/v1/agents/${encodeURIComponent(agentId)}/revoke`, { method: "POST" }),
  deleteAgent: (agentId: string) =>
    request<{ deleted: boolean; agent_id: string }>(`/v1/agents/${encodeURIComponent(agentId)}`, {
      method: "DELETE",
    }),
  agentOidcConfig: () => request<AgentOidcConfigResponse>("/v1/agents-oidc/config"),
  saveAgentOidcConfig: (config: AgentOidcConfig) =>
    request<{ config: AgentOidcConfig; problems: string[] }>("/v1/agents-oidc/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  testAgentOidc: (token: string) =>
    request<{ valid: boolean; reason: string | null; role: string | null; subject: string | null }>(
      "/v1/agents-oidc/test",
      { method: "POST", body: JSON.stringify({ token }) }
    ),

  // -- Agent memory management -------------------------------------------
  memoryUsers: () => request<MemoryUsersResponse>("/v1/agent-memory/users"),
  memoryEntries: (params: { user_id: string; session_id?: string; memory_type?: string; status?: string }) => {
    const q = new URLSearchParams({ user_id: params.user_id });
    if (params.session_id) q.set("session_id", params.session_id);
    if (params.memory_type) q.set("memory_type", params.memory_type);
    if (params.status) q.set("status", params.status);
    return request<{ user_id: string; entries: MemoryEntry[] }>(`/v1/agent-memory/entries?${q.toString()}`);
  },
  memorySearch: (payload: { user_id: string; query: string; memory_type?: string; top_k?: number }) =>
    request<{ user_id: string; entries: MemoryEntry[] }>("/v1/agent-memory/search", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  editMemory: (memoryId: string, patch: { content?: string; memory_type?: string; metadata?: Record<string, string> }) =>
    request<{ memory_id: string; entry: MemoryEntry }>(
      `/v1/agent-memory/entries/${encodeURIComponent(memoryId)}`,
      { method: "PUT", body: JSON.stringify(patch) }
    ),
  deleteMemory: (memoryId: string) =>
    request<{ deleted: boolean; memory_id: string }>(
      `/v1/agent-memory/entries/${encodeURIComponent(memoryId)}`,
      { method: "DELETE" }
    ),
  forgetMemoryUser: (userId: string) =>
    request<{ forgotten: boolean; user_id: string; entries_removed: number }>("/v1/agent-memory/forget", {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    }),
  memoryConfig: () =>
    request<{ config: MemoryConfig; defaults: MemoryConfig; importance_weights: Record<string, number> }>(
      "/v1/agent-memory/config"
    ),
  saveMemoryConfig: (config: MemoryConfig) =>
    request<{ config: MemoryConfig }>("/v1/agent-memory/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  consolidateMemory: (userId?: string) =>
    request<{ report: ConsolidationReport; last_consolidation_at: string }>("/v1/agent-memory/consolidate", {
      method: "POST",
      body: JSON.stringify({ user_id: userId || null }),
    }),

  searchKnowledge: (apiKey: string, query: string, topK = 5) =>
    request<{ role: string; results: KnowledgeChunkResult[]; latency_ms: number }>(
      "/v1/agent/knowledge/search",
      {
        method: "POST",
        headers: { Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({ query, top_k: topK }),
      }
    ),
};

export { ApiError };
