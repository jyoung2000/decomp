import type { PhaseView } from '../lib/derive';

/** A progress row. A bar is filled only when the controller supplied a denominator; otherwise counts + "unknown scope". */
export function PhaseProgressRow({ view }: { view: PhaseView }) {
  const label = `${view.label} progress`;
  let nums: string;
  if (!view.reported) nums = 'not reported yet';
  else if (view.percent != null) nums = `${view.done} / ${view.total}${view.unit ? ` ${view.unit}` : ''} · ${Math.floor(view.percent)}%`;
  else nums = `${view.done ?? 0}${view.unit ? ` ${view.unit}` : ''} done · unknown scope`;
  return (
    <div className="progress" data-phase={view.phase}>
      <div style={{ fontWeight: 500 }}>{view.label}</div>
      {view.percent != null ? (
        <div className="bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={view.total ?? undefined} aria-valuenow={view.done ?? undefined} aria-valuetext={nums}>
          <span style={{ width: `${view.percent}%` }} />
        </div>
      ) : (
        <div className="bar unknown" role="progressbar" aria-label={label} aria-valuetext={nums} />
      )}
      <div className="nums">{nums}</div>
    </div>
  );
}

export function CountsProgress({ done, total, unit }: { done?: number | null; total?: number | null; unit?: string }) {
  if (typeof done !== 'number') return <span className="muted">no counts reported</span>;
  if (typeof total === 'number' && total > 0) {
    const pct = Math.min(100, (done / total) * 100);
    return (
      <span className="row" style={{ gap: 8, flexWrap: 'nowrap' }}>
        <span className="bar" style={{ width: 80, display: 'inline-block' }} role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done} aria-label="Job progress">
          <span style={{ width: `${pct}%` }} />
        </span>
        <span className="small mono">
          {done}/{total}
          {unit ? ` ${unit}` : ''}
        </span>
      </span>
    );
  }
  return (
    <span className="small">
      <span className="mono">{done}</span>
      {unit ? ` ${unit}` : ''} <span className="muted">· unknown scope</span>
    </span>
  );
}
