import { useState } from 'react';
import { costText, modelText, taskLabel } from '../../lib/ai';
import { plural, usd } from '../../lib/format';
import type { PlanAi } from '../../lib/types';
import { LocalityBadge, OriginBadge } from './Badges';

export { OriginBadge };

/** Compact AI chip for a plan item or job, expandable to the ordered fallbacks. */
export function AiLine({ id, ai }: { id: string; ai: PlanAi }) {
  const [open, setOpen] = useState(false);
  const fb = ai.fallbacks ?? [];
  const cost = costText(ai);
  const rationale = ai.rationale && ai.rationale !== 'auto' ? ai.rationale : 'Auto';
  const detailsId = `ai-details-${id}`;
  const without = ai.runs_without_ai;
  return (
    <div className="ai-line" data-testid={`ai-chip-${id}`}>
      <div className="ai-chip">
        <span className="chip retest" title="This work may use an AI model">
          AI
        </span>
        <span>{taskLabel(ai.task)}</span>
        <span aria-hidden="true">·</span>
        {ai.primary ? (
          <>
            <span className="mono wrap-any">{ai.primary.model}</span>
            <LocalityBadge locality={ai.primary.locality} />
          </>
        ) : (
          <span className="muted">no model available</span>
        )}
        {fb.length > 0 && <span className="small">+{plural(fb.length, 'fallback')}</span>}
        <span className="small muted" title="Why this model was chosen">
          {rationale}
        </span>
        {cost && (
          <span className={`chip ${cost === 'unknown price' ? 'warn' : 'outline'}`} title={cost === 'unknown price' ? 'The price of the chosen model is not known, so it may need your approval before it runs.' : 'Expected cost of this work'}>
            {cost === 'unknown price' ? 'Unknown price' : cost}
          </span>
        )}
        {without != null && (
          <span className={`chip ${without ? 'ok' : 'warn'}`} title={ai.without_ai ?? undefined}>
            {without ? 'Works without AI' : 'Needs AI'}
          </span>
        )}
        <button type="button" className="btn sm ghost" aria-expanded={open} aria-controls={detailsId} onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide AI details' : 'AI details'}
        </button>
      </div>
      {open && (
        <div className="ai-details" id={detailsId}>
          <ol className="bullets">
            {ai.primary && (
              <li>
                <strong>Primary:</strong> {modelText(ai.primary)}
              </li>
            )}
            {fb.map((m, i) => (
              <li key={`${m.model}${i}`}>
                <strong>Fallback {i + 1}:</strong> {modelText(m)}
              </li>
            ))}
          </ol>
          {fb.length === 0 && <p className="small muted">No fallbacks: if the primary is unavailable this work waits or is marked blocked.</p>}
          {ai.budget_usd != null && <p className="small">Budget: {usd(ai.budget_usd)}</p>}
          <p className="small">
            <strong>Without AI:</strong> {ai.without_ai ?? (without ? 'This work still completes using deterministic tools.' : 'This work cannot be done deterministically; it becomes a scaffold or is blocked.')}
          </p>
        </div>
      )}
    </div>
  );
}
