import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore } from '../test-utils';

const combos = ['rust', 'rust_bevy', 'web', 'csharp', 'java', 'auto'].flatMap((t) =>
  ['exe', 'installer', 'portable', 'web', 'pwa'].map((o) => ({ target_language: t, output_type: o, state: (t === 'csharp' || t === 'java') && (o === 'web' || o === 'pwa') ? 'unsupported' : 'supported' })));

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '#/new';
});

describe('New project: targets and forecast (R4)', () => {
  it('offers C# and Java and shows which target is the likely path to a verified rebuild', async () => {
    const { store, calls } = makeStore(caseRoutes({
      '/capabilities': { output_combinations: combos },
      '/connections': [],
      'POST /implementation/forecast': (b: unknown) => {
        const t = (b as { target_language: string }).target_language;
        return t === 'csharp'
          ? { state: 'native_rebuild', summary: 'Rebuild in the original language: the C# that ILSpy recovers is compiled and checked first.', details: [], blockers: [],
              can_produce_implementation: true, will_use_ai: false, likely_path: 'C# is the likely path to a verified rebuild for this program.' }
          : { state: 'scaffold_only', summary: 'No AI connected: you will get recovered evidence and a Rust scaffold.', details: [], blockers: [], can_produce_implementation: false,
              will_use_ai: false, likely_path: 'Auto picks the original language when it can: .NET programs are rebuilt in C#, Java programs in Java.' };
      },
    }));
    render(<App store={store} />);
    const csharp = await screen.findByRole('radio', { name: /^C#/ });
    expect(screen.getByRole('radio', { name: /^Java/ })).toBeInTheDocument();
    const panel = await screen.findByTestId('target-forecast');
    expect(panel).toHaveTextContent('.NET programs are rebuilt in C#');
    await userEvent.click(csharp);
    await waitFor(() => expect(screen.getByTestId('target-forecast')).toHaveTextContent('Rebuild in the original language'));
    expect(screen.getByTestId('likely-path')).toHaveTextContent('C# is the likely path to a verified rebuild');
    const sent = calls.filter((c) => c.path === '/implementation/forecast').map((c) => (c.body as { target_language: string }).target_language);
    expect(sent).toContain('csharp');
    expect(screen.getByTestId('output-web').querySelector('input')).toBeDisabled();       // C# builds a program, not a site
    store.stop();
  });
});
