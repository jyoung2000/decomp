import { describe, expect, it } from 'vitest';
import { applyEvent, describeEvent, emptyStudio, jobCounts, normalizeProgress, phaseViews } from './derive';
import type { Plan } from './types';
import { Api, describeError } from './api';

// Shapes observed from the real controller (rebuild_controller) that differ from docs/API.md.
describe('controller progress shapes', () => {
  it('flattens plan progress.groups (discovery → analysis) and keeps unknown totals unknown', () => {
    const real = {
      jobs: { queued: 0, running: 0, completed: 3 },
      groups: {
        discovery: { done: 2, total: 3, failed: 0, running: 0, unit: 'jobs' },
        recovery: { done: 1, total: 2, failed: 1, running: 0, unit: 'jobs' },
        implementation: { done: 0, total: null, unit: 'features' },
        build: { done: 0, total: null, unit: 'jobs' },
        verification: { done: 0, total: null, unit: 'features' },
      },
    } as unknown as Plan['progress'];
    const v = phaseViews(normalizeProgress(real)!);
    expect(v.map((x) => [x.phase, x.done, x.total, x.percent != null])).toEqual([
      ['analysis', 2, 3, true],
      ['recovery', 1, 2, true],
      ['implementation', 0, null, false],
      ['build', 0, null, false],
      ['verification', 0, null, false],
    ]);
  });
  it('leaves the documented shape alone', () => {
    const doc = { analysis: { done: 1, total: null } } as Plan['progress'];
    expect(normalizeProgress(doc)).toBe(doc);
  });
  it('derives raw job counts from free-form fields without inventing totals', () => {
    expect(jobCounts({ scenarios_done: 1, scenarios_total: 3 })).toMatchObject({ done: 1, total: 3, unit: 'scenarios' });
    expect(jobCounts({ module: 'app.exe', functions_decompiled: 4, functions_total: 9, decompile_errors: 1 })).toMatchObject({ done: 4, total: 9, unit: 'functions', current: 'app.exe' });
    expect(jobCounts({ files_scanned: 6, modules: 2 })).toMatchObject({ done: 6, total: null, unit: 'files' });
    expect(jobCounts({ done: 2, total: null, unit: 'files' })).toMatchObject({ done: 2, total: null, unit: 'files' });
    expect(jobCounts({})).toMatchObject({ done: null, total: null });
  });
});

describe('undocumented controller event kinds', () => {
  const ev = (kind: string, payload: Record<string, unknown>) => ({ seq: 10, ts: '2026-10-06T12:00:00Z', case_id: 'case_x', job_id: null, kind, payload });
  it('verification.completed refreshes candidates, features and previews and reads well', () => {
    const r = applyEvent(emptyStudio(), ev('verification.completed', { candidate_id: 'c1', summary: { scenarios: 1, passed: 0, failed: 1, errors: 0 } }));
    expect(r.refresh.map((x) => x.key).sort()).toEqual(['candidates', 'features', 'previews']);
    expect(describeEvent(ev('verification.completed', { summary: { scenarios: 1, passed: 0, failed: 1, errors: 0 } }))).toBe('Verification finished: 0 passed, 1 failed, 0 errors of 1 scenarios');
  });
  it('preview.opened / feature.stale have readable descriptions', () => {
    expect(describeEvent(ev('preview.opened', { url: 'http://127.0.0.1:1/index.html' }))).toBe('Preview opened at http://127.0.0.1:1/index.html');
    expect(describeEvent(ev('feature.stale', { count: 2, reason: 'new candidate' }))).toBe('2 feature result(s) marked stale: new candidate');
  });
});

describe('controller error bodies', () => {
  const api = (status: number, body: unknown) =>
    new Api({ baseUrl: '', token: 't', mock: false, source: 'url' }, (async () => new Response(JSON.stringify(body), { status })) as typeof fetch);
  it('FastAPI 422 detail list → what/affected/next', async () => {
    const e = await api(422, { detail: [{ loc: ['body', 'priority'], msg: 'bad pattern', type: 'x' }] }).post('/cases/c/feedback', {}).catch((x) => x);
    expect(describeError(e)).toMatchObject({ what: 'The controller rejected the request: priority: bad pattern.', next: 'Correct the listed fields and try again.', code: 'validation_error' });
  });
  it('404 KeyError → readable not-found', async () => {
    const e = await api(404, { error: { code: 'KeyError', message: "'nope'", next_action: null } }).get('/cases/nope').catch((x) => x);
    expect(describeError(e)).toMatchObject({ what: 'nope was not found (GET /cases/nope).', code: 'not_found' });
    expect(describeError(e).next).toBeTruthy();
  });
  it('404 for an unknown route', async () => {
    const e = await api(404, { detail: 'Not Found' }).get('/capabilities').catch((x) => x);
    expect(describeError(e).what).toBe('GET /capabilities: Not Found.');
  });
  it('preview open answered 200 with opened:false is an error', async () => {
    const e = await api(200, { opened: false, message: 'preview files missing', next_action: 'rebuild the candidate' }).openPreview('p').catch((x) => x);
    expect(describeError(e)).toMatchObject({ what: 'preview files missing', next: 'rebuild the candidate', code: 'preview_not_opened' });
  });
  it('budgets accepts both {budgets, quotas} and a bare list', async () => {
    expect(await api(200, { budgets: [{ budget_id: 'b' }], quotas: [] }).budgets()).toEqual([{ budget_id: 'b' }]);
    expect(await api(200, [{ budget_id: 'c' }]).budgets()).toEqual([{ budget_id: 'c' }]);
  });
});
