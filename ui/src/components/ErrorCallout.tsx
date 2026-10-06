import { describeError } from '../lib/api';

/** Every error explains what happened, what is affected and the next action. */
export function ErrorCallout({ error, title, onRetry, tone = 'bad' }: { error: unknown; title?: string; onRetry?: () => void; tone?: 'bad' | 'warn' }) {
  if (!error) return null;
  const d = describeError(error);
  return (
    <div className={`callout ${tone}`} role="alert">
      {title && <div className="ttl">{title}</div>}
      <div>
        <strong>What happened: </strong>
        {d.what}
        {d.code && <span className="muted xs"> ({d.code})</span>}
      </div>
      <div>
        <strong>Affected: </strong>
        {d.affected ?? 'This view’s data may be incomplete.'}
      </div>
      <div>
        <strong>Next: </strong>
        {d.next ?? 'Retry. If it keeps failing, check Advanced → Raw logs for details.'}
      </div>
      {onRetry && (
        <div>
          <button type="button" className="btn sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}
    </div>
  );
}

export function Explain({ what, affected, next, tone = 'bad' }: { what: string; affected: string; next: string; tone?: 'bad' | 'warn' | 'info' }) {
  return (
    <div className={`callout ${tone}`} role={tone === 'info' ? 'note' : 'alert'}>
      <div>
        <strong>What happened: </strong>
        {what}
      </div>
      <div>
        <strong>Affected: </strong>
        {affected}
      </div>
      <div>
        <strong>Next: </strong>
        {next}
      </div>
    </div>
  );
}
