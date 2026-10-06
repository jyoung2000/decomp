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
  delivered: ['ok', '✓'],
  runnable: ['info', '●'],
  planned: ['queue', '○'],
  unplanned: ['outline', '◇'],
  unsupported: ['muted', '–'],
  deferred: ['muted', '…'],
  ok: ['ok', '✓', 'OK'],
  unprobed: ['outline', '?'],
  unreachable: ['bad', '✕'],
  accepted: ['ok', '✓'],
  rejected: ['bad', '✕'],
};

/** Plain-language meaning shown as the chip's tooltip (and to assistive tech) unless the caller passes its own title. */
const MEANING: Record<string, string> = {
  completed: 'Finished successfully',
  running: 'Work is in progress right now',
  queued: 'Waiting for a worker or a dependency',
  blocked: 'Cannot continue until a blocker is resolved',
  failed: 'Stopped with an error — see details or Advanced → Raw logs',
  cancelled: 'Stopped by request; completed work is kept and you can resume',
  needs_retest: 'Something it depends on changed; verification must run again',
  received: 'Saved by the controller, not yet triaged',
  triaged: 'Reviewed; no work linked yet',
  in_progress: 'Linked work is being done',
  ready_to_retest: 'A newer build should fix this — please test again',
  resolved: 'Closed; no further action',
  reopened: 'Reopened after a retest',
  proposed: 'Suggested knowledge, not yet validated',
  validating: 'Being checked against fixtures',
  promoted: 'Validated and in use',
  quarantined: 'Failed validation; not used',
  rolled_back: 'Reverted to the previous version',
  pass: 'Matches the original within the declared rule',
  fail: 'Differs from the original beyond the declared rule',
  error: 'The comparison could not run',
  skipped: 'Not compared (not applicable or not requested)',
  missing: 'Not found on this machine',
  detected: 'Found, but not installed by Rebuild Studio',
  installed: 'Installed, not yet smoke-tested',
  usable: 'Installed and passed a smoke test',
  verified: 'Verified against a known-good fixture',
  built: 'Build finished; output hash recorded',
  building: 'Build is running',
  pending: 'Not started yet',
  untested: 'No verification has run yet',
  partial: 'Some checks passed, some did not run or failed',
  stale: 'Superseded by newer work; information may be out of date',
  created: 'Created, not started',
  paused: 'Paused; press Resume to continue',
  delivered: 'Finished: the output folder holds the delivered build and reports',
  runnable: 'A runnable implementation exists; not yet verified against the original',
  planned: 'In the plan, not started',
  unplanned: 'Detected, but no plan work exists for it yet',
  unsupported: 'Cannot be rebuilt with the available backends',
  deferred: 'Postponed; not part of the current delivery',
  ok: 'Reachable and working',
  unprobed: 'Not tested yet — use Probe',
  unreachable: 'Could not be reached',
  accepted: 'Accepted by you',
  rejected: 'Rejected by you',
  unknown: 'Not reported by the controller',
};

export function chipMeaning(status: string | null | undefined): string | undefined {
  return MEANING[status ?? 'unknown'];
}

export function chipTone(status: string): Tone {
  return MAP[status]?.[0] ?? 'outline';
}

export function StatusChip({ status, label, title }: { status: string | null | undefined; label?: string; title?: string }) {
  const s = status ?? 'unknown';
  const [tone, glyph, text] = MAP[s] ?? ['outline', '·'];
  return (
    <span className={`chip ${tone}`} title={title ?? MEANING[s]} data-status={s}>
      <span className="glyph" aria-hidden="true">
        {glyph}
      </span>
      {label ?? text ?? humanize(s)}
    </span>
  );
}
