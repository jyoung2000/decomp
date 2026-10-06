// Types mirror docs/API.md and controller/rebuild_controller/store/schema.sql.
// Fields marked "UI extension" are not (yet) in docs/API.md; the UI degrades gracefully when they are absent.

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface ControllerEvent {
  seq: number;
  ts: string;
  case_id: string | null;
  job_id: string | null;
  kind: string;
  payload: Record<string, unknown>;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  affected?: string;
  next_action?: string;
}

export interface Health {
  ok: boolean;
  version: string;
  pid: number;
  heartbeat_seconds: number;
  latest_seq: number;
  started_at: string;
  /** UI extension: "mock" when served by ui/mock/server.mjs. */
  mode?: string;
}

export type JobState = 'queued' | 'running' | 'blocked' | 'failed' | 'cancelled' | 'completed' | 'needs_retest';

/** Raw counts reported by a job. Percentages are only shown when `total` is a non-null number. */
export interface Progress {
  done?: number | null;
  total?: number | null;
  unit?: string;
  current?: string;
  target?: string;
  [k: string]: unknown;
}

export interface Job {
  job_id: string;
  case_id: string;
  stage: string;
  title: string;
  state: JobState;
  attempt: number;
  max_attempts?: number;
  priority?: number;
  progress: Progress;
  blocker: string | null;
  /** epoch seconds (controller) or ISO string */
  heartbeat_at: number | string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  milestone_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

export type TargetLanguage = 'rust' | 'rust_bevy' | 'web' | 'auto';
export type OutputType = 'exe' | 'installer' | 'portable' | 'web' | 'pwa';
export type AiMode = 'no_ai' | 'assist_on_failure' | 'assisted';

export interface LaunchScenario {
  /** controller key (required by the real controller's capture/feature stages) */
  id?: string;
  title?: string;
  name?: string;
  args?: string[];
  stdin?: string;
  /** cli scenarios as the controller runs them */
  steps?: { args: string[]; stdin?: string }[];
  /** web scenarios: browser actions for the capture harness */
  actions?: unknown[];
  feature_id?: string;
}

export type LaunchKind = 'cli' | 'web';

export interface LaunchProfile {
  execute_original: boolean;
  /** documented in docs/API.md */
  command?: string[];
  scenarios?: LaunchScenario[];
  /** used by the real controller: how the original runs ("cli" default, or "web") */
  kind?: LaunchKind;
  /** used by the real controller: cli → {type:"command", command:[..]}; web → {root, entry} */
  launch?: { type?: string; command?: string[]; root?: string; entry?: string };
}

/**
 * Derived by the controller (controller/rebuild_controller/outcome.py). Pipeline progress (steps done) and verified behaviour
 * (declared scenarios passed) are separate on purpose: a delivered file is not a behavioural match.
 */
export type OutcomeState = 'fully_matched' | 'partially_matched' | 'tested' | 'scaffolded' | 'delivered' | 'built' | 'recovered' | 'untested';
export type VerificationState = 'untested' | 'tested' | 'partially_matched' | 'fully_matched';

export interface Outcome {
  state: OutcomeState;
  label: string;
  /** true only for fully_matched; the only state in which "complete" or a full bar may be shown */
  can_claim_complete: boolean;
  scaffold_only: boolean;
  verification: {
    state: VerificationState;
    verdict?: string;
    declared: number;
    passed: number;
    failed: number;
    untested: number;
    stale: boolean;
    text?: string;
    scope?: string;
    /** UI extension: 'features' when scenario counts were not reported and the UI estimated from feature results */
    unit?: 'scenarios' | 'features';
  };
  pipeline: { recovered: boolean; scaffolded: boolean; built: boolean; delivered: boolean; steps_done: number; steps_total: number; text?: string };
  coverage: {
    features_total: number;
    /** null = unknown (estimated outcome) */
    features_without_oracle: number | null;
    features_without_oracle_titles?: string[];
    unsupported_modules: number;
    unsupported_modules_titles?: string[];
    unknown_scope: boolean;
  };
  outstanding: string[];
  scope_statement?: string;
  /** UI extension: 'estimated' when derived by the app instead of reported by the controller */
  source?: 'controller' | 'estimated';
}

export interface CaseCounts {
  jobs?: Partial<Record<JobState, number>>;
  features?: Record<string, number>;
  candidates?: number;
  previews?: number;
  open_feedback?: number;
}

export interface ResourceUsage {
  cpu_percent?: number | null;
  memory_mb?: number | null;
  disk_mb?: number | null;
  [k: string]: unknown;
}

export interface Case {
  case_id: string;
  name: string;
  source_root: string;
  output_root: string;
  target_language: TargetLanguage;
  output_type: OutputType;
  ai_policy: { mode: AiMode; budget_usd?: number | null };
  launch_profile: LaunchProfile;
  status: string;
  created_at: string;
  updated_at: string;
  app_version?: string;
  settings?: Record<string, unknown>;
  counts?: CaseCounts;
  /** derived by the controller; absent from older controllers and the mock server */
  outcome?: Outcome | null;
  /** UI extension: time the current run started. */
  started_at?: string | null;
  /** UI extension: resource usage snapshot. */
  resources?: ResourceUsage | null;
}

export interface NewCaseBody {
  name: string;
  source_root: string;
  output_root: string;
  target_language: TargetLanguage;
  output_type: OutputType;
  ai_policy: { mode: AiMode; budget_usd?: number };
  launch_profile: LaunchProfile;
  /** stored in cases.settings by the controller */
  settings?: { limits?: Record<string, number> };
}

export const PHASES = ['analysis', 'recovery', 'implementation', 'build', 'verification'] as const;
export type Phase = (typeof PHASES)[number];

export interface PhaseProgress {
  done: number | null;
  total: number | null;
  unit?: string;
}

export interface Eta {
  seconds: number;
  /** seconds (±) or free text */
  uncertainty: number | string | null;
  updated_at: string;
}

export type PlanStatus = 'completed' | 'running' | 'queued' | 'blocked' | 'failed' | 'cancelled' | 'needs_retest';
export type PlanKind = 'milestone' | 'deliverable' | 'feature' | 'discovery' | 'deferred' | 'unsupported';

export type AcceptanceCheck = string | { id?: string; description?: string; command?: string; status?: string };

export interface PlanItem {
  item_id: string;
  parent_id: string | null;
  title: string;
  outcome: string;
  kind: PlanKind;
  status: PlanStatus;
  owner: string | null;
  depends_on: string[];
  acceptance: AcceptanceCheck[];
  evidence_ids: string[];
  files: string[];
  preview_id: string | null;
  blockers: string[];
  feature_id: string | null;
  job_ids: string[];
  sort_order: number;
  updated_at?: string;
}

export type UnknownScopeEntry = string | { id?: string; title?: string; reason?: string };

export interface Plan {
  revision: number;
  items: PlanItem[];
  unknown_scope: UnknownScopeEntry[];
  progress: Partial<Record<Phase | 'discovery', PhaseProgress>>;
  eta: Eta | null;
  outcome?: Outcome | null;
}

export interface PlanRevision {
  revision: number;
  reason: string;
  created_at: string;
  changes?: string[];
}

export interface Feature {
  feature_id: string;
  case_id?: string;
  title: string;
  description?: string;
  origin?: string;
  critical: boolean | number;
  impl_status: string;
  verify_status: string;
  verify_candidate: string | null;
  user_review: string | null;
}

export interface Candidate {
  candidate_id: string;
  case_id: string;
  revision: number;
  target_language: string;
  output_type: string;
  source_dir: string;
  dist_dir: string | null;
  build_hash: string | null;
  plan_revision: number;
  build_status: string;
  verification: string;
  last_known_good: boolean | number;
  created_at: string;
  manifest?: Record<string, unknown>;
  meta?: Record<string, unknown>;
}

export type PreviewKind = 'real' | 'mockup' | 'recording' | 'fixture';

export interface Preview {
  preview_id: string;
  case_id: string;
  candidate_id: string;
  plan_revision: number;
  kind: PreviewKind;
  title: string;
  available: string[];
  incomplete: string[];
  requirements: string[];
  steps: string[];
  launch: { kind?: string; url?: string; command?: string[] | string } | Record<string, unknown>;
  build_hash: string | null;
  verification: string;
  stale: boolean | number;
  /** UI extension */
  stale_reason?: string | null;
  /** UI extension: ids of feature rows this preview exercises */
  feature_ids?: string[];
  /** UI extension: running instance id if open */
  instance_id?: string | null;
  created_at: string;
}

export interface PreviewOpenResult {
  kind: 'browser' | 'native';
  url?: string;
  command?: string[] | string;
  instance_id: string;
}

export interface Artifact {
  name?: string;
  role?: string;
  path?: string;
  url?: string;
  media_type?: string;
  sha256?: string;
}

export type Verdict = 'pass' | 'fail' | 'error' | 'skipped';

export interface Comparison {
  comparison_id: string;
  case_id?: string;
  candidate_id: string;
  feature_id: string | null;
  channel: string;
  rule: string;
  tolerance: Record<string, unknown>;
  verdict: Verdict;
  original_hash: string | null;
  candidate_hash: string | null;
  details: string | Record<string, unknown>;
  command?: string;
  environment?: Record<string, unknown>;
  artifacts: Artifact[];
  created_at: string;
}

export type FeedbackStatus = 'received' | 'triaged' | 'queued' | 'in_progress' | 'ready_to_retest' | 'resolved' | 'reopened';
export type FeedbackClass = 'bug' | 'change' | 'question' | 'acceptance';

export interface FeedbackHistoryEntry {
  ts: string;
  status?: string;
  note?: string;
  actor?: string;
  /** real controller name for the actor */
  by?: string;
}

export interface Feedback {
  feedback_id: string;
  case_id: string;
  target_kind: string;
  target_id: string;
  candidate_id: string | null;
  plan_revision: number | null;
  classification: FeedbackClass;
  priority: string;
  comment: string;
  expected: string;
  actual: string;
  attachments: { name: string; size?: number; sha256?: string }[];
  context: { build_hash?: string | null; fixed_candidate_id?: string | null; [k: string]: unknown };
  status: FeedbackStatus;
  linked_items: string[];
  history: FeedbackHistoryEntry[];
  created_at: string;
  updated_at: string;
}

export interface NewFeedbackBody {
  target_kind: string;
  target_id: string;
  candidate_id?: string;
  classification: FeedbackClass;
  priority: string;
  comment: string;
  expected?: string;
  actual?: string;
  attachments?: { name: string; bytes_b64: string }[];
}

export interface Connection {
  connection_id: string;
  provider: string;
  label: string;
  endpoint: string;
  auth_mode: 'api_key' | 'subscription_handoff' | 'local' | 'none' | string;
  models: string[];
  capabilities?: Record<string, unknown>;
  limits?: { text?: string; [k: string]: unknown };
  state: string;
  last_probe?: string | null;
  created_at?: string;
}

export interface RouteFallback {
  connection: string;
  model: string;
}

export interface TaskRoute {
  task: string;
  primary_connection: string | null;
  primary_model: string | null;
  fallbacks: RouteFallback[];
  updated_at?: string;
}

export interface Budget {
  budget_id: string;
  scope: string;
  limit_usd: number;
  reserved_usd: number;
  spent_usd: number;
  currency?: string;
  updated_at?: string;
}

export interface AiCall {
  call_id: string;
  case_id?: string | null;
  job_id?: string | null;
  provider: string;
  model: string;
  task: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens?: number;
  cost_usd: number | null;
  cost_known: boolean | number;
  outcome: string;
  latency_ms?: number | null;
  created_at: string;
}

export type KnowledgeState = 'proposed' | 'validating' | 'promoted' | 'quarantined' | 'rolled_back';

export interface KnowledgeEntry {
  knowledge_id: string;
  kind: string;
  name: string;
  version: number;
  state: KnowledgeState;
  constraints: Record<string, unknown>;
  author: string;
  source: string;
  confidence: number;
  evidence: string[];
  regression: { passed?: number; failed?: number; cases?: { id: string; verdict: string; detail?: string }[]; [k: string]: unknown };
  lineage: string | null;
  created_at: string;
  updated_at: string;
}

export type Availability = 'missing' | 'detected' | 'installed' | 'usable' | 'verified';

export interface ToolProbe {
  name: string;
  availability: Availability;
  path?: string | null;
  version?: string | null;
  detail?: string;
  prerequisites?: string[];
  license?: string;
  source?: string;
  pinned?: string;
  integrity?: string;
  integration?: string;
  /** UI extension */
  size_bytes?: number | null;
}

export interface BackendEntry {
  backend_id: string;
  title: string;
  formats: string[];
  platforms: string[];
  profiles: string[];
  tools: ToolProbe[];
  resources: Record<string, unknown>;
  tested_support: string[];
  experimental: boolean;
  availability: Availability;
  smoke?: Record<string, unknown>;
}

export interface DoctorReport {
  backends: BackendEntry[];
  summary: Partial<Record<Availability, number>>;
}

export interface Module {
  module_id: string;
  rel_path: string;
  sha256: string;
  size: number;
  format: string;
  profile: string;
  arch: string | null;
}

export interface Evidence {
  evidence_id: string;
  case_id?: string;
  kind: string;
  module_id: string | null;
  title: string;
  revision: number;
  producer?: string;
  created_at: string;
  stale: boolean | number;
  body?: string | Record<string, unknown>;
}

/** UI extension endpoint GET /capabilities */
export interface Capabilities {
  output_combinations: {
    target_language: TargetLanguage;
    output_type: OutputType;
    state: 'supported' | 'experimental' | 'handoff' | 'unsupported';
    reason?: string;
  }[];
}

export type HermesStatus = Record<string, unknown> & {
  paired?: boolean;
  state?: string;
  diagnostics?: ({ check: string; ok: boolean; detail?: string } | string)[];
};

// ---- guided tool setup (GET /tools/setup)
export type ToolSetupStatus = 'not_installed' | 'installed' | 'corrupt' | 'blocked_unverified' | 'update_available' | 'installing';

export interface ToolSetupError {
  code: string;
  message: string;
  affected?: string | null;
  next_action?: string | null;
  retryable?: boolean;
  url?: string | null;
}

export interface ToolSetupJob {
  phase: 'queued' | 'downloading' | 'verifying' | 'extracting' | 'checking' | 'installing' | 'activating' | 'done' | 'failed' | 'cancelled';
  bytes_done: number;
  bytes_total: number;
  percent: number | null;
  message: string;
  error: ToolSetupError | null;
  finished: boolean;
  chain: string[];
  cancelled: boolean;
}

export interface ToolSetupEntry {
  name: string;
  title: string;
  purpose: string;
  role?: string | null;
  version: string;
  installed_version?: string | null;
  license?: string | null;
  optional: boolean;
  size_bytes?: number | null;
  file_name?: string | null;
  url?: string | null;
  sha256?: string | null;
  /** Large tools whose installer fetches more than the pinned download (e.g. the Rust toolchain). */
  footprint?: { installer_download_bytes: number; disk_bytes: number; note?: string } | null;
  requires: string[];
  status: ToolSetupStatus;
  disk_status: ToolSetupStatus;
  blocked_reason?: string | null;
  install_path: string;
  job: ToolSetupJob | null;
}

export interface ToolSetupSnapshot {
  tools_dir: string;
  lock_path: string | null;
  tools: ToolSetupEntry[];
  any_installed: boolean;
  required_missing: string[];
  busy: boolean;
}


/** GET /cases/{id}/consent/original-execution */
export interface OriginalConsent {
  allowed: boolean;
  at: string | null;
  via: string | null;
  history: { allowed: boolean; at: string; via: string; note?: string }[];
}

/** GET /isolation: what this computer offers (probed by the controller). */
export interface IsolationInfo {
  platform: string;
  network_default?: string;
  network_blocking?: string;
  default_mode?: string;
  modes?: Record<string, { available?: boolean; [k: string]: unknown }>;
}

export type ScenarioStatus = 'no_baseline' | 'changed' | 'not_run' | 'passed' | 'failed';

export interface UserScenarioBody {
  title: string;
  feature_id?: string | null;
  steps: { args: string[]; stdin: string }[];
  compare: { exit_code: boolean; output: boolean; files: boolean };
  normalize: { line_endings: boolean; trailing_spaces: boolean; trim: boolean; ignore_timestamps: boolean };
  timeout?: number;
}

export interface UserScenario extends UserScenarioBody {
  scenario_id: string;
  case_id: string;
  status: ScenarioStatus;
  recorded_at: string | null;
  recorded_evidence: string | null;
  updated_at: string;
}

export interface ScenarioCounts {
  declared: number;
  no_baseline: number;
  changed: number;
  recorded: number;
  passed: number;
  failed: number;
  not_run: number;
}

export interface ScenarioList {
  scenarios: UserScenario[];
  counts: ScenarioCounts;
  consent: OriginalConsent;
}

export interface RecordResult extends ScenarioList {
  evidence_id: string;
  baseline_revision: number;
  recorded: string[];
  scenarios_in_baseline: number;
}
