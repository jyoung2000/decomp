import { Link } from 'react-router-dom';
import { Empty, Loading } from '../components/Empty';
import { ErrorCallout } from '../components/ErrorCallout';
import { CaseStatusChip } from '../components/Outcome';
import { dateTime } from '../lib/format';
import { setSelectedCase, useSelectedCase } from '../lib/selection';
import { useApi, useResource, useStoreSelector } from '../lib/store';

const TARGET: Record<string, string> = { rust: 'Rust', rust_bevy: 'Rust + Bevy', web: 'HTML/CSS/JS', auto: 'Auto' };

export function ProjectsView() {
  const api = useApi();
  const version = useStoreSelector((s) => Object.keys(s.state.cases).length + (s.health ? 1 : 0));
  const res = useResource(() => api.cases(), [api], version);
  const selected = useSelectedCase();
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Projects</h1>
          <p className="lead">Each project rebuilds one original program into a new target. Select a project to open its workspace.</p>
        </div>
        <Link className="btn primary" to="/new">
          New project
        </Link>
      </div>
      {res.error ? (
        <ErrorCallout error={res.error} title="Projects could not be loaded" onRetry={res.reload} />
      ) : res.loading && !res.data ? (
        <Loading what="projects" />
      ) : !res.data?.length ? (
        <div className="card">
          <Empty title="No projects yet" action={<Link className="btn primary" to="/new">Create your first project</Link>}>
            Choose the folder that contains the original program, where the rebuilt output should go, and the target language. The plan appears as soon as discovery starts.
          </Empty>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="Projects">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Status</th>
                <th scope="col">Target</th>
                <th scope="col">Output</th>
                <th scope="col">Created</th>
              </tr>
            </thead>
            <tbody>
              {res.data.map((c) => (
                <tr key={c.case_id} aria-current={c.case_id === selected ? 'true' : undefined}>
                  <td>
                    <Link to={`/projects/${encodeURIComponent(c.case_id)}/overview`} onClick={() => setSelectedCase(c.case_id)} data-testid={`project-link-${c.case_id}`}>
                      {c.name}
                    </Link>
                    <div className="xs muted mono">{c.case_id}</div>
                  </td>
                  <td>
                    <CaseStatusChip status={c.status} outcome={c.outcome ?? null} />
                  </td>
                  <td>
                    {TARGET[c.target_language] ?? c.target_language} · {c.output_type}
                  </td>
                  <td className="wrap-any small">{c.output_root}</td>
                  <td className="small">{dateTime(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
