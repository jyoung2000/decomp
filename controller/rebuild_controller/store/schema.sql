-- Rebuild Studio durable store. Schema version tracked in meta.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Sequenced, durable controller events. seq is the single global ordering used by UI reconnects.
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  case_id TEXT,
  job_id TEXT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id, seq);

CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  source_root TEXT NOT NULL,
  output_root TEXT NOT NULL,
  target_language TEXT NOT NULL,
  output_type TEXT NOT NULL,
  ai_policy TEXT NOT NULL,
  launch_profile TEXT NOT NULL,
  status TEXT NOT NULL,
  app_version TEXT NOT NULL,
  settings TEXT NOT NULL DEFAULT '{}'
);

-- Job DAG. state: queued|running|blocked|failed|cancelled|completed|needs_retest
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  title TEXT NOT NULL,
  inputs TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  lease_owner TEXT,
  lease_expires REAL,
  heartbeat_at REAL,
  progress TEXT NOT NULL DEFAULT '{}',
  result TEXT,
  error TEXT,
  blocker TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  milestone_id TEXT,
  FOREIGN KEY(case_id) REFERENCES cases(case_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_case_state ON jobs(case_id, state);

CREATE TABLE IF NOT EXISTS job_deps (
  job_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY(job_id, depends_on)
);

CREATE TABLE IF NOT EXISTS job_attempts (
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  error TEXT,
  worker TEXT
);

-- Content-addressed evidence. blob_sha points at blobs/<sha>. kind: inventory|module|function|decompile|capture|comparison|...
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  kind TEXT NOT NULL,
  module_id TEXT,
  title TEXT NOT NULL,
  blob_sha TEXT,
  meta TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  producer TEXT NOT NULL,
  created_at TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id, kind);

CREATE TABLE IF NOT EXISTS modules (
  module_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  format TEXT NOT NULL,
  profile TEXT NOT NULL,
  arch TEXT,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_modules_case ON modules(case_id);

-- Feature ledger. status is written ONLY by the verifier for machine_verified; user_reviewed by feedback; others by controller.
CREATE TABLE IF NOT EXISTS features (
  feature_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL,            -- static|runtime|user|discovery
  critical INTEGER NOT NULL DEFAULT 0,
  impl_status TEXT NOT NULL,       -- unplanned|planned|in_progress|runnable|blocked|unsupported|deferred
  verify_status TEXT NOT NULL,     -- untested|verified|partial|failed|stale
  verify_candidate TEXT,
  verify_evidence TEXT,
  user_review TEXT,                -- NULL|accepted|rejected
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  target_language TEXT NOT NULL,
  output_type TEXT NOT NULL,
  source_dir TEXT NOT NULL,
  dist_dir TEXT,
  build_hash TEXT,
  manifest_sha TEXT,
  plan_revision INTEGER NOT NULL,
  build_status TEXT NOT NULL,      -- pending|building|built|failed
  verification TEXT NOT NULL DEFAULT 'untested',
  last_known_good INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS comparisons (
  comparison_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  feature_id TEXT,
  channel TEXT NOT NULL,
  rule TEXT NOT NULL,
  tolerance TEXT NOT NULL DEFAULT '{}',
  verdict TEXT NOT NULL,           -- pass|fail|error|skipped
  original_hash TEXT,
  candidate_hash TEXT,
  details TEXT NOT NULL,
  command TEXT NOT NULL DEFAULT '',
  environment TEXT NOT NULL DEFAULT '{}',
  artifacts TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  writer TEXT NOT NULL             -- must be 'verifier'
);

CREATE TABLE IF NOT EXISTS plan_items (
  item_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  parent_id TEXT,
  title TEXT NOT NULL,
  outcome TEXT NOT NULL,
  kind TEXT NOT NULL,              -- milestone|deliverable|feature|discovery|deferred|unsupported
  status TEXT NOT NULL,            -- queued|running|completed|blocked|failed|cancelled|needs_retest
  owner TEXT,
  depends_on TEXT NOT NULL DEFAULT '[]',
  acceptance TEXT NOT NULL DEFAULT '[]',
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  files TEXT NOT NULL DEFAULT '[]',
  preview_id TEXT,
  blockers TEXT NOT NULL DEFAULT '[]',
  feature_id TEXT,
  job_ids TEXT NOT NULL DEFAULT '[]',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_revisions (
  revision INTEGER NOT NULL,
  case_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(case_id, revision)
);

CREATE TABLE IF NOT EXISTS previews (
  preview_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  plan_revision INTEGER NOT NULL,
  kind TEXT NOT NULL,              -- real|mockup|recording|fixture
  title TEXT NOT NULL,
  available TEXT NOT NULL DEFAULT '[]',
  incomplete TEXT NOT NULL DEFAULT '[]',
  requirements TEXT NOT NULL DEFAULT '[]',
  steps TEXT NOT NULL DEFAULT '[]',
  launch TEXT NOT NULL,
  build_hash TEXT,
  verification TEXT NOT NULL DEFAULT 'untested',
  stale INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  target_kind TEXT NOT NULL,       -- milestone|deliverable|feature|preview|comparison
  target_id TEXT NOT NULL,
  candidate_id TEXT,
  plan_revision INTEGER,
  classification TEXT NOT NULL,    -- bug|change|question|acceptance
  priority TEXT NOT NULL,
  comment TEXT NOT NULL,
  expected TEXT NOT NULL DEFAULT '',
  actual TEXT NOT NULL DEFAULT '',
  attachments TEXT NOT NULL DEFAULT '[]',
  context TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,            -- received|triaged|queued|in_progress|ready_to_retest|resolved|reopened
  linked_items TEXT NOT NULL DEFAULT '[]',
  history TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
  knowledge_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- signature|type_lib|parser|recipe|rewrite|template|replay|fixture
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  state TEXT NOT NULL,             -- proposed|validating|promoted|quarantined|rolled_back
  constraints TEXT NOT NULL,       -- arch/abi/version applicability
  body_sha TEXT NOT NULL,
  author TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence TEXT NOT NULL DEFAULT '[]',
  regression TEXT NOT NULL DEFAULT '{}',
  lineage TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
  budget_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,             -- job:<id>|jev:setup|jev:monthly:<yyyy-mm>|case:<id>
  limit_usd REAL NOT NULL,
  reserved_usd REAL NOT NULL DEFAULT 0,
  spent_usd REAL NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'USD',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
  reservation_id TEXT PRIMARY KEY,
  budget_id TEXT NOT NULL,
  amount_usd REAL NOT NULL,
  state TEXT NOT NULL,             -- held|settled|released
  request_key TEXT NOT NULL UNIQUE,
  actual_usd REAL,
  usage TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_calls (
  call_id TEXT PRIMARY KEY,
  case_id TEXT,
  job_id TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  task TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL,
  cost_known INTEGER NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL,
  latency_ms INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
  connection_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  label TEXT NOT NULL,
  endpoint TEXT NOT NULL DEFAULT '',
  auth_mode TEXT NOT NULL,         -- api_key|subscription_handoff|local|none
  secret_ref TEXT,
  models TEXT NOT NULL DEFAULT '[]',
  capabilities TEXT NOT NULL DEFAULT '{}',
  limits TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'unprobed',
  last_probe TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_routes (
  task TEXT PRIMARY KEY,           -- interpretation|repair|visual_review|verification_assist|knowledge
  primary_connection TEXT,
  primary_model TEXT,
  fallbacks TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);
