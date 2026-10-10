import { HashRouter, Navigate, NavLink, Route, Routes } from 'react-router-dom';
import { ConnectionBanner, MockBanner } from './components/ConnectionBanner';
import { ToastProvider } from './components/Toasts';
import { StudioProvider, useApi, useResource, useStoreSelector, type StudioStore } from './lib/store';
import { getSelectedCase, useSelectedCase } from './lib/selection';
import { ProjectsView } from './views/Projects';
import { NewProjectView } from './views/NewProject';
import { WorkspaceView } from './views/Workspace';
import { ConnectionsView } from './views/Connections';
import { KnowledgeView } from './views/Knowledge';
import { SettingsView } from './views/Settings';
import { ToolsView } from './views/Tools';
import { Empty } from './components/Empty';
import { DependencyStatusBar } from './components/DependencyStatus';

function Sidebar() {
  const selected = useSelectedCase();
  const api = useApi();
  const liveName = useStoreSelector((s) => (selected ? s.state.cases[selected]?.case?.value.name ?? null : null));
  // name of the selected project even before its workspace has been opened in this session
  const fetched = useResource(() => (selected && !liveName ? api.getCase(selected) : Promise.resolve(null)), [api, selected, !!liveName]);
  const caseName = liveName ?? fetched.data?.name ?? null;
  const version = useStoreSelector((s) => s.health?.version ?? null);
  return (
    <nav className="sidebar" aria-label="Main">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true" />
        <span className="brand-name">Rebuild Studio</span>
      </div>
      <div className="nav-section">Projects</div>
      <NavLink to="/projects" end className="nav-link" data-tooltip="All projects" data-testid="nav-projects">
        <span className="nav-icon" aria-hidden="true">▦</span>
        <span className="nav-label">All projects</span>
      </NavLink>
      <NavLink to="/new" className="nav-link" data-testid="nav-new">
        <span className="nav-icon" aria-hidden="true">＋</span>
        <span className="nav-label">New project</span>
      </NavLink>
      {selected && (
        <NavLink to={`/projects/${encodeURIComponent(selected)}/overview`} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} data-testid="nav-workspace" aria-label={`Workspace: ${caseName ?? selected}`}>
          <span className="nav-icon" aria-hidden="true">◧</span>
          <span className="nav-label">{caseName ?? selected}</span>
        </NavLink>
      )}
      <div className="nav-section">Studio</div>
      <NavLink to="/connections" className="nav-link" data-testid="nav-connections">
        <span className="nav-icon" aria-hidden="true">⇄</span>
        <span className="nav-label">Connections</span>
      </NavLink>
      <NavLink to="/tools" className="nav-link" data-testid="nav-tools">
        <span className="nav-icon" aria-hidden="true">⬇</span>
        <span className="nav-label">Tools</span>
      </NavLink>
      <NavLink to="/knowledge" className="nav-link" data-testid="nav-knowledge">
        <span className="nav-icon" aria-hidden="true">◈</span>
        <span className="nav-label">Knowledge</span>
      </NavLink>
      <NavLink to="/settings" className="nav-link" data-testid="nav-settings">
        <span className="nav-icon" aria-hidden="true">⚙</span>
        <span className="nav-label">Settings</span>
      </NavLink>
      <div className="sidebar-foot">{version ? `Controller ${version}` : 'Controller version unknown'}</div>
    </nav>
  );
}

function Home() {
  const sel = getSelectedCase();
  return <Navigate to={sel ? `/projects/${encodeURIComponent(sel)}/overview` : '/projects'} replace />;
}

export function Shell() {
  return (
    <div className="app">
      <a className="skip-link" href="#main-content" onClick={(e) => {
        e.preventDefault();
        document.getElementById('main-content')?.focus();
      }}>
        Skip to content
      </a>
      <Sidebar />
      <div className="main">
        <MockBanner />
        <ConnectionBanner />
        <main className="main-scroll" id="main-content" tabIndex={-1}>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/projects" element={<ProjectsView />} />
            <Route path="/new" element={<NewProjectView />} />
            <Route path="/projects/:caseId/*" element={<WorkspaceView />} />
            <Route path="/connections" element={<ConnectionsView />} />
            <Route path="/tools" element={<ToolsView />} />
            <Route path="/knowledge" element={<KnowledgeView />} />
            <Route path="/settings" element={<SettingsView />} />
            <Route
              path="*"
              element={
                <div className="page">
                  <Empty title="Page not found">This address does not match any view. Use the sidebar to continue.</Empty>
                </div>
              }
            />
          </Routes>
        </main>
        <DependencyStatusBar />
      </div>
    </div>
  );
}

export function App({ store }: { store?: StudioStore }) {
  return (
    <StudioProvider store={store}>
      <ToastProvider>
        <HashRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Shell />
        </HashRouter>
      </ToastProvider>
    </StudioProvider>
  );
}
