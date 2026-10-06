import { humanize } from '../lib/format';

type Tone = 'ok' | 'run' | 'queue' | 'warn' | 'bad' | 'muted' | 'retest' | 'info' | 'outline';
const MAP: Record<string, [Tone, string, string?]> = {
  // plan / job
  completed: ['ok', '✓'],
  running: ['run', '▶'],
  queued: ['queue', '○'],
  blocked: ['warn', '⊘'],
  failed: ['bad', '✕'],
  cancelled: ['muted', '–'],
  needs_retest: ['retest', '↻', 'Needs retest'],
  // feedback
  received: ['info', '●'],
  triaged: ['outline', '◇'],
  in_progress: ['run', '▶', 'In progress'],
  ready_to_retest: ['retest', '↻', 'Ready to retest'],
  resolved: ['ok', '✓'],
  reopened: ['warn', '↺'],
  // knowledge
  proposed: ['outline', '◇'],
  validating: ['run', '…'],
  promoted: ['ok', '✓'],
  quarantined: ['bad', '⊘'],
  rolled_back: ['muted', '↶', 'Rolled back'],
  // verdicts
  pass: ['ok', '✓', 'Pass'],
  fail: ['bad', '✕', 'Fail'],
  error: ['warn', '!', 'Error'],
  skipped: ['muted', '–', 'Skipped'],
  // availability
  missing: ['bad', '✕'],
  detected: ['outline', '◇'],
  installed: ['queue', '○'],
  usable: ['info', '●'],
  verified: ['ok', '✓'],
  // builds / misc
  built: ['ok', '✓'],
  building: ['run', '▶'],
  pending: ['queue', '○'],
  untested: ['outline', '?'],
  partial: ['warn', '~'],
  stale: ['warn', '⟳'],
  created: ['queue', '○'],
  paused: ['warn', '||'],
  ok: ['ok', '✓', 'OK'],
  unprobed: ['outline', '?'],
  unreachable: ['bad', '✕'],
  accepted: ['ok', '✓'],
  rejected: ['bad', '✕'],
};

export function chipTone(status: string): Tone {
  return MAP[status]?.[0] ?? 'outline';
}

export function StatusChip({ status, label, title }: { status: string | null | undefined; label?: string; title?: string }) {
  const s = status ?? 'unknown';
  const [tone, glyph, text] = MAP[s] ?? ['outline', '·'];
  return (
    <span className={`chip ${tone}`} title={title} data-status={s}>
      <span className="glyph" aria-hidden="true">
        {glyph}
      </span>
      {label ?? text ?? humanize(s)}
    </span>
  );
}
