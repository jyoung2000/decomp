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
  name: string;
  args?: string[];
  stdin?: string;
}

export interface LaunchProfile {
  execute_original: boolean;
  command?: string[];
  scenarios?: LaunchScenario[];
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
