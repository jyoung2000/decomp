import { aiQuality, CODE_TASKS, scoreLine, type ModelScore } from '../../lib/aiQuality';
import { taskLabel } from '../../lib/ai';
import { useApi, useResource } from '../../lib/store';
import { LocalityBadge } from './Badges';

/** R5: what each model actually achieved on this PC (builds, verifier verdicts, usable names), used by the ladder presets. */
export function ModelScoreboard({ version = 0 }: { version?: number }) {
  const api = useApi();
  const q = aiQuality(api);
  const board = useResource(() => q.scoreboard(), [api], version);
  const models = board.data?.models ?? [];
  return (
    <section className="card" aria-labelledby="scoreboard-h" data-testid="model-scoreboard">
      <div className="card-head">
        <h3 id="scoreboard-h">Model scoreboard</h3>
        <button type="button" className="btn sm" onClick={board.reload} aria-label="Refresh the model scoreboard">
          Refresh
        </button>
      </div>
      <p className="small muted" style={{ marginBottom: 12 }}>
        Measured on this PC: every AI attempt (did it compile, did the verifier pass it) and every naming call. Presets put the models that worked here first and warn about
        models that never compiled anything. Builds and the verifier decide; a model&apos;s own claims are never counted.
      </p>
      {board.error ? (
        <p className="small muted" data-testid="scoreboard-unavailable">
          The scoreboard is not available from this controller yet.
        </p>
      ) : !board.data ? (
        <p className="small muted">Loading the scoreboard…</p>
      ) : models.length === 0 ? (
        <p className="small muted" data-testid="scoreboard-empty">
          Nothing measured yet. It fills in as projects use AI, or import a benchmark run (scripts/benchmark.py --models).
        </p>
      ) : (
        <table className="table" aria-label="Model scoreboard">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Where</th>
              <th scope="col">Measured</th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <ScoreRow key={m.model} m={m} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ScoreRow({ m }: { m: ModelScore }) {
  const tasks = Object.entries(m.tasks);
  const neverCompiled = m.code_runs > 0 && !m.ever_compiled;
  return (
    <tr data-testid={`score-${m.model}`}>
      <td>
        <strong>{m.model}</strong>
        {neverCompiled && (
          <span className="chip warn" style={{ marginLeft: 6 }} title="Tried for code on this PC and nothing it wrote compiled">
            Never compiled
          </span>
        )}
        {m.ever_compiled && (
          <span className="chip ok" style={{ marginLeft: 6 }}>
            Compiled code here
          </span>
        )}
      </td>
      <td>
        <LocalityBadge locality={(m.locality as 'local' | 'cloud' | null) ?? null} />
      </td>
      <td>
        <ul className="list small">
          {tasks.map(([task, t]) => (
            <li key={task}>
              <strong>{taskLabel(task)}:</strong> {scoreLine(task, t)}
              {!CODE_TASKS.includes(task) && t.sources?.includes('benchmark') ? ' (includes benchmark runs)' : ''}
            </li>
          ))}
        </ul>
      </td>
    </tr>
  );
}
