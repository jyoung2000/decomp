import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Dialog } from '../../components/Dialog';
import { Empty } from '../../components/Empty';
import { ErrorCallout, Explain } from '../../components/ErrorCallout';
import { StatusChip } from '../../components/StatusChip';
import { useToast } from '../../components/Toasts';
import { values } from '../../lib/derive';
import { bytes, dateTime, fileToBase64, humanize, itemLabel, shortHash } from '../../lib/format';
import { useApi, useCaseState, useResource, useStore } from '../../lib/store';
import type { Candidate, Feedback, FeedbackClass } from '../../lib/types';

const CLASSES: { id: FeedbackClass; label: string; sub: string }[] = [
  { id: 'bug', label: 'Bug', sub: 'Something behaves wrongly' },
  { id: 'change', label: 'Change', sub: 'Request a different result' },
  { id: 'question', label: 'Question', sub: 'Ask about the plan or build' },
  { id: 'acceptance', label: 'Acceptance', sub: 'Confirm it works' },
];
// the controller validates priority against low|medium|high|critical
const PRIORITIES = ['low', 'medium', 'high', 'critical'];
const MAX_FILE = 10 * 1024 * 1024;

export function FeedbackTab({ caseId }: { caseId: string }) {
  const cs = useCaseState(caseId);
  const [params] = useSearchParams();
  const focus = params.get('focus');
  const list = useMemo(() => (cs ? values(cs.feedback).sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? '')) : []), [cs]);
  if (!cs) return <Empty title="Loading feedback…" />;
  return (
    <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', alignItems: 'start' }} data-testid="feedback">
      <FeedbackForm caseId={caseId} />
      <section className="card" aria-labelledby="fb-list-h">
        <div className="card-head">
          <h3 id="fb-list-h">Feedback ({list.length})</h3>
        </div>
        {list.length === 0 ? (
          <Empty title="No feedback yet">Use the form to report a bug, request a change, ask a question or accept a feature. Each entry keeps the build and plan revision it was about.</Empty>
        ) : (
          <ul className="list" data-testid="feedback-list">
            {list.map((f) => (
              <FeedbackRow key={f.feedback_id} caseId={caseId} f={f} initiallyOpen={focus === f.feedback_id} candidates={values(cs.candidates)} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function FeedbackForm({ caseId }: { caseId: string }) {
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const cs = useCaseState(caseId)!;
  const [params] = useSearchParams();
  const candidates = useMemo(() => values(cs.candidates).sort((a, b) => b.revision - a.revision), [cs.candidates]);
  const [targetKind, setTargetKind] = useState(params.get('target_kind') ?? 'preview');
  const [targetId, setTargetId] = useState(params.get('target_id') ?? '');
  const [candidateId, setCandidateId] = useState(params.get('candidate_id') ?? '');
  const [cls, setCls] = useState<FeedbackClass>((params.get('classification') as FeedbackClass) ?? 'bug');
  const [priority, setPriority] = useState('medium');
  const [comment, setComment] = useState('');
  const [expected, setExpected] = useState('');
  const [actual, setActual] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const k = params.get('target_kind');
    const t = params.get('target_id');
    const c = params.get('candidate_id');
    const cl = params.get('classification');
    if (k) setTargetKind(k);
    if (t) setTargetId(t);
    if (c) setCandidateId(c);
    if (cl) setCls(cl as FeedbackClass);
  }, [params]);

  const targets = useMemo(() => {
    switch (targetKind) {
      case 'preview':
        return values(cs.previews).map((p) => ({ id: p.preview_id, label: `${p.title} (${p.kind})` }));
      case 'feature':
        return values(cs.features).map((f) => ({ id: f.feature_id, label: f.title }));
      case 'milestone':
      case 'deliverable':
        return values(cs.planItems)
          .filter((i) => i.kind === targetKind)
          .map((i) => ({ id: i.item_id, label: `${itemLabel(i.item_id)} ${i.title}` }));
      case 'comparison':
        return [];
      default:
        return [];
    }
  }, [targetKind, cs]);

  const effectiveCandidate: Candidate | undefined = candidates.find((c) => c.candidate_id === candidateId) ?? (candidateId ? undefined : candidates[0]);
  const preview = targetKind === 'preview' ? cs.previews[targetId]?.value : undefined;
  const buildHash = preview?.build_hash ?? effectiveCandidate?.build_hash ?? null;
  const planRev = preview?.plan_revision ?? effectiveCandidate?.plan_revision ?? cs.planRevision;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!targetId.trim()) errs.target = 'Choose what this feedback is about. Feedback without a target cannot be routed to the right plan item.';
    if (!comment.trim()) errs.comment = 'Describe what you saw or want. The comment is what the team reads first.';
    for (const f of files) if (f.size > MAX_FILE) errs.files = `“${f.name}” is ${bytes(f.size)}; attachments are limited to ${bytes(MAX_FILE)} each. Remove it or attach a smaller file.`;
    setErrors(errs);
    setServerError(null);
    if (Object.keys(errs).length) return;
    setBusy(true);
    try {
      const attachments = await Promise.all(files.map(async (f) => ({ name: f.name, bytes_b64: await fileToBase64(f) })));
      const fb = await api.createFeedback(caseId, {
        target_kind: targetKind,
        target_id: targetId.trim(),
        candidate_id: effectiveCandidate?.candidate_id ?? preview?.candidate_id,
        classification: cls,
        priority,
        comment: comment.trim(),
        ...(expected.trim() ? { expected: expected.trim() } : {}),
        ...(actual.trim() ? { actual: actual.trim() } : {}),
        ...(attachments.length ? { attachments } : {}),
      });
      toast.success('Feedback saved', `Status: ${humanize(fb.status ?? 'received')}`);
      setComment('');
      setExpected('');
      setActual('');
      setFiles([]);
      void store.refresh(caseId, 'feedback');
    } catch (err) {
      setServerError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card" aria-labelledby="fb-form-h">
      <h3 id="fb-form-h">Give feedback</h3>
      <form className="form" onSubmit={submit} noValidate aria-label="Give feedback">
        {serverError ? <ErrorCallout error={serverError} title="Feedback was not saved" /> : null}
        <div className="field-row">
          <div className="field">
            <label htmlFor="fb-kind">About</label>
            <select
              id="fb-kind"
              value={targetKind}
              onChange={(e) => {
                setTargetKind(e.target.value);
                setTargetId('');
              }}
            >
              <option value="preview">Preview</option>
              <option value="feature">Feature</option>
              <option value="milestone">Milestone</option>
              <option value="deliverable">Deliverable</option>
              <option value="comparison">Comparison</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="fb-target">Target</label>
            {targets.length ? (
              <select id="fb-target" value={targetId} onChange={(e) => setTargetId(e.target.value)} aria-invalid={!!errors.target}>
                <option value="">Choose…</option>
                {targetId && !targets.some((t) => t.id === targetId) && <option value={targetId}>{targetId}</option>}
                {targets.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.label}
                  </option>
                ))}
              </select>
            ) : (
              <input id="fb-target" type="text" value={targetId} onChange={(e) => setTargetId(e.target.value)} placeholder="ID" aria-invalid={!!errors.target} />
            )}
          </div>
        </div>
        {errors.target && <span className="field-error">{errors.target}</span>}

        <div className="stack" role="radiogroup" aria-label="Kind of feedback">
          <span className="field-label">Kind</span>
          <div className="choice-grid">
            {CLASSES.map((c) => (
              <label key={c.id} className="choice">
                <input type="radio" name="fb-class" value={c.id} checked={cls === c.id} onChange={() => setCls(c.id)} />
                <span>
                  <span className="choice-title">{c.label}</span>
                  <span className="choice-sub">{c.sub}</span>
                </span>
              </label>
            ))}
          </div>
        </div>

        <div className="field">
          <label htmlFor="fb-comment">Comment *</label>
          <textarea id="fb-comment" value={comment} onChange={(e) => setComment(e.target.value)} aria-invalid={!!errors.comment} aria-describedby={errors.comment ? 'fb-comment-err' : undefined} />
          {errors.comment && (
            <span className="field-error" id="fb-comment-err">
              {errors.comment}
            </span>
          )}
        </div>
        {(cls === 'bug' || cls === 'change') && (
          <div className="field-row">
            <div className="field">
              <label htmlFor="fb-expected">Expected</label>
              <textarea id="fb-expected" value={expected} onChange={(e) => setExpected(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="fb-actual">Actual</label>
              <textarea id="fb-actual" value={actual} onChange={(e) => setActual(e.target.value)} />
            </div>
          </div>
        )}
        <div className="field-row">
          <div className="field">
            <label htmlFor="fb-priority">Priority</label>
            <select id="fb-priority" value={priority} onChange={(e) => setPriority(e.target.value)}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {humanize(p)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="fb-candidate">Build version</label>
            <select id="fb-candidate" value={candidateId} onChange={(e) => setCandidateId(e.target.value)}>
              <option value="">{candidates[0] ? `Latest (version ${candidates[0].revision})` : 'No builds yet'}</option>
              {candidates.map((c) => (
                <option key={c.candidate_id} value={c.candidate_id}>
                  Version {c.revision} · {shortHash(c.build_hash)}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="fb-files">Attachments</label>
          <input id="fb-files" type="file" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []))} aria-describedby="fb-files-hint" />
          <span className="hint" id="fb-files-hint">
            Screenshots or logs, up to {bytes(MAX_FILE)} each.{files.length > 0 && ` Selected: ${files.map((f) => `${f.name} (${bytes(f.size)})`).join(', ')}`}
          </span>
          {errors.files && <span className="field-error">{errors.files}</span>}
        </div>
        <fieldset>
          <legend className="small">Captured automatically</legend>
          <div className="field-row">
            <div className="field">
              <label htmlFor="fb-auto-cand">Candidate</label>
              <input id="fb-auto-cand" type="text" readOnly value={effectiveCandidate?.candidate_id ?? preview?.candidate_id ?? 'none'} />
            </div>
            <div className="field">
              <label htmlFor="fb-auto-hash">Build hash</label>
              <input id="fb-auto-hash" type="text" readOnly value={buildHash ?? 'not built'} data-testid="fb-build-hash" />
            </div>
            <div className="field">
              <label htmlFor="fb-auto-plan">Plan revision</label>
              <input id="fb-auto-plan" type="text" readOnly value={planRev != null ? `r${planRev}` : 'unknown'} data-testid="fb-plan-rev" />
            </div>
          </div>
        </fieldset>
        <div className="row">
          <button type="submit" className="btn primary" disabled={busy} data-testid="submit-feedback">
            {busy ? 'Saving…' : 'Submit feedback'}
          </button>
        </div>
      </form>
    </section>
  );
}

function FeedbackRow({ caseId, f, initiallyOpen, candidates }: { caseId: string; f: Feedback; initiallyOpen: boolean; candidates: Candidate[] }) {
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const [open, setOpen] = useState(initiallyOpen);
  const [compare, setCompare] = useState(false);
  const [triage, setTriage] = useState(false);
  const fixed = f.context?.fixed_candidate_id ?? null;
  const reopen = async () => {
    try {
      await api.reopenFeedback(f.feedback_id);
      toast.success('Feedback reopened');
      void store.refresh(caseId, 'feedback');
    } catch (e) {
      toast.error('Could not reopen', e);
    }
  };
  return (
    <li data-testid={`feedback-${f.feedback_id}`}>
      <div className="row between">
        <div className="row" style={{ minWidth: 0 }}>
          <StatusChip status={f.status} />
          <span className="chip outline">{f.classification}</span>
          <span className="small muted">{f.priority}</span>
          <span className="small muted">
            {f.target_kind} <span className="mono">{f.target_id}</span>
          </span>
        </div>
        <div className="btn-group">
          {f.status !== 'resolved' && f.status !== 'ready_to_retest' && (
            <button type="button" className="btn sm" onClick={() => setTriage(true)} data-testid="triage-feedback" data-tooltip="Set status, add a note, or turn into plan work">
              Triage…
            </button>
          )}
          {(f.status === 'resolved' || f.status === 'ready_to_retest') && (
            <button type="button" className="btn sm" onClick={reopen} data-testid="reopen-feedback">
              Reopen
            </button>
          )}
          {fixed && fixed !== f.candidate_id && (
            <button type="button" className="btn sm" aria-expanded={compare} onClick={() => setCompare((v) => !v)}>
              Compare fix
            </button>
          )}
          <button type="button" className="btn sm ghost" aria-expanded={open} aria-controls={`fb-hist-${f.feedback_id}`} onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide history' : 'History'}
          </button>
        </div>
      </div>
      <p className="wrap-any" style={{ marginTop: 4 }}>
        {f.comment}
      </p>
      <p className="xs muted">
        {dateTime(f.created_at)} · build {shortHash(f.context?.build_hash ?? candidates.find((c) => c.candidate_id === f.candidate_id)?.build_hash)} · plan r{f.plan_revision ?? '—'}
        {f.attachments?.length ? ` · ${f.attachments.length} attachment${f.attachments.length > 1 ? 's' : ''}` : ''}
        {f.linked_items?.length ? (
          <span data-testid="linked-items">
            {' '}
            · plan work <span className="mono">{f.linked_items.map(itemLabel).join(', ')}</span>
          </span>
        ) : null}
      </p>
      {(f.expected || f.actual) && (
        <dl className="kv small" style={{ marginTop: 4 }}>
          {f.expected && (
            <>
              <dt>Expected</dt>
              <dd>{f.expected}</dd>
            </>
          )}
          {f.actual && (
            <>
              <dt>Actual</dt>
              <dd>{f.actual}</dd>
            </>
          )}
        </dl>
      )}
      {open && (
        <ol className="bullets small" id={`fb-hist-${f.feedback_id}`} style={{ marginTop: 8 }}>
          {(f.history ?? []).length === 0 && <li className="muted">No history recorded.</li>}
          {(f.history ?? []).map((h, i) => (
            <li key={i}>
              <span className="muted">{dateTime(h.ts)}</span> {h.status && <StatusChip status={h.status} />} {h.note} {(h.actor ?? h.by) && <span className="muted">— {h.actor ?? h.by}</span>}
            </li>
          ))}
        </ol>
      )}
      {compare && fixed && <FixCompare caseId={caseId} f={f} reported={f.candidate_id} fixed={fixed} candidates={candidates} />}
      <TriageDialog open={triage} caseId={caseId} f={f} onClose={() => setTriage(false)} />
    </li>
  );
}

// statuses the controller accepts for triage (rebuild_controller/feedback.py STATUSES)
const TRIAGE_STATUSES: { id: string; label: string }[] = [
  { id: 'triaged', label: 'Triaged — reviewed, no work yet' },
  { id: 'queued', label: 'Queued — work is planned' },
  { id: 'in_progress', label: 'In progress' },
  { id: 'resolved', label: 'Resolved — no further action' },
];

function TriageDialog({ open, caseId, f, onClose }: { open: boolean; caseId: string; f: Feedback; onClose: () => void }) {
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const [status, setStatus] = useState('triaged');
  const [note, setNote] = useState('');
  const [createWork, setCreateWork] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.triageFeedback(f.feedback_id, createWork ? 'queued' : status, note.trim(), createWork);
      toast.success('Feedback triaged', `Status: ${humanize(r?.status ?? status)}${r?.linked_items?.length ? ` · plan work ${r.linked_items.join(', ')}` : ''}`);
      setNote('');
      setCreateWork(false);
      void store.refresh(caseId, 'feedback');
      if (createWork) void store.refresh(caseId, 'plan');
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      title="Triage feedback"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn primary" onClick={submit} disabled={busy} data-testid="triage-submit">
            {busy ? 'Saving…' : 'Save triage'}
          </button>
        </>
      }
    >
      <div className="form">
        {error ? <ErrorCallout error={error} title="Triage was not saved" /> : null}
        <p className="small muted wrap-any">“{f.comment}”</p>
        <div className="field">
          <label htmlFor={`tri-status-${f.feedback_id}`}>New status</label>
          <select id={`tri-status-${f.feedback_id}`} value={createWork ? 'queued' : status} disabled={createWork} onChange={(e) => setStatus(e.target.value)} data-autofocus>
            {TRIAGE_STATUSES.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
        <label className="check">
          <input type="checkbox" checked={createWork} onChange={(e) => setCreateWork(e.target.checked)} data-testid="triage-create-work" /> Turn into plan work
        </label>
        <span className="hint">Adds a linked plan item, revises the plan and queues the work. Feedback never changes baselines or verdicts.</span>
        <div className="field">
          <label htmlFor={`tri-note-${f.feedback_id}`}>Note</label>
          <textarea id={`tri-note-${f.feedback_id}`} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why this status? (kept in the history)" />
        </div>
      </div>
    </Dialog>
  );
}

function FixCompare({ caseId, f, reported, fixed, candidates }: { caseId: string; f: Feedback; reported: string | null; fixed: string; candidates: Candidate[] }) {
  const api = useApi();
  const a = candidates.find((c) => c.candidate_id === reported);
  const b = candidates.find((c) => c.candidate_id === fixed);
  const ra = useResource(() => (reported ? api.comparisons(caseId, reported) : Promise.resolve([])), [api, caseId, reported]);
  const rb = useResource(() => api.comparisons(caseId, fixed), [api, caseId, fixed]);
  const rel = (rows?: { feature_id: string | null; verdict: string }[]) => {
    if (!rows) return '…';
    const r = f.target_kind === 'feature' ? rows.filter((x) => x.feature_id === f.target_id) : rows;
    const n = (v: string) => r.filter((x) => x.verdict === v).length;
    return r.length ? `${n('pass')} pass · ${n('fail')} fail · ${n('error')} error` : 'no comparisons';
  };
  if (!b) return <Explain tone="warn" what="The fixed build is not in this project’s candidate list." affected="The before/after comparison." next="Wait for the candidate to finish building, then try again." />;
  return (
    <div className="table-wrap" style={{ marginTop: 8 }} data-testid="fix-compare">
      <table className="table">
        <thead>
          <tr>
            <th scope="col"></th>
            <th scope="col">Reported (v{a?.revision ?? '?'})</th>
            <th scope="col">Fixed (v{b.revision})</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Build hash</th>
            <td className="mono">{shortHash(a?.build_hash)}</td>
            <td className="mono">{shortHash(b.build_hash)}</td>
          </tr>
          <tr>
            <th scope="row">Verification</th>
            <td>{a ? <StatusChip status={a.verification} /> : '—'}</td>
            <td>
              <StatusChip status={b.verification} />
            </td>
          </tr>
          <tr>
            <th scope="row">{f.target_kind === 'feature' ? 'Comparisons for this feature' : 'Comparisons'}</th>
            <td>{rel(ra.data)}</td>
            <td>{rel(rb.data)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
