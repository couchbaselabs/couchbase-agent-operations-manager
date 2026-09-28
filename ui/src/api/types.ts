export interface Role {
  id: string;
  description: string;
}

export interface SdkInfo {
  version: string;
  filename: string;
  size_bytes: number;
}

export interface SkillInfo {
  platform: string;
  label: string;
  version: string;
  filename: string;
  size_bytes: number;
}

export type DownstreamAuthMode = "none" | "bearer" | "header" | "basic";

/** How this gateway authenticates to one registered MCP server. The
 * credential itself is never returned by the API - `secret_configured` is
 * all the dashboard is told about it. */
export interface DownstreamAuth {
  mode: DownstreamAuthMode;
  header_name: string;
  username: string;
  secret_configured: boolean;
}

export interface ServerDoc {
  server_id: string;
  label: string;
  owner: string;
  mcp_url: string;
  trust_status: "trusted" | "untrusted";
  default_allowed_roles: string[];
  auth?: DownstreamAuth;
  seeded?: boolean;
  tool_count: number;
}

export interface HijackSignal {
  pattern_id: string;
  category: string;
  severity: "critical" | "high" | "medium";
  matched_text: string;
}

export interface ToolDoc {
  tool_id: string;
  server_id: string;
  name: string;
  description: string;
  allowed_roles: string[];
  risk_level: string;
  trust_status: string;
  hijack_status?: "clear" | "flagged";
  hijack_severity?: string | null;
  hijack_signals?: HijackSignal[];
  hijack_scanned_at?: string;
  hijack_manual_override?: "trusted" | "quarantined";
  drift_status?: "clear" | "drifted";
  drift_changes?: DriftChange[];
  drift_detected_at?: string;
  approved_at?: string;
  definition_version?: number;
}

/** One facet of a tool definition that moved after it was approved. */
export interface DriftChange {
  kind: "schema" | "description" | "name";
  detail: string;
  added_properties?: string[];
  removed_properties?: string[];
  diff?: string[];
}

export interface DriftedTool {
  tool_id: string;
  server_id: string;
  name: string;
  risk_level: string;
  trust_status: string;
  drift_status: string;
  drift_changes: DriftChange[];
  drift_detected_at?: string;
  approved_at?: string;
  approved_definition?: { name?: string; description?: string; input_schema?: Record<string, unknown> };
  definition_version?: number;
  description?: string;
  severity: "critical" | "high";
}

export interface AuditLogEntry {
  timestamp: string;
  action: "authenticate" | "discover" | "invoke";
  role: string | null;
  subject: string | null;
  query: string | null;
  tool_id: string | null;
  server_id: string | null;
  decision: "ALLOW" | "DENY" | "ERROR";
  reason: string;
  latency_ms: number;
  hijack_flagged?: boolean;
  hijack_severity?: string | null;
  hijack_signals?: HijackSignal[];
}

export interface Finding {
  id: string;
  severity: "critical" | "high" | "medium" | "low" | "info";
  title: string;
  summary: string;
  tags: string[];
}

export interface DashboardSummary {
  registered_servers: number;
  trusted_servers: number;
  tools_ingested: number;
  quarantined_tools: number;
  roles: number;
  access_events_24h: number;
  deny_rate_pct: number;
  open_findings: number;
}

export interface HourlyVolumePoint {
  hour: string;
  timestamp: string;
  count: number;
}

export interface DashboardResponse {
  generated_at: string;
  events_examined: number;
  summary: DashboardSummary;
  decision_breakdown: { ALLOW: number; DENY: number; ERROR: number };
  action_breakdown: { discover: number; invoke: number; authenticate: number };
  hourly_volume: HourlyVolumePoint[];
  top_findings: Finding[];
}

export interface DiscoveredTool {
  tool_id: string;
  name: string;
  description: string;
  server_id: string;
  risk_level: string;
  similarity: number;
}

export interface HealthResponse {
  status: string;
  appliance: string;
  couchbase_connected: boolean;
  embeddings_ready: boolean;
  // Set when the appliance came up but one or more startup steps failed -
  // it is serving, with some data missing or stale. Optional so a dashboard
  // built after an older API build still type-checks against it.
  degraded?: boolean;
  startup_failures?: string[];
  timestamp: string;
}

export interface HijackScanResult {
  flagged: boolean;
  severity: string | null;
  signals: HijackSignal[];
}

export interface ThreatDetectionResponse {
  last_scan_at: string | null;
  scan_interval_minutes: number;
  chain_window_seconds: number;
  quarantined_tools: ToolDoc[];
  flagged_responses: AuditLogEntry[];
  chain_findings: Finding[];
  drifted_tools: DriftedTool[];
}

// --- LLM response caching for agents ---------------------------------------

export interface LLMModelOption {
  id: string;
  input_usd_per_1m: number;
  output_usd_per_1m: number;
}

export interface LLMProvider {
  id: string;
  label: string;
  vendor: string;
  env_key: string;
  docs_url: string;
  default_model: string;
  api_key_configured: boolean;
  models: LLMModelOption[];
}

export interface LLMCacheConfig {
  enabled: boolean;
  provider: string;
  model: string;
  fallback_provider: string | null;
  max_output_tokens: number;
  temperature: number;
  semantic_enabled: boolean;
  similarity_threshold: number;
  semantic_candidates: number;
  ttl_seconds: number;
  stale_while_revalidate_seconds: number;
  max_entries: number;
  eviction_policy: string;
  max_reuse_hits: number;
  invalidate_on_model_change: boolean;
  invalidate_on_config_change: boolean;
  invalidate_on_catalog_change: boolean;
  cache_scope: string;
  namespace: string;
  bypass_patterns: string[];
  no_cache_roles: string[];
  sweep_interval_minutes: number;
}

export interface LLMConfigResponse {
  config: LLMCacheConfig;
  config_version: string;
  defaults: LLMCacheConfig;
  cache_scopes: string[];
  eviction_policies: string[];
  last_sweep_at: string | null;
  cached_entries: number;
}

export interface LLMCacheEntry {
  entry_id: string;
  provider: string;
  model: string;
  scope_key: string;
  namespace: string;
  prompt_preview: string;
  response_preview: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  created_at: string;
  last_hit_at: string | null;
  hit_count: number;
  exact_hits: number;
  semantic_hits: number;
  tokens_saved: number;
  cost_saved_usd: number;
  origin_latency_ms: number;
  override: boolean;
  stub: boolean;
  state: "fresh" | "stale" | "invalid";
  state_reason: string | null;
  age_seconds: number;
}

export interface LLMCacheEvent {
  timestamp: string;
  outcome: "hit_exact" | "hit_semantic" | "miss" | "bypass" | "error";
  provider: string;
  model: string;
  role: string | null;
  subject: string | null;
  entry_id: string | null;
  similarity: number | null;
  prompt_preview: string;
  total_tokens?: number;
  tokens_saved?: number;
  cost_usd?: number;
  cost_saved_usd?: number;
  latency_ms: number;
  latency_saved_ms?: number;
  reason: string;
}

export interface LLMCacheSummary {
  requests: number;
  cacheable_requests: number;
  hits: number;
  exact_hits: number;
  semantic_hits: number;
  misses: number;
  bypasses: number;
  errors: number;
  hit_rate_pct: number;
  tokens_saved: number;
  tokens_spent: number;
  cost_saved_usd: number;
  cost_spent_usd: number;
  latency_saved_ms: number;
  avg_hit_latency_ms: number;
  avg_miss_latency_ms: number;
}

export interface LLMHourlyPoint {
  hour: string;
  timestamp: string;
  hits: number;
  misses: number;
}

export interface LLMModelBreakdownRow {
  provider: string;
  provider_label: string;
  model: string;
  requests: number;
  hits: number;
  misses: number;
  hit_rate_pct: number;
  tokens_saved: number;
  tokens_spent: number;
  cost_saved_usd: number;
  cost_spent_usd: number;
}

export interface LLMDashboardResponse {
  generated_at: string;
  events_examined: number;
  tokens_saved_total: number;
  cost_saved_usd_total: number;
  enabled: boolean;
  provider: string;
  provider_label: string;
  model: string;
  api_key_configured: boolean;
  semantic_enabled: boolean;
  similarity_threshold: number;
  ttl_seconds: number;
  cached_entries: number;
  max_entries: number;
  last_sweep_at: string | null;
  summary: LLMCacheSummary;
  hourly: LLMHourlyPoint[];
  model_breakdown: LLMModelBreakdownRow[];
  recent_events: LLMCacheEvent[];
}

export interface LLMCompleteResponse {
  provider: string;
  model: string;
  role: string;
  response: string;
  cache: {
    status: "hit_exact" | "hit_semantic" | "miss" | "bypass";
    entry_id: string | null;
    similarity: number | null;
    hit_count: number;
    created_at: string | null;
    reason: string | null;
  };
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  cost_usd: number;
  tokens_saved: number;
  cost_saved_usd: number;
  latency_ms: number;
  stub: boolean;
}

// -- Context caching (arbitrary agent-fetched data, not LLM completions) --
// Same shape family as the LLM cache types above, minus the token/dollar
// economics (context values are opaque, so "latency avoided" is the value
// metric) and minus semantic matching (see app/context_cache.py).

export interface ContextCacheConfig {
  enabled: boolean;
  ttl_seconds: number;
  max_entries: number;
  eviction_policy: string;
  cache_scope: string;
  namespace: string;
  max_value_bytes: number;
  sweep_interval_minutes: number;
}

export interface ContextConfigResponse {
  config: ContextCacheConfig;
  defaults: ContextCacheConfig;
  cache_scopes: string[];
  eviction_policies: string[];
  last_sweep_at: string | null;
  cached_entries: number;
}

export interface ContextCacheEntry {
  entry_id: string;
  namespace: string;
  scope_key: string;
  subject: string | null;
  role: string | null;
  key_preview: string;
  value_preview: string;
  value_bytes: number;
  created_at: string;
  last_hit_at: string | null;
  hit_count: number;
  ttl_seconds: number;
  origin_latency_ms: number;
  state: "fresh" | "invalid";
  state_reason: string | null;
  age_seconds: number;
}

export interface ContextCacheEvent {
  timestamp: string;
  outcome: "hit" | "miss" | "write";
  namespace: string;
  scope_key: string;
  subject: string | null;
  role: string | null;
  key_preview: string;
  latency_ms: number;
  latency_saved_ms: number;
  value_bytes: number;
}

export interface ContextCacheSummary {
  lookups: number;
  hits: number;
  misses: number;
  writes: number;
  hit_rate_pct: number;
  latency_saved_ms: number;
  avg_hit_latency_ms: number;
  avg_miss_latency_ms: number;
  bytes_cached: number;
}

export interface ContextHourlyPoint {
  hour: string;
  timestamp: string;
  hits: number;
  misses: number;
}

export interface ContextAgentBreakdownRow {
  agent: string;
  namespace: string;
  lookups: number;
  hits: number;
  misses: number;
  writes: number;
  latency_saved_ms: number;
  hit_rate_pct: number;
}

export interface ContextDashboardResponse {
  generated_at: string;
  enabled: boolean;
  ttl_seconds: number;
  cache_scope: string;
  cached_entries: number;
  max_entries: number;
  last_sweep_at: string | null;
  lookups_total: number;
  hits_total: number;
  latency_saved_ms_total: number;
  summary: ContextCacheSummary;
  hourly: ContextHourlyPoint[];
  agent_breakdown: ContextAgentBreakdownRow[];
  recent_events: ContextCacheEvent[];
}


// --- Local dashboard login ---------------------------------------------

export interface AuthUser {
  username: string;
  role: string;
  source: "local" | "ldap";
  active: boolean;
  must_change_password: boolean;
  has_password: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_login_at: string | null;
}

export interface AuthRole {
  id: string;
  description: string;
}

export interface LdapConfig {
  enabled: boolean;
  host: string;
  port: number;
  use_ssl: boolean;
  start_tls: boolean;
  bind_dn: string;
  bind_password_set: boolean;
  user_search_base: string;
  user_search_filter: string;
  admin_group_dn: string;
  group_member_attribute: string;
  ca_certificate: string;
  ca_certificate_info: CaCertificateInfo | null;
}

// -- SIEM / log-forwarding destinations (Audit Log page) --------------------
// Each destination's config is a loose bag of vendor-specific fields (see
// operations-manager/app/siem_forwarding.py DEFAULT_DESTINATIONS) - always
// includes "enabled", plus one "<field>_set": boolean per secret field the
// backend never returns in plaintext.
export interface SiemDestinationConfig {
  enabled: boolean;
  [field: string]: string | number | boolean;
}

export interface SiemDeliveryStatus {
  status: "ok" | "error";
  detail: string;
  at: string;
}

export interface SiemConfigResponse {
  config: Record<string, SiemDestinationConfig>;
  status: Record<string, SiemDeliveryStatus>;
  vendors: Record<string, string>;
}

// -- Dashboard topology diagram ---------------------------------------------
// Live connectivity graph: which RBAC roles have actually reached which MCP
// tool servers and LLM providers within the lookback window (see
// GET /v1/topology) - not just what's registered, but what's active.
export interface TopologyAgent {
  role: string;
  description: string;
  tool_calls: number;
  llm_calls: number;
  last_active_at: string | null;
}

export interface TopologyServer {
  server_id: string;
  label: string;
  owner: string | null;
  trust_status: string;
  tool_count: number;
  calls: number;
  last_active_at: string | null;
}

export interface TopologyLlmProvider {
  provider: string;
  label: string;
  vendor: string;
  configured: boolean;
  caching_enabled: boolean;
  is_default: boolean;
  calls: number;
  last_active_at: string | null;
}

export interface TopologyEdge {
  role: string;
  server_id?: string;
  provider?: string;
  count: number;
  last_at: string | null;
}

export interface TopologyResponse {
  generated_at: string;
  window_hours: number;
  agents: TopologyAgent[];
  servers: TopologyServer[];
  llm_providers: TopologyLlmProvider[];
  edges: { agent_server: TopologyEdge[]; agent_llm: TopologyEdge[] };
}

export interface CaCertificateInfo {
  subject: string;
  issuer: string;
  not_valid_before: string;
  not_valid_after: string;
  is_expired: boolean;
  error?: string;
}

export interface ServerCertificateInfo {
  subject: string;
  issuer: string;
  not_valid_before: string;
  not_valid_after: string;
  is_expired: boolean;
  is_self_signed: boolean;
  subject_alt_names: string[];
}

// --- Agent run traces -------------------------------------------------------

export type SpanKind = "user" | "llm" | "tool_call" | "tool_result" | "hand_off" | "internal" | "assistant";

export interface TraceSpan {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  kind: SpanKind;
  name: string;
  status: "ok" | "error";
  started_at: string;
  ended_at: string;
  started_epoch_ms: number;
  latency_ms: number;
  role: string | null;
  subject: string | null;
  agent_id: string | null;
  session_id: string | null;
  attributes: Record<string, unknown>;
  error: string | null;
}

export interface TraceRun {
  trace_id: string;
  started_at: string | null;
  ended_at: string | null;
  latency_ms: number;
  span_count: number;
  error_count: number;
  status: "ok" | "error";
  role: string | null;
  subject: string | null;
  agent_id: string | null;
  session_id: string | null;
  kinds: Record<string, number>;
  tools_called: string[];
  tools_denied: string[];
  models_called: string[];
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  cache_hits: number;
  cache_misses: number;
  memory_recalls: number;
  hijack_flags: number;
  limit_blocks: number;
  first_query: string | null;
}

export interface TraceRoleAggregate {
  role: string | null;
  runs: number;
  errors: number;
  tokens: number;
  cost_usd: number;
  cache_hits: number;
  hijack_flags: number;
  limit_blocks: number;
}

export interface TracesResponse {
  runs: TraceRun[];
  by_role: TraceRoleAggregate[];
  totals: {
    runs: number;
    errors: number;
    tokens: number;
    cost_usd: number;
    cache_hits: number;
    hijack_flags: number;
    limit_blocks: number;
  };
  window_hours: number;
  tracing_enabled: boolean;
  span_kinds: SpanKind[];
}

/** A run, its spans in order, and the current catalog state of every tool it
 * touched - the join that only works because traces and the catalog live in
 * the same cluster. */
export interface TraceDetailResponse {
  run: TraceRun | null;
  spans: TraceSpan[];
  tools: Array<{
    tool_id: string;
    server_id: string;
    risk_level: string;
    trust_status: string;
    drift_status?: string;
    allowed_roles: string[];
    definition_version?: number;
  }>;
}

// --- Evaluation -------------------------------------------------------------

export type EvalCaseKind = "discover" | "invoke" | "complete";

export interface EvalCase {
  case_id: string;
  kind: EvalCaseKind;
  description: string;
  role: string;
  weight: number;
  query?: string;
  top_k?: number;
  expect_tools?: string[];
  forbid_tools?: string[];
  tool_id?: string;
  arguments?: Record<string, unknown>;
  expect_decision?: "ALLOW" | "DENY";
  prompt?: string;
  expect_contains?: string[];
  expect_similar_to?: string;
  similarity_threshold?: number;
}

export interface EvalDataset {
  dataset_id: string;
  name: string;
  description: string;
  enabled: boolean;
  run_on_catalog_change: boolean;
  cases: EvalCase[];
  created_at: string;
  updated_at: string;
  latest_run?: EvalRunSummary | null;
}

export interface EvalCaseResult {
  case_id: string;
  kind: EvalCaseKind;
  description: string;
  role: string | null;
  weight: number;
  status: "passed" | "failed" | "error";
  score: number;
  detail: string;
  observed: Record<string, unknown>;
  error: string | null;
  latency_ms: number;
}

export interface EvalComparison {
  trend: "improved" | "stable" | "regressed" | "baseline";
  delta: number;
  previous_run_id: string | null;
  previous_score: number | null;
  newly_failing: string[];
  newly_passing: string[];
  summary: string;
}

export interface EvalRunSummary {
  run_id: string;
  dataset_id: string;
  dataset_name: string;
  trigger: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  score: number;
  case_count: number;
  passed: number;
  failed: number;
  errored: number;
  status: "passed" | "failed" | "error";
  comparison?: EvalComparison;
}

export interface EvalRun extends EvalRunSummary {
  results: EvalCaseResult[];
  catalog_version?: string;
}

export interface EvalsResponse {
  datasets: EvalDataset[];
  runs: EvalRunSummary[];
  case_kinds: EvalCaseKind[];
  run_on_catalog_change: boolean;
  last_gate_at: string | null;
}

// --- Limits and budgets -----------------------------------------------------

export interface GovernanceConfig {
  enabled: boolean;
  enforce: boolean;
  requests_per_minute: number;
  tool_calls_per_minute: number;
  tool_calls_per_run: number;
  tokens_per_hour: number;
  spend_per_day_usd: number;
  exempt_roles: string[];
  downstream_timeout_seconds: number;
  llm_timeout_seconds: number;
  request_timeout_seconds: number;
}

export interface LimitVerdict {
  family: string;
  label: string;
  limit: number;
  used: number;
  remaining: number | null;
  exceeded: boolean;
  blocked: boolean;
  reason: string | null;
  retry_after_seconds: number | null;
}

export interface GovernanceUsageRow {
  subject: string;
  role: string;
  limits: LimitVerdict[];
}

export interface GovernanceConfigResponse {
  config: GovernanceConfig;
  defaults: GovernanceConfig;
  window_seconds: Record<string, number>;
  roles: string[];
  usage: GovernanceUsageRow[];
}

// --- Guardrails and PII -----------------------------------------------------

export interface GuardrailsConfig {
  enabled: boolean;
  detectors: string[];
  redact_audit_log: boolean;
  redact_traces: boolean;
  redact_memory: boolean;
  never_cache_pii: boolean;
  scan_prompts: boolean;
  scan_tool_arguments: boolean;
  block_injection_at: "off" | "critical" | "high" | "medium";
  custom_patterns: string[];
}

export interface DetectorInfo {
  id: string;
  label: string;
  severity: string;
}

export interface GuardrailsConfigResponse {
  config: GuardrailsConfig;
  defaults: GuardrailsConfig;
  detectors: DetectorInfo[];
  block_levels: string[];
}

/** A detected value is reported by class and fingerprint, never by its text -
 * this result is itself stored and displayed. */
export interface PiiMatch {
  detector: string;
  label: string;
  severity: string;
  fingerprint: string;
}

export interface GuardrailsTestResponse {
  pii: { found: boolean; severity: string | null; matches: PiiMatch[] };
  injection: { flagged: boolean; severity: string | null; signals: HijackSignal[] };
  blocked: boolean;
  redacted: string;
  summary: string;
}

// --- Human approval tier ----------------------------------------------------

export type ApprovalStatus = "pending" | "approved" | "denied" | "consumed" | "expired";

export interface Approval {
  approval_id: string;
  status: ApprovalStatus;
  tool_id: string;
  server_id: string | null;
  risk_level: string | null;
  arguments: Record<string, unknown>;
  role: string;
  subject: string;
  trace_id: string | null;
  requested_reason: string;
  requested_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  consumed_at: string | null;
  usable_until?: string;
}

export interface ApprovalsResponse {
  approvals: Approval[];
  config: ApprovalConfig;
  statuses: ApprovalStatus[];
  pending_count: number;
}

export interface ApprovalConfig {
  enabled: boolean;
  require_at_risk_level: string;
  require_for_tools: string[];
  exempt_roles: string[];
  ttl_seconds: number;
  grace_seconds: number;
}

export interface ApprovalConfigResponse {
  config: ApprovalConfig;
  defaults: ApprovalConfig;
  risk_levels: string[];
  roles: string[];
}

// --- Knowledge base ---------------------------------------------------------

export interface KnowledgeDocument {
  document_id: string;
  title: string;
  source: string;
  format: string;
  allowed_roles: string[];
  chunk_count: number;
  char_count: number;
  fingerprint: string;
  uploaded_by: string | null;
  metadata: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeResponse {
  documents: KnowledgeDocument[];
  chunk_count: number;
  roles: string[];
  chunk_chars: number;
  chunk_overlap: number;
  max_upload_mb: number;
  supported_extensions: string[];
}

export interface KnowledgeChunkResult {
  chunk_id: string;
  document_id: string;
  document_title: string;
  chunk_index: number;
  content: string;
  score: number;
}

// --- Agent memory management ------------------------------------------------

export type MemoryStatus = "active" | "superseded";

export interface MemoryEntry {
  memory_id: string;
  user_id: string;
  session_id: string;
  memory_type: "conversational" | "profile" | "semantic";
  content: string;
  metadata: Record<string, string>;
  created_at: string;
  updated_at: string;
  status?: MemoryStatus;
  importance?: number;
  importance_band?: "high" | "medium" | "low";
  recall_count?: number;
  reinforcement_count?: number;
  superseded_by?: string | null;
  superseded_at?: string | null;
  consolidated_from?: string[];
  consolidation_kind?: "dedup" | "rollup";
  consolidated_at?: string;
  similarity?: number | null;
}

export interface MemoryUserRow {
  user_id: string;
  total: number;
  superseded: number;
  sessions: number;
  last_updated_at: string;
}

export interface MemoryConfig {
  enabled: boolean;
  dedup_enabled: boolean;
  dedup_similarity_threshold: number;
  rollup_enabled: boolean;
  rollup_memory_types: string[];
  rollup_min_entries: number;
  rollup_min_idle_hours: number;
  importance_enabled: boolean;
  track_recall: boolean;
  interval_minutes: number;
  max_users_per_pass: number;
  retain_superseded_hours: number;
}

export interface ConsolidationReport {
  users_examined: number;
  duplicates_merged: number;
  groups_merged: number;
  sessions_rolled_up: number;
  entries_superseded: number;
  importance_updated: number;
  llm_calls: number;
  cache_hits: number;
  errors: string[];
  trigger?: string;
  finished_at?: string;
}

export interface MemoryUsersResponse {
  users: MemoryUserRow[];
  stats: { total?: number; superseded?: number; users?: number; consolidated?: number };
  config: MemoryConfig;
  last_consolidation_at: string | null;
  last_report: ConsolidationReport | null;
  memory_types: string[];
}

// --- Inbound agent identity -------------------------------------------------

export interface AgentKeySummary {
  key_prefix: string;
  status: "active" | "rotated" | "revoked";
  created_at: string;
  expires_at: string | null;
  label: string;
}

export interface AgentIdentity {
  agent_id: string;
  name: string;
  owner: string;
  description: string;
  role: string;
  allowed_tools: string[];
  status: "active" | "revoked";
  expires_at: string | null;
  created_at: string;
  created_by: string | null;
  last_used_at: string | null;
  use_count: number;
  seeded: boolean;
  expired: boolean;
  keys: AgentKeySummary[];
}

export interface AgentsResponse {
  agents: AgentIdentity[];
  roles: Array<{ id: string; description: string }>;
  rotation_grace_seconds: number;
  oidc: { enabled: boolean; issuer: string; problems: string[] };
}

export interface AgentOidcConfig {
  enabled: boolean;
  issuer: string;
  jwks_uri: string;
  audience: string;
  algorithms: string[];
  role_claim: string;
  role_map: Record<string, string>;
  default_role: string;
  subject_claim: string;
  leeway_seconds: number;
  jwks_cache_seconds: number;
}

export interface AgentOidcConfigResponse {
  config: AgentOidcConfig;
  defaults: AgentOidcConfig;
  problems: string[];
  roles: string[];
  algorithms: string[];
}
