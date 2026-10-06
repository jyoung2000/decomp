import { describe, expect, it } from 'vitest';
import { buildLaunchProfile, comboState, emptyForm, isAbsolutePath, isInside, splitArgs, validateNewProject } from './validate';
import { describeTolerance } from './format';
import type { Capabilities } from './types';

const ok = { ...emptyForm(), name: 'Demo', source_root: 'C:\\Originals\\App', output_root: 'D:\\Out\\App' };

describe('new project validation', () => {
  it('accepts a complete form', () => {
    expect(validateNewProject(ok)).toEqual([]);
  });

  it('explains every error with what happened, what is affected and the next action', () => {
    const errs = validateNewProject(emptyForm());
    expect(errs.map((e) => e.field).sort()).toEqual(['name', 'output_root', 'source_root']);
    for (const e of errs) {
      expect(e.what.length).toBeGreaterThan(5);
      expect(e.affected.length).toBeGreaterThan(5);
      expect(e.next.length).toBeGreaterThan(5);
    }
  });

  it('rejects relative paths and outputs inside/equal to the source (case-insensitive on Windows)', () => {
    expect(validateNewProject({ ...ok, source_root: 'relative\\dir' }).map((e) => e.field)).toContain('source_root');
    expect(validateNewProject({ ...ok, output_root: 'c:\\originals\\app\\' })[0].what).toMatch(/same as the source/);
    expect(validateNewProject({ ...ok, output_root: 'C:\\Originals\\App\\out' })[0].what).toMatch(/inside the source/);
    expect(validateNewProject({ ...ok, source_root: '/a/b/c', output_root: '/a/b' })[0].what).toMatch(/source folder is inside the output/);
    expect(validateNewProject({ ...ok, source_root: '/a/b', output_root: '/a/bc' })).toEqual([]);
  });

  it('requires a bounded budget when AI is enabled', () => {
    expect(validateNewProject({ ...ok, ai_mode: 'assisted', budget_usd: '' })[0].field).toBe('budget_usd');
    expect(validateNewProject({ ...ok, ai_mode: 'assisted', budget_usd: '-1' })[0].field).toBe('budget_usd');
    expect(validateNewProject({ ...ok, ai_mode: 'assisted', budget_usd: '0.5' })).toEqual([]);
    expect(validateNewProject({ ...ok, ai_mode: 'no_ai', budget_usd: '' })).toEqual([]);
  });

  it('requires a program when executing the original, and named scenarios', () => {
    const e = validateNewProject({ ...ok, execute_original: true, scenarios: [{ name: '', args: '', stdin: '' }] });
    expect(e.map((x) => x.field)).toEqual(['program', 'scenario_0']);
  });

  it('validates resource limits', () => {
    expect(validateNewProject({ ...ok, max_workers: '0' })[0].field).toBe('max_workers');
    expect(validateNewProject({ ...ok, max_memory_mb: '128' })[0].field).toBe('max_memory_mb');
    expect(validateNewProject({ ...ok, stage_timeout_minutes: '1.5' })[0].field).toBe('stage_timeout_minutes');
    expect(validateNewProject({ ...ok, max_workers: '8', max_disk_gb: '50' })).toEqual([]);
  });

  it('rejects combinations the controller marks unsupported', () => {
    const caps: Capabilities = { output_combinations: [{ target_language: 'web', output_type: 'exe', state: 'unsupported', reason: 'web targets are not executables' }] };
    expect(comboState(caps, 'web', 'exe')?.state).toBe('unsupported');
    expect(comboState(caps, 'rust', 'exe')).toBeNull();
    const e = validateNewProject({ ...ok, target_language: 'web', output_type: 'exe' }, caps);
    expect(e[0]).toMatchObject({ field: 'output_type' });
    expect(e[0].what).toMatch(/web targets are not executables/);
  });
});

describe('helpers', () => {
  it('splits quoted arguments', () => {
    expect(splitArgs('--mode demo "file with spaces.txt"  x')).toEqual(['--mode', 'demo', 'file with spaces.txt', 'x']);
  });
  it('recognises absolute paths', () => {
    expect(isAbsolutePath('C:\\x')).toBe(true);
    expect(isAbsolutePath('\\\\server\\share\\x')).toBe(true);
    expect(isAbsolutePath('/x')).toBe(true);
    expect(isAbsolutePath('x\\y')).toBe(false);
  });
  it('detects nesting', () => {
    expect(isInside('C:/a/b', 'c:\\a')).toBe(true);
    expect(isInside('/a', '/a')).toBe(false);
  });
});

describe('describeTolerance', () => {
  it('calls an empty tolerance exact', () => {
    expect(describeTolerance({})).toEqual({ exact: true, text: 'Exact match required (no tolerance declared)' });
  });
  it('shows the declared tolerance and never claims pixel-perfect', () => {
    const t = describeTolerance({ max_diff_ratio: 0.01, ignore_regions: 1 });
    expect(t.exact).toBe(false);
    expect(t.text).toContain('Max diff ratio ≤ 0.01');
    expect(t.text).toContain('Not exact');
    expect(t.text.toLowerCase()).not.toContain('pixel-perfect');
    expect(t.text.toLowerCase()).not.toContain('exact match');
  });
});

describe('buildLaunchProfile', () => {
  const base = { ...emptyForm(), name: 'Demo', source_root: '/src', output_root: '/out' };
  it('sends only execute_original=false when capture is off', () => {
    expect(buildLaunchProfile(base)).toEqual({ execute_original: false });
  });
  it('web: kind, launch root/entry and a default scenario with an id', () => {
    const lp = buildLaunchProfile({ ...base, execute_original: true, launch_kind: 'web' });
    expect(lp).toMatchObject({ execute_original: true, kind: 'web', launch: { root: '.', entry: 'index.html' } });
    expect(lp.scenarios).toEqual([{ id: 'initial_load', title: 'Initial load', name: 'Initial load', actions: [] }]);
  });
  it('cli: documented command plus controller launch spec and steps; unique ids', () => {
    const lp = buildLaunchProfile({ ...base, execute_original: true, program: 'bin/app', args: '--x "a b"', scenarios: [{ name: 'List items', args: 'list', stdin: '' }, { name: 'List items', args: 'list -v', stdin: 'q' }] });
    expect(lp.command).toEqual(['bin/app', '--x', 'a b']);
    expect(lp.launch).toEqual({ type: 'command', command: ['bin/app', '--x', 'a b'] });
    expect(lp.scenarios?.map((s) => s.id)).toEqual(['list_items', 'list_items_2']);
    expect(lp.scenarios?.[1].steps).toEqual([{ args: ['list', '-v'], stdin: 'q' }]);
  });
  it('web entry must be relative', () => {
    const errs = validateNewProject({ ...base, execute_original: true, launch_kind: 'web', entry: '/etc/index.html' });
    expect(errs.map((e) => e.field)).toContain('entry');
    expect(validateNewProject({ ...base, execute_original: true, launch_kind: 'web' }).map((e) => e.field)).not.toContain('program');
  });
});
