import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type MutableRefObject, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import {
  GRAPH_NODE_CAPS,
  XREF_LIMIT,
  XREF_LIMIT_MAX,
  buildCallTree,
  reRefsApi,
  type CallGraph,
  type CallTreeNode,
  type FunctionXrefs,
  type GraphDirection,
  type RefKind,
  type XrefRow,
} from '../../lib/re';
import { useApi } from '../../lib/store';
import { useLoad } from './useLoad';

const n = (v: number) => v.toLocaleString('en-US');

/** How the pane links to another function: `href(addr)` is the URL search that selects it (so the link can be opened in a new
 * window), `open(addr)` selects it in place (table, pane and URL). */
export interface FnNav {
  href: (addr: string) => string;
  open: (addr: string) => void;
}

const KIND_LABEL: Record<RefKind, string> = { call: 'call', jump: 'jump', data: 'data', string: 'string' };
const KIND_HELP: Record<RefKind, string> = {
  call: 'a call instruction',
  jump: 'a jump (for example a tail call or a thunk)',
  data: 'a data reference (an address is read, written or taken)',
  string: 'a reference to a string in the program',
};

function Kind({ kind }: { kind: RefKind }) {
  return (
    <span className={`chip ref-kind ref-${kind}`} title={KIND_HELP[kind]}>
      {KIND_LABEL[kind] ?? kind}
    </span>
  );
}

/** A link that selects another function. Plain clicks select in place; modified clicks keep the browser's behaviour. */
export function FnLink({ addr, name, nav, current, testId, tabIndex }: { addr: string; name?: string | null; nav: FnNav; current?: string; testId?: string; tabIndex?: number }) {
  const self = addr === current;
  return (
    <Link
      to={{ search: nav.href(addr) }}
      replace
      className="mono wrap-any fn-link"
      tabIndex={tabIndex}
      onClick={(e) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        nav.open(addr);
      }}
      aria-label={`Open ${name || addr}${name ? ` at ${addr}` : ''}${self ? ' (this function)' : ''}`}
      data-testid={testId}
    >
      {name || addr}
      {self && <span className="muted"> (this function)</span>}
    </Link>
  );
}

// ------------------------------------------------------------------------------------------------ References
export function ReferencesView({ caseId, moduleId, addr, nav }: { caseId: string; moduleId: string; addr: string; nav: FnNav }) {
  const api = useApi();
  const re = useMemo(() => reRefsApi(api), [api]);
  const [limit, setLimit] = useState(XREF_LIMIT);
  const res = useLoad<FunctionXrefs>(`${moduleId}|${addr}|${limit}`, () => re.xrefs(caseId, moduleId, addr, limit));
  if (res.error) return <ErrorCallout error={res.error} title="References could not be listed" onRetry={res.reload} />;
  const x = res.data;
  if (!x) return <Loading what="references (the first time, the program's strings are read too)" />;
  const more = (d: FunctionXrefs['to']) =>
    d.more && (
      <p className="small callout info" data-testid={`fn-refs-more-${d.direction}`}>
        Showing the first {n(d.refs.length)} of {n(d.total)}.{' '}
        {limit < XREF_LIMIT_MAX ? (
          <button type="button" className="btn sm" onClick={() => setLimit(XREF_LIMIT_MAX)} data-testid={`fn-refs-all-${d.direction}`}>
            Show up to {n(XREF_LIMIT_MAX)}
          </button>
        ) : (
          <span className="muted">Only the first {n(XREF_LIMIT_MAX)} are listed here; the MCP tools xrefs_to / xrefs_from page through the rest.</span>
        )}
      </p>
    );
  return (
    <div className="stack fn-refs" data-testid="fn-refs">
      <section aria-labelledby="fn-refs-to-h" className="stack">
        <h4 id="fn-refs-to-h" className="fn-sub-h">
          Called from <span className="muted small">({n(x.to.total)})</span>
        </h4>
        {x.to.total === 0 ? (
          <p className="small muted" data-testid="fn-refs-to-empty">
            Nothing in the analysed code calls or references this function. It may be an entry point, an export, or reached only through a pointer computed at run time; try the
            Call graph with direction “Called by” or check the Disassembly of likely callers.
          </p>
        ) : (
          <RefTable rows={x.to.refs} dir="to" nav={nav} current={x.addr} />
        )}
        {more(x.to)}
      </section>
      <section aria-labelledby="fn-refs-from-h" className="stack">
        <h4 id="fn-refs-from-h" className="fn-sub-h">
          Calls / uses <span className="muted small">({n(x.from.total)})</span>
        </h4>
        {x.from.total === 0 ? (
          <p className="small muted" data-testid="fn-refs-from-empty">
            This function makes no calls and uses no data the analysis could resolve. Indirect calls through registers are not listed; read the Disassembly.
          </p>
        ) : (
          <RefTable rows={x.from.refs} dir="from" nav={nav} current={x.addr} />
        )}
        {more(x.from)}
        {x.internal_jumps > 0 && (
          <p className="xs muted" data-testid="fn-refs-internal">
            {n(x.internal_jumps)} jump{x.internal_jumps === 1 ? '' : 's'} inside this function {x.internal_jumps === 1 ? 'is' : 'are'} not listed (control flow; see Disassembly).
          </p>
        )}
      </section>
    </div>
  );
}

function RefTable({ rows, dir, nav, current }: { rows: XrefRow[]; dir: 'to' | 'from'; nav: FnNav; current: string }) {
  const caption = dir === 'to' ? 'Places that call or reference this function' : 'Functions, data and strings this function references';
  return (
    <div className="table-wrap">
      <table className="table fn-refs-table small" data-testid={`fn-refs-${dir}`}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">{dir === 'to' ? 'Call site' : 'Target'}</th>
            <th scope="col">Kind</th>
            <th scope="col">{dir === 'to' ? 'In function' : 'Function or item'}</th>
            {dir === 'from' && (
              <th scope="col" className="col-at">
                At
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const fa = dir === 'to' ? r.fcn_addr : r.to_fcn_addr;
            const fname = dir === 'to' ? r.fcn_name : r.to_fcn_name;
            let item: ReactNode;
            if (fa) {
              const inside = dir === 'from' && r.to !== fa;
              item = (
                <>
                  <FnLink addr={fa} name={fname} nav={nav} current={current} testId={`fn-ref-link-${dir}-${i}`} />
                  {inside && <span className="xs muted"> (inside it, at {r.to})</span>}
                </>
              );
            } else if (r.kind === 'string' && r.string !== undefined) {
              item = <span className="mono wrap-any fn-ref-string">“{r.string}”</span>;
            } else if (r.name) {
              item = (
                <span className="mono wrap-any">
                  {r.name} <span className="xs muted">import</span>
                </span>
              );
            } else {
              item = <span className="muted">{dir === 'to' ? 'not inside an analysed function' : 'no function or known item'}</span>;
            }
            return (
              <tr key={`${r.from}>${r.to}>${i}`} data-testid={`fn-ref-${dir}-${i}`}>
                <td className="mono">{dir === 'to' ? r.from : r.to}</td>
                <td>
                  <Kind kind={r.kind} />
                </td>
                <td className="wrap-any">{item}</td>
                {dir === 'from' && <td className="mono muted col-at">{r.from}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Call graph
export interface GraphSettings {
  direction: GraphDirection;
  depth: number;
  maxNodes: number;
}
export const DEFAULT_GRAPH: GraphSettings = { direction: 'callees', depth: 2, maxNodes: 200 };
const DIRECTIONS: { id: GraphDirection; label: string }[] = [
  { id: 'callees', label: 'Calls (what it calls)' },
  { id: 'callers', label: 'Called by (what calls it)' },
  { id: 'both', label: 'Both' },
];

/** Default expansion: the root and the first level open, deeper levels closed. */
function initialOpen(root: CallTreeNode): Set<string> {
  const open = new Set<string>([root.id]);
  for (const c of root.children) if (c.children.length) open.add(c.id);
  return open;
}

interface Visible {
  node: CallTreeNode;
  level: number;
  parent: string | null;
  setsize: number;
  posinset: number;
}

function visibleItems(root: CallTreeNode, open: Set<string>): Visible[] {
  const out: Visible[] = [];
  const walk = (node: CallTreeNode, level: number, parent: string | null, setsize: number, posinset: number) => {
    out.push({ node, level, parent, setsize, posinset });
    if (open.has(node.id)) node.children.forEach((c, i) => walk(c, level + 1, node.id, node.children.length, i + 1));
  };
  walk(root, 1, null, 1, 1);
  return out;
}

export function CallGraphView({ caseId, moduleId, addr, nav, settings, onSettings }: { caseId: string; moduleId: string; addr: string; nav: FnNav; settings: GraphSettings; onSettings: (s: GraphSettings) => void }) {
  const api = useApi();
  const re = useMemo(() => reRefsApi(api), [api]);
  const { direction, depth, maxNodes } = settings;
  const res = useLoad<CallGraph>(`${moduleId}|${addr}|${direction}|${depth}|${maxNodes}`, () => re.callGraph(caseId, moduleId, addr, { direction, depth, maxNodes }));
  const g = res.data;
  const tree = useMemo(() => (g ? buildCallTree(g) : null), [g]);
  const set = (patch: Partial<GraphSettings>) => onSettings({ ...settings, ...patch });
  const nextCap = GRAPH_NODE_CAPS.find((c) => c > maxNodes);
  const frontier = useMemo(() => {
    let k = 0;
    const walk = (x: CallTreeNode) => {
      if (x.frontier) k++;
      x.children.forEach(walk);
    };
    if (tree) walk(tree);
    return k;
  }, [tree]);
  return (
    <div className="stack" data-testid="fn-graph">
      <div className="filters fn-graph-controls" role="group" aria-label="Call graph settings">
        <label className="field">
          <span className="small">Direction</span>
          <select value={direction} onChange={(e) => set({ direction: e.target.value as GraphDirection })} data-testid="fn-graph-direction">
            {DIRECTIONS.map((d) => (
              <option key={d.id} value={d.id}>
                {d.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="small">Depth</span>
          <select value={depth} onChange={(e) => set({ depth: Number(e.target.value) })} data-testid="fn-graph-depth">
            {[1, 2, 3, 4, 5].map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="small">At most</span>
          <select value={maxNodes} onChange={(e) => set({ maxNodes: Number(e.target.value) })} data-testid="fn-graph-cap">
            {GRAPH_NODE_CAPS.map((c) => (
              <option key={c} value={c}>
                {c} functions
              </option>
            ))}
          </select>
        </label>
      </div>
      {res.error ? (
        <ErrorCallout error={res.error} title="The call graph could not be built" onRetry={res.reload} />
      ) : !g || !tree ? (
        <Loading what={`call graph (depth ${depth})`} />
      ) : (
        <>
          <p className="small muted" aria-live="polite" data-testid="fn-graph-summary">
            {g.nodes.length <= 1
              ? direction === 'callers'
                ? 'Nothing in the analysed code calls this function.'
                : direction === 'callees'
                  ? 'This function calls no other analysed function.'
                  : 'This function neither calls nor is called by another analysed function.'
              : `${n(g.nodes.length)} functions and ${n(g.edges.length)} call${g.edges.length === 1 ? '' : 's'} within depth ${g.depth}.`}
          </p>
          {g.node_limit_hit && (
            <div className="callout warn small" role="status" data-testid="fn-graph-capped">
              <div>
                The graph stopped at {n(g.max_nodes)} functions, so some branches are cut. Lower the depth, or raise the limit.
              </div>
              {nextCap && (
                <div>
                  <button type="button" className="btn sm" onClick={() => set({ maxNodes: nextCap })} data-testid="fn-graph-raise">
                    Raise the limit to {nextCap}
                  </button>
                </div>
              )}
            </div>
          )}
          {g.nodes.length > 1 && <CallTree key={`${g.root}|${g.direction}|${g.depth}|${g.max_nodes}`} tree={tree} nav={nav} />}
          {frontier > 0 && depth < 5 && (
            <p className="xs muted" data-testid="fn-graph-frontier">
              {n(frontier)} function{frontier === 1 ? ' is' : 's are'} at depth {g.depth} and {frontier === 1 ? 'was' : 'were'} not followed further (marked “depth limit”). Raise the depth to see more.
            </p>
          )}
        </>
      )}
    </div>
  );
}

function CallTree({ tree, nav }: { tree: CallTreeNode; nav: FnNav }) {
  const [open, setOpen] = useState<Set<string>>(() => initialOpen(tree));
  const [focusState, setFocusId] = useState<string>(tree.id);
  const items = useMemo(() => visibleItems(tree, open), [tree, open]);
  // a collapsed branch can hide the focused item: fall back to the root
  const focusId = items.some((v) => v.node.id === focusState) ? focusState : tree.id;
  const refs = useRef(new Map<string, HTMLLIElement>());
  const moved = useRef(false);
  useEffect(() => {
    if (!moved.current) return;
    moved.current = false;
    refs.current.get(focusId)?.focus();
  }, [focusId, items]);

  const all = useMemo(() => {
    const ids: string[] = [];
    const walk = (x: CallTreeNode) => {
      if (x.children.length) ids.push(x.id);
      x.children.forEach(walk);
    };
    walk(tree);
    return ids;
  }, [tree]);
  const toggle = (id: string, to?: boolean) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (to ?? !next.has(id)) next.add(id);
      else next.delete(id);
      return next;
    });
  const go = (id: string) => {
    moved.current = true;
    setFocusId(id);
  };

  const onKey = (e: KeyboardEvent<HTMLUListElement>) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;          // Alt+Left/Right belong to the pane's Back/Forward
    const i = items.findIndex((v) => v.node.id === focusId);
    if (i < 0) return;
    const cur = items[i];
    const hasKids = cur.node.children.length > 0;
    const isOpen = open.has(cur.node.id);
    switch (e.key) {
      case 'ArrowDown':
        if (i + 1 < items.length) go(items[i + 1].node.id);
        break;
      case 'ArrowUp':
        if (i > 0) go(items[i - 1].node.id);
        break;
      case 'Home':
        go(items[0].node.id);
        break;
      case 'End':
        go(items[items.length - 1].node.id);
        break;
      case 'ArrowRight':
        if (hasKids && !isOpen) toggle(cur.node.id, true);
        else if (hasKids) go(cur.node.children[0].id);
        break;
      case 'ArrowLeft':
        if (hasKids && isOpen) toggle(cur.node.id, false);
        else if (cur.parent) go(cur.parent);
        break;
      case '*':
        setOpen((prev) => {
          const next = new Set(prev);
          const parent = cur.parent ? items.find((v) => v.node.id === cur.parent)?.node : null;
          for (const s of parent ? parent.children : [tree]) if (s.children.length) next.add(s.id);
          return next;
        });
        break;
      case 'Enter':
        if (cur.node.addr) nav.open(cur.node.addr);
        break;
      default:
        return;
    }
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <div className="stack">
      <div className="row fn-graph-bar">
        <button type="button" className="btn sm" onClick={() => setOpen(new Set(all))} data-testid="fn-graph-expand-all">
          Expand all
        </button>
        <button type="button" className="btn sm" onClick={() => setOpen(new Set([tree.id]))} data-testid="fn-graph-collapse-all">
          Collapse all
        </button>
      </div>
      <ul role="tree" aria-label={`Call graph of ${tree.name || tree.addr}`} aria-describedby="fn-graph-keys" className="fn-tree" onKeyDown={onKey} data-testid="fn-graph-tree">
        <TreeItem node={tree} level={1} setsize={1} posinset={1} open={open} focusId={focusId} refs={refs} nav={nav} onToggle={toggle} onFocusItem={setFocusId} root={tree.addr} />
      </ul>
      <p id="fn-graph-keys" className="xs muted">
        Arrow keys move through the tree; Right opens a branch and Left closes it; * opens all branches at the same level; Enter opens the function in this pane.
      </p>
    </div>
  );
}

function TreeItem({
  node,
  level,
  setsize,
  posinset,
  open,
  focusId,
  refs,
  nav,
  onToggle,
  onFocusItem,
  root,
}: {
  node: CallTreeNode;
  level: number;
  setsize: number;
  posinset: number;
  open: Set<string>;
  focusId: string;
  refs: MutableRefObject<Map<string, HTMLLIElement>>;
  nav: FnNav;
  onToggle: (id: string) => void;
  onFocusItem: (id: string) => void;
  root: string;
}) {
  const hasKids = node.children.length > 0;
  const isOpen = hasKids && open.has(node.id);
  return (
    <li
      role="treeitem"
      aria-level={level}
      aria-setsize={setsize}
      aria-posinset={posinset}
      aria-expanded={hasKids ? isOpen : undefined}
      aria-selected={node.id === focusId}
      tabIndex={node.id === focusId ? 0 : -1}
      ref={(el) => {
        if (el) refs.current.set(node.id, el);
        else refs.current.delete(node.id);
      }}
      onFocus={(e) => {
        if (e.target === e.currentTarget) onFocusItem(node.id);
      }}
      className="fn-tree-item"
      data-testid={`fn-tree-${node.id}`}
    >
      <div className="fn-tree-row">
        {hasKids ? (
          <button type="button" className="fn-tree-toggle" tabIndex={-1} aria-hidden="true" onClick={() => onToggle(node.id)}>
            {isOpen ? '▾' : '▸'}
          </button>
        ) : (
          <span className="fn-tree-toggle" aria-hidden="true">
            ·
          </span>
        )}
        {node.relation && level > 1 && <span className="xs muted fn-tree-rel">{node.relation === 'calls' ? 'calls' : 'called by'}</span>}
        <FnLink addr={node.addr} name={node.name} nav={nav} current={root} tabIndex={-1} />
        <span className="xs muted mono">{node.addr}</span>
        {node.repeat === 'cycle' && (
          <span className="chip warn xs" data-testid="fn-tree-cycle">
            cycle: already open above
          </span>
        )}
        {node.repeat === 'seen' && <span className="chip xs">listed earlier</span>}
        {node.frontier && <span className="chip xs">depth limit</span>}
      </div>
      {isOpen && (
        <ul role="group">
          {node.children.map((c, i) => (
            <TreeItem key={c.id} node={c} level={level + 1} setsize={node.children.length} posinset={i + 1} open={open} focusId={focusId} refs={refs} nav={nav} onToggle={onToggle} onFocusItem={onFocusItem} root={root} />
          ))}
        </ul>
      )}
    </li>
  );
}
