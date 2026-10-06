import type { AiMode, Capabilities, LaunchKind, LaunchProfile, OutputType, TargetLanguage } from './types';

export interface FieldError {
  field: string;
  what: string;
  affected: string;
  next: string;
}

export interface NewProjectForm {
  name: string;
  source_root: string;
  output_root: string;
  target_language: TargetLanguage | '';
  output_type: OutputType | '';
  execute_original: boolean;
  launch_kind: LaunchKind;
  /** web: entry page relative to the site folder */
  entry: string;
  /** web: site folder relative to the source folder */
  site_root: string;
  program: string;
  args: string;
  scenarios: { name: string; args: string; stdin: string }[];
  ai_mode: AiMode;
  budget_usd: string;
  max_workers: string;
  max_memory_mb: string;
  max_disk_gb: string;
  stage_timeout_minutes: string;
}

export function isAbsolutePath(p: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(p) || /^\\\\[^\\]+\\[^\\]+/.test(p) || p.startsWith('/');
}

function norm(p: string): string {
  let s = p.trim().replace(/\\/g, '/');
  if (/^[A-Za-z]:\//.test(s)) s = s.toLowerCase(); // Windows paths are case-insensitive
  return s.replace(/\/+$/, '');
}

export function isInside(child: string, parent: string): boolean {
  const c = norm(child);
  const p = norm(parent);
  return c !== p && c.startsWith(p + '/');
}

/** Splits an argument string honouring double quotes: `a "b c" d` → [a, "b c", d]. */
export function splitArgs(s: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s))) out.push(m[1] ?? m[2]);
  return out;
}

export function comboState(caps: Capabilities | null | undefined, t: TargetLanguage | '', o: OutputType) {
  if (!caps || !t) return null;
  return caps.output_combinations.find((c) => c.target_language === t && c.output_type === o) ?? null;
}

function intIn(v: string, min: number, max: number): boolean {
  if (v.trim() === '') return true;
  const n = Number(v);
  return Number.isInteger(n) && n >= min && n <= max;
}

export function validateNewProject(f: NewProjectForm, caps?: Capabilities | null): FieldError[] {
  const e: FieldError[] = [];
  if (!f.name.trim()) e.push({ field: 'name', what: 'The project has no name.', affected: 'The project list and exported reports use this name.', next: 'Enter a short name such as “Inventory tool rebuild”.' });
  else if (f.name.length > 120) e.push({ field: 'name', what: 'The name is longer than 120 characters.', affected: 'Report titles and folder labels.', next: 'Shorten the name.' });

  if (!f.source_root.trim()) e.push({ field: 'source_root', what: 'No source folder was chosen.', affected: 'Discovery cannot start without the original program.', next: 'Choose the folder that contains the original program.' });
  else if (!isAbsolutePath(f.source_root.trim())) e.push({ field: 'source_root', what: 'The source folder is not a full path.', affected: 'The controller cannot locate the original files.', next: 'Use a full path such as C:\\Games\\MyApp.' });

  if (!f.output_root.trim()) e.push({ field: 'output_root', what: 'No output folder was chosen.', affected: 'Rebuilt source, builds, evidence and reports have nowhere to go.', next: 'Choose an empty folder for the rebuilt output.' });
  else if (!isAbsolutePath(f.output_root.trim())) e.push({ field: 'output_root', what: 'The output folder is not a full path.', affected: 'Outputs cannot be written.', next: 'Use a full path such as D:\\Rebuilds\\MyApp.' });
  else if (f.source_root.trim() && isAbsolutePath(f.source_root.trim())) {
    if (norm(f.output_root) === norm(f.source_root)) e.push({ field: 'output_root', what: 'The output folder is the same as the source folder.', affected: 'Originals must stay untouched; writing outputs there is refused.', next: 'Choose a different, empty folder for outputs.' });
    else if (isInside(f.output_root, f.source_root)) e.push({ field: 'output_root', what: 'The output folder is inside the source folder.', affected: 'Outputs would be scanned as part of the original on the next discovery.', next: 'Choose an output folder outside the source folder.' });
    else if (isInside(f.source_root, f.output_root)) e.push({ field: 'output_root', what: 'The source folder is inside the output folder.', affected: 'Publishing outputs could overwrite or delete original files.', next: 'Choose an output folder that does not contain the source.' });
  }

  if (!f.target_language) e.push({ field: 'target_language', what: 'No target language was selected.', affected: 'Reconstruction and build cannot be planned.', next: 'Pick Rust, Rust + Bevy, HTML/CSS/JS or Auto.' });
  if (!f.output_type) e.push({ field: 'output_type', what: 'No output type was selected.', affected: 'The build step does not know what to package.', next: 'Pick an output type.' });
  else {
    const st = comboState(caps, f.target_language, f.output_type);
    if (st?.state === 'unsupported') e.push({ field: 'output_type', what: `This output type is not supported for the chosen target${st.reason ? `: ${st.reason}` : '.'}`, affected: 'Packaging would fail at the build step.', next: 'Choose a supported output type or another target language.' });
  }

  if (f.execute_original) {
    if (f.launch_kind === 'web') {
      if (/^([A-Za-z]:)?[\\/]/.test(f.entry.trim()) || f.entry.includes('..')) e.push({ field: 'entry', what: 'The entry page must be relative to the site folder.', affected: 'The original site cannot be served for capture.', next: 'Enter a relative page such as index.html.' });
    } else if (!f.program.trim()) e.push({ field: 'program', what: 'Running the original is enabled but no program was given.', affected: 'Behaviour capture of the original (used for comparisons) cannot run.', next: 'Enter the executable to run (relative to the source folder), or turn off “Execute original”.' });
    f.scenarios.forEach((s, i) => {
      if (!s.name.trim()) e.push({ field: `scenario_${i}`, what: `Scenario ${i + 1} has no name.`, affected: 'Comparison rows are labelled by scenario name.', next: 'Name the scenario or remove it.' });
    });
  }

  if (f.ai_mode !== 'no_ai') {
    const b = Number(f.budget_usd);
    if (f.budget_usd.trim() === '' || !Number.isFinite(b) || b <= 0) e.push({ field: 'budget_usd', what: 'AI is enabled but no per-job budget is set.', affected: 'AI calls would be refused because spending must be bounded.', next: 'Enter a per-job budget in USD (for example 0.50), or choose “No AI”.' });
    else if (b > 1000) e.push({ field: 'budget_usd', what: 'The per-job budget is above $1000.', affected: 'Every AI-assisted job could spend up to this amount.', next: 'Enter a smaller budget.' });
  }

  if (!intIn(f.max_workers, 1, 64)) e.push({ field: 'max_workers', what: 'Workers must be a whole number from 1 to 64.', affected: 'How many jobs run at once.', next: 'Enter a value from 1 to 64 or leave empty for the default.' });
  if (!intIn(f.max_memory_mb, 256, 1048576)) e.push({ field: 'max_memory_mb', what: 'Memory limit must be a whole number of at least 256 MB.', affected: 'Each worker process is capped at this memory.', next: 'Enter at least 256, or leave empty for the default.' });
  if (!intIn(f.max_disk_gb, 1, 100000)) e.push({ field: 'max_disk_gb', what: 'Disk limit must be a whole number of at least 1 GB.', affected: 'Extraction and builds stop when this is exceeded.', next: 'Enter at least 1, or leave empty for the default.' });
  if (!intIn(f.stage_timeout_minutes, 1, 1440)) e.push({ field: 'stage_timeout_minutes', what: 'Stage timeout must be 1–1440 minutes.', affected: 'Long stages are stopped after this time.', next: 'Enter 1–1440 or leave empty for the default.' });
  return e;
}

export function emptyForm(): NewProjectForm {
  return {
    name: '',
    source_root: '',
    output_root: '',
    target_language: 'auto',
    output_type: 'portable',
    execute_original: false,
    launch_kind: 'cli',
    entry: 'index.html',
    site_root: '.',
    program: '',
    args: '',
    scenarios: [],
    ai_mode: 'no_ai',
    budget_usd: '',
    max_workers: '',
    max_memory_mb: '',
    max_disk_gb: '',
    stage_timeout_minutes: '',
  };
}

/** Stable scenario ids derived from names (the controller keys captures and features by id). */
export function scenarioId(name: string, taken: Set<string>): string {
  const base = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40) || 'scenario';
  let id = base;
  for (let i = 2; taken.has(id); i++) id = `${base}_${i}`;
  taken.add(id);
  return id;
}

/**
 * Builds launch_profile. Sends both the documented fields (command, scenarios[].name/args) and the fields the
 * controller's capture stage reads (kind, launch, scenarios[].id/steps).
 */
export function buildLaunchProfile(f: NewProjectForm): LaunchProfile {
  if (!f.execute_original) return { execute_original: false };
  const taken = new Set<string>();
  if (f.launch_kind === 'web') {
    return {
      execute_original: true,
      kind: 'web',
      launch: { root: f.site_root.trim() || '.', entry: f.entry.trim() || 'index.html' },
      // with no named scenario the entry page is still loaded and captured once
      scenarios: f.scenarios.length
        ? f.scenarios.map((s) => ({ id: scenarioId(s.name, taken), title: s.name.trim(), name: s.name.trim(), actions: [] }))
        : [{ id: 'initial_load', title: 'Initial load', name: 'Initial load', actions: [] }],
    };
  }
  const command = [f.program.trim(), ...splitArgs(f.args)];
  return {
    execute_original: true,
    kind: 'cli',
    command,
    launch: { type: 'command', command },
    // with no named scenario the original is still run once with the base arguments
    scenarios: (f.scenarios.length ? f.scenarios : [{ name: 'Default run', args: '', stdin: '' }]).map((s) => {
      const args = splitArgs(s.args);
      const stdin = s.stdin ? { stdin: s.stdin } : {};
      return { id: scenarioId(s.name, taken), title: s.name.trim(), name: s.name.trim(), args, ...stdin, steps: [{ args, ...stdin }] };
    }),
  };
}
