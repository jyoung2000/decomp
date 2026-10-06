import { useEffect, useState } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { ConfirmDialog } from '../components/Dialog';
import { Empty } from '../components/Empty';
import { ErrorCallout } from '../components/ErrorCallout';
import { CaseStatusChip, useOutcome } from '../components/Outcome';
import { RouteTabs } from '../components/Tabs';
import { useToast } from '../components/Toasts';
import { ApiError } from '../lib/api';
import { values } from '../lib/derive';
import { setSelectedCase } from '../lib/selection';
import { useApi, useCaseState, useResource, useStore } from '../lib/store';
import { OverviewTab } from './workspace/Overview';
import { PlanTab } from './workspace/Plan';
import { PreviewTab } from './workspace/Preview';
import { FeedbackTab } from './workspace/Feedback';
import { ComparisonsTab } from './workspace/Comparisons';
import { AdvancedTab } from './workspace/Advanced';

const TARGET: Record<string, string> = { rust: 'Rust', rust_bevy: 'Rust + Bevy', web: 'HTML/CSS/JS', auto: 'Auto' };

export function WorkspaceView() {
  const { caseId = '' } = useParams();
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const cs = useCaseState(caseId);
  const outcome = useOutcome(caseId);
  const fetched = useResource(() => api.getCase(caseId), [api, caseId]);
  const [confirmStop, setConfirmStop] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (caseId) setSelectedCase(caseId);
  }, [caseId]);

  const notFound = fetched.error instanceof ApiError && fetched.error.status === 404;
  useEffect(() => {
    if (notFound) setSelectedCase(null);
  }, [notFound]);

  const c = cs?.case?.value ?? fetched.data;
  if (notFound) {
    return (
      <div className="page">
        <Empty title="Project not found" action={<a className="btn primary" href="#/projects">Go to projects</a>}>
          The project “{caseId}” does not exist on this controller. It may have been deleted, or the controller is using a different data folder.
        </Empty>
      </div>
    );
  }
  if (!c) {
    return (
      <div className="page">
        {fetched.error ? <ErrorCallout error={fetched.error} title="Project could not be loaded" onRetry={fetched.reload} /> : <Empty title="Loading project…" />}
      </div>
    );
  }

  const status = c.status;
  const running = status === 'running' || status === 'queued';
  const paused = status === 'paused';
  const finished = ['completed', 'failed', 'cancelled'].includes(status);
  const act = async (name: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(name);
    try {
      await fn();
      toast.success(done);
      void store.refresh(caseId, 'case');
      void store.refresh(caseId, 'jobs');
    } catch (e) {
      toast.error(`${name} failed`, e);
    } finally {
      setBusy(null);
    }
  };

  const openFeedback = cs ? values(cs.feedback).filter((f) => f.status !== 'resolved').length : 0;
  const previews = cs ? Object.keys(cs.previews).length : 0;
  const base = `/projects/${encodeURIComponent(caseId)}`;

  return (
    <div>
      <header className="ws-head" aria-label="Project header">
        <div className="ws-title">
          <div className="stack" style={{ gap: 4 }}>
            <div className="row">
              <h1 data-testid="project-title">{c.name}</h1>
              <CaseStatusChip status={status} outcome={outcome} />
            </div>
            <div className="small muted row">
              <span className="mono">{c.case_id}</span>
              <span aria-hidden="true">·</span>
              <span>
                {TARGET[c.target_language] ?? c.target_language} → {c.output_type}
              </span>
              <span aria-hidden="true">·</span>
              <span>AI: {c.ai_policy?.mode === 'no_ai' ? 'off' : `${c.ai_policy?.mode?.replace(/_/g, ' ')}${c.ai_policy?.budget_usd ? ` ($${c.ai_policy.budget_usd}/job)` : ''}`}</span>
            </div>
          </div>
          <div className="btn-group" role="group" aria-label="Run controls">
            <button type="button" className="btn primary" disabled={running || paused || !!busy} data-tooltip={running ? 'Already running' : paused ? 'Paused — use Resume' : 'Start discovery and rebuild'} onClick={() => act('Start', () => api.startCase(caseId), 'Rebuild started')} data-testid="btn-start">
              ▶ Start
            </button>
            <button type="button" className="btn" disabled={!running || !!busy} data-tooltip={running ? 'Pause after current steps finish' : 'Only a running project can be paused'} onClick={() => act('Pause', () => api.pauseCase(caseId), 'Pause requested')} data-testid="btn-pause">
              ❚❚ Pause
            </button>
            <button type="button" className="btn" disabled={!(paused || status === 'failed' || status === 'cancelled') || !!busy} data-tooltip={paused || finished ? 'Continue from the last completed step' : 'Nothing to resume'} onClick={() => act('Resume', () => api.resumeCase(caseId), 'Resumed')} data-testid="btn-resume">
              ↻ Resume
            </button>
            <button type="button" className="btn danger" disabled={!(running || paused) || !!busy} data-tooltip={running || paused ? 'Cancel all queued and running jobs' : 'Nothing is running'} data-tooltip-pos="left" onClick={() => setConfirmStop(true)} data-testid="btn-stop">
              ■ Stop
            </button>
          </div>
        </div>
        <RouteTabs
          label="Project sections"
          tabs={[
            { to: `${base}/overview`, label: 'Overview', testId: 'tab-overview' },
            { to: `${base}/plan`, label: 'Plan', testId: 'tab-plan' },
            { to: `${base}/preview`, label: 'Preview & Test', badge: previews || null, testId: 'tab-preview' },
            { to: `${base}/feedback`, label: 'Feedback', badge: openFeedback || null, testId: 'tab-feedback' },
            { to: `${base}/comparisons`, label: 'Comparisons', testId: 'tab-comparisons' },
            { to: `${base}/advanced`, label: 'Advanced', testId: 'tab-advanced' },
          ]}
        />
      </header>
      <div className="page">
        <Routes>
          <Route index element={<Navigate to={`${base}/overview`} replace />} />
          <Route path="overview" element={<OverviewTab caseId={caseId} />} />
          <Route path="plan" element={<PlanTab caseId={caseId} />} />
          <Route path="preview" element={<PreviewTab caseId={caseId} />} />
          <Route path="feedback" element={<FeedbackTab caseId={caseId} />} />
          <Route path="comparisons" element={<ComparisonsTab caseId={caseId} />} />
          <Route path="advanced" element={<AdvancedTab caseId={caseId} />} />
          <Route path="*" element={<Navigate to={`${base}/overview`} replace />} />
        </Routes>
      </div>
      <ConfirmDialog
        open={confirmStop}
        title="Stop this rebuild?"
        body={<p>All queued and running jobs are cancelled. Completed work, evidence and builds are kept, and you can resume later from the last completed step.</p>}
        confirmLabel="Stop rebuild"
        danger
        onConfirm={() => act('Stop', () => api.cancelCase(caseId), 'Stop requested')}
        onClose={() => setConfirmStop(false)}
      />
    </div>
  );
}
