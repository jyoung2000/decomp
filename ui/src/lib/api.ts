import type { StudioConfig } from './config';
import type {
  AiCall, ApiErrorBody, Budget, Candidate, Capabilities, Case, Comparison, Connection, ControllerEvent, DoctorReport,
  Evidence, Feature, Feedback, Health, HermesStatus, Job, KnowledgeEntry, Module, NewCaseBody, NewFeedbackBody, Plan,
  PlanRevision, Preview, PreviewOpenResult, TaskRoute,
} from './types';

/** Error with the controller's three-part explanation: what happened, what is affected, what to do next. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly affected?: string;
  readonly nextAction?: string;
  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.status = status;
    this.code = body.code;
    this.affected = body.affected;
    this.nextAction = body.next_action;
  }
}

export function describeError(e: unknown): { what: string; affected?: string; next?: string; code?: string } {
  if (e instanceof ApiError) return { what: e.message, affected: e.affected, next: e.nextAction, code: e.code };
  if (e instanceof TypeError) {
    return {
      what: 'The controller could not be reached.',
      affected: 'Live updates and every action that talks to the controller.',
      next: 'Check that Rebuild Studio’s controller is running, then retry. The banner at the top shows reconnect status.',
      code: 'network',
    };
  }
  return { what: e instanceof Error ? e.message : String(e) };
}

type Fetch = typeof fetch;

export class Api {
  constructor(private cfg: StudioConfig, private fetchImpl: Fetch = (...a) => fetch(...a)) {}

  get config(): StudioConfig {
    return this.cfg;
  }

  async request<T>(method: string, path: string, body?: unknown, init?: { signal?: AbortSignal }): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (this.cfg.token) headers.Authorization = `Bearer ${this.cfg.token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await this.fetchImpl(this.cfg.baseUrl + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: init?.signal,
    });
    const text = await res.text();
    let data: unknown = undefined;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    if (!res.ok) {
      const err = (data && typeof data === 'object' && 'error' in (data as object) ? (data as { error: ApiErrorBody }).error : null) ?? {
        code: `http_${res.status}`,
        message: `The controller answered ${res.status} ${res.statusText || ''} for ${method} ${path}.`.replace(/\s+/g, ' '),
        next_action: res.status === 401 || res.status === 403 ? 'Restart Rebuild Studio so the controller token is refreshed.' : 'Retry; if it keeps failing, open Advanced → Raw logs.',
      };
      throw new ApiError(res.status, err);
    }
    return data as T;
  }

  /** Fetches binary content with auth (for screenshots / attachments) and returns an object URL. */
  async blobUrl(path: string): Promise<string> {
    const url = /^https?:/.test(path) ? path : this.cfg.baseUrl + path;
    const res = await this.fetchImpl(url, { headers: this.cfg.token ? { Authorization: `Bearer ${this.cfg.token}` } : {} });
    if (!res.ok) throw new ApiError(res.status, { code: `http_${res.status}`, message: `Could not load ${path}` });
    return URL.createObjectURL(await res.blob());
  }

  get = <T>(p: string) => this.request<T>('GET', p);
  post = <T>(p: string, b: unknown = {}) => this.request<T>('POST', p, b);
  put = <T>(p: string, b: unknown) => this.request<T>('PUT', p, b);
  del = <T>(p: string) => this.request<T>('DELETE', p);

  health = () => this.get<Health>('/health');
  events = (since: number, caseId?: string) =>
    this.get<ControllerEvent[]>(`/events?since=${since}${caseId ? `&case_id=${encodeURIComponent(caseId)}` : ''}`);
  doctor = (smoke = false) => this.get<DoctorReport>(`/doctor?smoke=${smoke ? 1 : 0}`);
  capabilities = () => this.get<Capabilities>('/capabilities');

  cases = () => this.get<Case[]>('/cases');
  createCase = (b: NewCaseBody) => this.post<Case>('/cases', b);
  getCase = (id: string) => this.get<Case>(`/cases/${enc(id)}`);
  startCase = (id: string) => this.post<{ job_ids: string[] }>(`/cases/${enc(id)}/start`);
  pauseCase = (id: string) => this.post<unknown>(`/cases/${enc(id)}/pause`);
  resumeCase = (id: string) => this.post<unknown>(`/cases/${enc(id)}/resume`);
  cancelCase = (id: string) => this.post<unknown>(`/cases/${enc(id)}/cancel`);
  jobs = (id: string) => this.get<Job[]>(`/cases/${enc(id)}/jobs`);
  cancelJob = (id: string) => this.post<unknown>(`/jobs/${enc(id)}/cancel`);
  resumeJob = (id: string) => this.post<unknown>(`/jobs/${enc(id)}/resume`);
  modules = (id: string) => this.get<Module[]>(`/cases/${enc(id)}/modules`);
  evidenceList = (id: string, kind?: string, moduleId?: string) =>
    this.get<Evidence[]>(`/cases/${enc(id)}/evidence${qs({ kind, module_id: moduleId })}`);
  evidence = (id: string, maxBytes = 65536) => this.get<Evidence>(`/evidence/${enc(id)}?max_bytes=${maxBytes}`);
  searchEvidence = (id: string, q: string) => this.get<Evidence[]>(`/cases/${enc(id)}/evidence/search?q=${encodeURIComponent(q)}`);
  features = (id: string) => this.get<Feature[]>(`/cases/${enc(id)}/features`);
  plan = (id: string) => this.get<Plan>(`/cases/${enc(id)}/plan`);
  planRevisions = (id: string) => this.get<PlanRevision[]>(`/cases/${enc(id)}/plan/revisions`);
  prioritize = (id: string, itemId: string) => this.post<unknown>(`/cases/${enc(id)}/plan/prioritize`, { item_id: itemId });
  requestChange = (id: string, itemId: string, request: string, reason: string) =>
    this.post<{ affected_items?: string[]; affected_tests?: string[]; [k: string]: unknown }>(`/cases/${enc(id)}/plan/change`, {
      item_id: itemId,
      request,
      reason,
    });
  exportPlan = (id: string) => this.get<{ json?: string; html?: string; paths?: string[]; [k: string]: unknown }>(`/cases/${enc(id)}/plan/export`);
  candidates = (id: string) => this.get<Candidate[]>(`/cases/${enc(id)}/candidates`);
  candidate = (id: string) => this.get<Candidate>(`/candidates/${enc(id)}`);
  comparisons = (id: string, candidateId?: string) => this.get<Comparison[]>(`/cases/${enc(id)}/comparisons${qs({ candidate_id: candidateId })}`);
  previews = (id: string) => this.get<Preview[]>(`/cases/${enc(id)}/previews`);
  openPreview = (id: string) => this.post<PreviewOpenResult>(`/previews/${enc(id)}/open`);
  stopPreview = (id: string, instanceId?: string) => this.post<unknown>(`/previews/${enc(id)}/stop`, instanceId ? { instance_id: instanceId } : {});
  feedback = (id: string) => this.get<Feedback[]>(`/cases/${enc(id)}/feedback`);
  createFeedback = (id: string, b: NewFeedbackBody) => this.post<Feedback>(`/cases/${enc(id)}/feedback`, b);
  triageFeedback = (id: string, status: string, note: string, createWork: boolean) =>
    this.post<Feedback>(`/feedback/${enc(id)}/triage`, { status, note, create_work: createWork });
  reopenFeedback = (id: string) => this.post<Feedback>(`/feedback/${enc(id)}/reopen`);
  connections = () => this.get<Connection[]>('/connections');
  addConnection = (b: Record<string, unknown>) => this.post<Connection>('/connections', b);
  probeConnection = (id: string) => this.post<Connection & { probe?: Record<string, unknown> }>(`/connections/${enc(id)}/probe`);
  deleteConnection = (id: string) => this.del<unknown>(`/connections/${enc(id)}`);
  routes = () => this.get<TaskRoute[]>('/routes');
  putRoute = (task: string, b: Omit<TaskRoute, 'task' | 'updated_at'>) => this.put<TaskRoute>(`/routes/${enc(task)}`, b);
  budgets = () => this.get<Budget[]>('/budgets');
  aiCalls = (caseId?: string) => this.get<AiCall[]>(`/ai/calls${qs({ case_id: caseId })}`);
  knowledge = () => this.get<KnowledgeEntry[]>('/knowledge');
  knowledgeItem = (id: string) => this.get<KnowledgeEntry>(`/knowledge/${enc(id)}`);
  validateKnowledge = (id: string) => this.post<KnowledgeEntry>(`/knowledge/${enc(id)}/validate`);
  rollbackKnowledge = (id: string) => this.post<KnowledgeEntry>(`/knowledge/${enc(id)}/rollback`);
  settings = () => this.get<Record<string, unknown>>('/settings');
  putSettings = (b: Record<string, unknown>) => this.put<Record<string, unknown>>('/settings', b);
  hermesStatus = () => this.get<HermesStatus>('/hermes/status');
  hermesPair = (profilePath?: string) => this.post<HermesStatus>('/hermes/pair', profilePath ? { profile_path: profilePath } : {});
  hermesRegister = (dryRun: boolean) => this.post<Record<string, unknown>>('/hermes/register_mcp', { dry_run: dryRun });
}

function enc(s: string) {
  return encodeURIComponent(s);
}

function qs(o: Record<string, string | undefined>): string {
  const p = Object.entries(o).filter(([, v]) => v !== undefined && v !== '');
  return p.length ? '?' + p.map(([k, v]) => `${k}=${encodeURIComponent(v as string)}`).join('&') : '';
}
