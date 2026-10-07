import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useStaleness } from '../../components/ConnectionBanner';
import { Empty } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { useToast } from '../../components/Toasts';
import { filterLog, LEVEL_ICON, logToText, mergeLog, scrub, stageLabel, stamp, type LogFilter } from '../../lib/log';
import { useApi, useCaseState, useResource } from '../../lib/store';
import type { LogEntry } from '../../lib/types';

/** Initial rows from GET /cases/{id}/log, merged by seq with the rows derived from live events (deduped). */
export function useLiveLog(caseId: string, limit = 500) {
  const api = useApi();
  const cs = useCaseState(caseId);
  const initial = useResource(() => api.caseLog(caseId, { limit }), [api, caseId, limit]);
  const live = cs?.liveLog;
  const entries = useMemo(() => mergeLog(initial.data?.entries, live), [initial.data, live]);
  return { entries, initial, cs };
}

/** Why the feed may be behind: reuses the connection banner's staleness logic. null while live. */
export function useFeedState(): { tone: 'warn' | 'bad'; text: string } | null {
  const { stale, reason, snap } = useStaleness();
  const status = snap?.status ?? 'connecting';
  if (status === 'disconnected') return { tone: 'bad', text: 'Disconnected from the controller. New lines will not appear until it reconnects; lines already shown may be out of date.' };
  if (status !== 'connected') return { tone: 'warn', text: status === 'reconnecting' ? 'Reconnecting… missed lines are filled in automatically.' : 'Connecting to the controller…' };
  if (stale) return { tone: 'warn', text: reason === 'never_received' ? 'Connected, but nothing has been received yet. The feed may be stale.' : 'Connected, but no events have arrived for a while. The feed may be stale.' };
  return null;
}

const FILTERS: { id: 'all' | 'problems' | 'ai'; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'problems', label: 'Problems' },
  { id: 'ai', label: 'AI' },
];

export function LiveLogTab({ caseId }: { caseId: string }) {
  const toast = useToast();
  const { entries, initial, cs } = useLiveLog(caseId);
  const feedState = useFeedState();
  const [kind, setKind] = useState<'all' | 'problems' | 'ai' | 'stage'>('all');
  const [stage, setStage] = useState('');
  const [paused, setPaused] = useState(false);
  const [seenCount, setSeenCount] = useState(0);
  const feedRef = useRef<HTMLDivElement>(null);
  const programmatic = useRef(false);

  const filter: LogFilter = kind === 'stage' && stage ? { kind: 'stage', stage } : { kind: kind === 'stage' ? 'all' : kind };
  const shown = useMemo(() => filterLog(entries, filter), [entries, kind, stage]); // eslint-disable-line react-hooks/exhaustive-deps
  const stages = useMemo(() => [...new Set(entries.map((e) => e.stage).filter((s): s is string => !!s))], [entries]);
  const problems = useMemo(() => entries.filter((e) => e.level !== 'info').length, [entries]);
  const aiCount = useMemo(() => entries.filter((e) => e.kind === 'ai').length, [entries]);

  const toBottom = useCallback(() => {
    const el = feedRef.current;
    if (!el) return;
    programmatic.current = true;
    el.scrollTop = el.scrollHeight;
    requestAnimationFrame(() => {
      programmatic.current = false;
    });
  }, []);

  useLayoutEffect(() => {
    if (!paused) {
      toBottom();
      setSeenCount(shown.length);
    }
  }, [shown.length, paused, toBottom]);

  const onScroll = () => {
    const el = feedRef.current;
    if (!el || programmatic.current) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (!atBottom && !paused) setPaused(true);
    else if (atBottom && paused) {
      setPaused(false);
    }
  };
  const jump = () => {
    setPaused(false);
    setTimeout(toBottom, 0);
  };
  const newCount = paused ? Math.max(0, shown.length - seenCount) : 0;

  const text = () => logToText(shown);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text());
      toast.success('Log copied', `${shown.length} line${shown.length === 1 ? '' : 's'} copied as plain text.`);
    } catch (e) {
      toast.error('Could not copy the log', e);
    }
  };
  const save = () => {
    const blob = new Blob([text()], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `rebuild-log-${caseId}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast.success('Log saved', 'The file is in your Downloads folder.');
  };

  useEffect(() => {
    if (kind === 'stage' && !stages.includes(stage)) setStage(stages[0] ?? '');
  }, [kind, stage, stages]);

  const status = cs?.case?.value.status;
  return (
    <section className="stack-lg" aria-labelledby="live-log-h" data-testid="live-log">
      <div>
        <h2 id="live-log-h">Live log</h2>
        <p className="small muted">What Rebuild Studio is doing right now, in plain language. Secrets are removed and prompts are never shown.</p>
      </div>
      {feedState && (
        <div className={`banner ${feedState.tone}`} role={feedState.tone === 'bad' ? 'alert' : 'status'} data-testid="live-log-stale" style={{ borderRadius: 'var(--r-2)', border: '1px solid var(--border)' }}>
          <span className={`dot ${feedState.tone}`} aria-hidden="true" />
          <strong>{feedState.tone === 'bad' ? 'Not live' : 'Live updates may be behind'}</strong>
          <span>{feedState.text}</span>
        </div>
      )}
      <div className="card live-log">
        <div className="live-log-bar" role="toolbar" aria-label="Log controls">
          <div className="row" role="group" aria-label="Show">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" className={`btn sm${kind === f.id ? ' primary' : ''}`} aria-pressed={kind === f.id} data-testid={`log-filter-${f.id}`} onClick={() => setKind(f.id)}>
                {f.label}
                {f.id === 'problems' && problems > 0 ? ` (${problems})` : f.id === 'ai' && aiCount > 0 ? ` (${aiCount})` : ''}
              </button>
            ))}
            <label className="row small" style={{ gap: 4 }}>
              <span className="muted">Step</span>
              <select
                className="input sm"
                aria-label="Filter by step"
                data-testid="log-filter-stage"
                value={kind === 'stage' ? stage : ''}
                onChange={(e) => {
                  if (e.target.value) {
                    setStage(e.target.value);
                    setKind('stage');
                  } else setKind('all');
                }}
              >
                <option value="">All steps</option>
                {stages.map((s) => (
                  <option key={s} value={s}>
                    {stageLabel(s)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <span className="spacer" />
          <div className="row">
            <button type="button" className="btn sm" aria-pressed={paused} data-testid="log-pause" onClick={() => (paused ? jump() : setPaused(true))}>
              {paused ? '▶ Resume auto-scroll' : '❚❚ Pause auto-scroll'}
            </button>
            <button type="button" className="btn sm" data-testid="log-jump" onClick={jump} disabled={!paused}>
              ↓ Jump to latest{newCount > 0 ? ` (${newCount} new)` : ''}
            </button>
            <button type="button" className="btn sm" data-testid="log-copy" onClick={copy} disabled={shown.length === 0}>
              Copy
            </button>
            <button type="button" className="btn sm" data-testid="log-save" onClick={save} disabled={shown.length === 0}>
              Save log as text
            </button>
          </div>
        </div>
        <span className="sr-only" role="status" data-testid="log-count">
          {shown.length} {shown.length === 1 ? 'line' : 'lines'} shown{paused ? ', auto-scroll paused' : ''}
        </span>
        {initial.error && entries.length === 0 ? (
          <ErrorCallout error={initial.error} title="The log could not be loaded" onRetry={initial.reload} tone="warn" />
        ) : (
          <div ref={feedRef} className="live-log-feed" role="log" aria-label="Rebuild log" aria-live="off" tabIndex={0} onScroll={onScroll} data-testid="log-feed">
            {shown.length === 0 ? (
              <Empty title={initial.loading ? 'Loading the log…' : entries.length ? 'No lines match this filter' : 'Nothing has happened yet'}>
                {entries.length
                  ? 'Choose “All” to see every line.'
                  : status === 'running'
                    ? 'The first lines appear as soon as a step starts.'
                    : 'Start the rebuild and each step shows up here as it happens.'}
              </Empty>
            ) : (
              <ol className="live-log-list">
                {shown.map((e) => (
                  <LogRow key={e.seq} e={e} />
                ))}
              </ol>
            )}
          </div>
        )}
        <div className="small muted row">
          <span>
            {shown.length} of {entries.length} lines
          </span>
          <span className="spacer" />
          <Link to={`/projects/${encodeURIComponent(caseId)}/advanced`}>Raw events (Advanced)</Link>
        </div>
      </div>
    </section>
  );
}

export function LogRow({ e }: { e: LogEntry }) {
  const icon = LEVEL_ICON[e.level];
  const detail = e.detail ? scrub(e.detail) : null;
  return (
    <li className={`live-log-row ${e.level}${e.kind === 'ai' ? ' ai' : ''}`} data-testid="log-row" data-seq={e.seq} data-level={e.level} data-kind={e.kind}>
      <time className="mono small muted live-log-time" dateTime={e.at} title={e.at}>
        {stamp(e.at)}
      </time>
      <span className={`live-log-icon ${e.level}`} aria-hidden="true">
        {icon.glyph}
      </span>
      <span className="sr-only">{icon.label}: </span>
      <span className="live-log-chips">
        {e.kind === 'ai' ? (
          <span className="chip info" title={[e.provider, e.model].filter(Boolean).join(' · ') || 'AI activity'}>
            AI
          </span>
        ) : (
          <span className="chip" title={e.stage ?? undefined}>
            {stageLabel(e.stage)}
          </span>
        )}
        {e.milestone && <span className="chip outline mono">{e.milestone.replace(/^M-/, '')}</span>}
      </span>
      <span className="live-log-text">
        {scrub(e.text)}
        {detail && (
          <details className="live-log-detail">
            <summary>Details</summary>
            <pre className="mono small">{detail}</pre>
          </details>
        )}
      </span>
    </li>
  );
}

/** Overview strip: the latest three lines, so "what is it doing right now" never needs a tab change. */
export function NowDoingStrip({ caseId }: { caseId: string }) {
  const { entries } = useLiveLog(caseId, 20);
  const last = entries.slice(-3);
  const feedState = useFeedState();
  return (
    <section className="card" aria-labelledby="now-doing-h" data-testid="now-doing">
      <div className="card-head">
        <h3 id="now-doing-h">Now doing</h3>
        <Link to={`/projects/${encodeURIComponent(caseId)}/live-log`} data-testid="now-doing-link">
          Open the live log
        </Link>
      </div>
      {feedState && (
        <p className="small muted" role="status">
          {feedState.tone === 'bad' ? 'Not live: ' : 'May be behind: '}
          {feedState.text}
        </p>
      )}
      {last.length === 0 ? (
        <p className="small muted">No activity recorded yet.</p>
      ) : (
        <ol className="live-log-list compact" aria-live="polite">
          {last.map((e) => (
            <LogRow key={e.seq} e={e} />
          ))}
        </ol>
      )}
    </section>
  );
}
