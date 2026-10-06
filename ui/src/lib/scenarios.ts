import type { ScenarioStatus, UserScenario, UserScenarioBody } from './types';
import { splitArgs } from './validate';

export interface StepForm {
  /** one line, like a command line: `add notes.dat "shopping list" milk` */
  args: string;
  stdin: string;
}

export interface ScenarioForm {
  title: string;
  feature_id: string;
  steps: StepForm[];
  exit_code: boolean;
  output: boolean;
  files: boolean;
  line_endings: boolean;
  trailing_spaces: boolean;
  trim: boolean;
  ignore_timestamps: boolean;
  timeout: string;
}

export interface ScenarioFieldError {
  field: string;
  what: string;
  next: string;
}

export const emptyScenarioForm = (): ScenarioForm => ({
  title: '',
  feature_id: '',
  steps: [{ args: '', stdin: '' }],
  exit_code: true,
  output: true,
  files: false,
  line_endings: true,
  trailing_spaces: false,
  trim: false,
  ignore_timestamps: false,
  timeout: '60',
});

/** Joins arguments back into one editable line (quoting those with spaces). */
export function joinArgs(args: string[]): string {
  return args.map((a) => (a === '' || /\s/.test(a) ? `"${a}"` : a)).join(' ');
}

export function scenarioToForm(s: UserScenario): ScenarioForm {
  return {
    title: s.title,
    feature_id: s.feature_id ?? '',
    steps: s.steps.map((x) => ({ args: joinArgs(x.args), stdin: x.stdin })),
    exit_code: s.compare.exit_code,
    output: s.compare.output,
    files: s.compare.files,
    line_endings: s.normalize.line_endings,
    trailing_spaces: s.normalize.trailing_spaces,
    trim: s.normalize.trim,
    ignore_timestamps: s.normalize.ignore_timestamps,
    timeout: String(s.timeout ?? 60),
  };
}

export function formToBody(f: ScenarioForm): UserScenarioBody {
  return {
    title: f.title.trim(),
    feature_id: f.feature_id || null,
    steps: f.steps.map((s) => ({ args: splitArgs(s.args), stdin: s.stdin })),
    compare: { exit_code: f.exit_code, output: f.output, files: f.files },
    normalize: { line_endings: f.line_endings, trailing_spaces: f.trailing_spaces, trim: f.trim, ignore_timestamps: f.ignore_timestamps },
    timeout: Number(f.timeout),
  };
}

export function validateScenarioForm(f: ScenarioForm): ScenarioFieldError[] {
  const e: ScenarioFieldError[] = [];
  if (!f.title.trim()) e.push({ field: 'title', what: 'The scenario has no title.', next: 'Give it a short name such as “Add an item”.' });
  else if (f.title.trim().length > 200) e.push({ field: 'title', what: 'The title is longer than 200 characters.', next: 'Shorten the title.' });
  if (f.steps.length === 0) e.push({ field: 'steps', what: 'The scenario has no steps.', next: 'Add at least one step.' });
  if (f.steps.length > 20) e.push({ field: 'steps', what: 'A scenario can have at most 20 steps.', next: 'Split it into several scenarios.' });
  f.steps.forEach((s, i) => {
    if ((s.args.match(/"/g) ?? []).length % 2 === 1) e.push({ field: `args_${i}`, what: `Step ${i + 1}: a quotation mark is not closed.`, next: 'Close the quotation mark, or remove it.' });
    if (splitArgs(s.args).length > 64) e.push({ field: `args_${i}`, what: `Step ${i + 1}: more than 64 arguments.`, next: 'Use fewer arguments.' });
    if (s.stdin.length > 100000) e.push({ field: `stdin_${i}`, what: `Step ${i + 1}: the typed input is longer than 100,000 characters.`, next: 'Shorten it, or put the data in a file the program reads.' });
  });
  if (!f.exit_code && !f.output && !f.files) e.push({ field: 'compare', what: 'Nothing is chosen to compare.', next: 'Tick at least one of: exit code, printed text, files it writes.' });
  const t = Number(f.timeout);
  if (f.timeout.trim() === '' || !Number.isFinite(t) || t < 1 || t > 300) e.push({ field: 'timeout', what: 'The time limit must be 1 to 300 seconds.', next: 'Enter a number from 1 to 300.' });
  return e;
}

export const STATUS_TEXT: Record<ScenarioStatus, { label: string; tone: 'ok' | 'bad' | 'warn' | 'outline' | 'info'; hint: string }> = {
  no_baseline: { label: 'No baseline yet', tone: 'outline', hint: 'The original has not been run for this scenario yet. Use “Record original behaviour”.' },
  changed: { label: 'Edited since recording', tone: 'warn', hint: 'You changed this scenario after its behaviour was recorded. Record it again so the comparison uses the new steps.' },
  not_run: { label: 'Recorded, not run on the current build', tone: 'info', hint: 'The original’s behaviour is recorded. The current rebuilt version has not been checked against it yet.' },
  passed: { label: 'Passed', tone: 'ok', hint: 'The current rebuilt version behaves like the original in this scenario.' },
  failed: { label: 'Failed', tone: 'bad', hint: 'The current rebuilt version differs from the original in this scenario. See Comparisons for details.' },
};

/** One-line summary of what a scenario runs. */
export function summarize(s: UserScenario): string {
  const first = s.steps[0];
  const cmd = first ? joinArgs(first.args) || '(no arguments)' : '';
  return s.steps.length > 1 ? `${s.steps.length} steps, starting with: ${cmd}` : cmd;
}
