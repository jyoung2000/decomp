import { useEffect, useId, useState, type KeyboardEvent } from 'react';
import { entryFromCatalog, entryFromManual, rungKey } from '../../lib/ai';
import { useApi, useResource } from '../../lib/store';
import type { Connection, LadderEntry, ModelCatalogItem } from '../../lib/types';
import { Dialog } from '../Dialog';
import { ErrorCallout } from '../ErrorCallout';
import { Availability, Capabilities, LocalityBadge, PriceLabel } from './Badges';

/**
 * Searchable model picker (combobox over GET /ai/models). Manual model-ID entry is always offered, because discovery
 * can be unavailable (provider not probed yet, offline, no model-listing endpoint).
 */
export function ModelPicker({ open, task, taskLabel, connections, existing, onPick, onClose }: { open: boolean; task: string; taskLabel: string; connections: Connection[]; existing: Set<string>; onPick: (e: LadderEntry) => void; onClose: () => void }) {
  const api = useApi();
  const uid = useId();
  const [q, setQ] = useState('');
  const [dq, setDq] = useState('');
  const [where, setWhere] = useState<'any' | 'local' | 'cloud'>('any');
  const [active, setActive] = useState(-1);
  const [mConn, setMConn] = useState('');
  const [mModel, setMModel] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDq(q.trim()), 150);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => {
    if (open) {
      setQ('');
      setDq('');
      setWhere('any');
      setActive(-1);
      setMModel('');
    }
  }, [open]);
  const models = useResource<ModelCatalogItem[]>(open ? () => api.aiModels(dq, task) : null, [api, open, dq, task]);
  const all = models.data ?? [];
  const shown = all.filter((m) => where === 'any' || m.locality === where);
  const listId = `${uid}-list`;
  const opt = (i: number) => `${uid}-opt-${i}`;

  const pick = (m: ModelCatalogItem) => {
    if (existing.has(rungKey(m))) return;
    onPick(entryFromCatalog(m));
    onClose();
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => Math.min(shown.length - 1, a + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === 'Enter' && active >= 0 && shown[active]) {
      e.preventDefault();
      pick(shown[active]);
    }
  };
  const manualConn = connections.find((c) => c.connection_id === mConn);
  const manualDup = !!manualConn && !!mModel.trim() && existing.has(rungKey({ connection_id: mConn, model: mModel.trim() }));
  const manualOk = !!manualConn && !!mModel.trim() && !manualDup;
  const manualWhy = !manualConn ? 'Choose a connection first.' : !mModel.trim() ? 'Type the model ID first.' : manualDup ? 'That model is already in this ladder.' : '';

  return (
    <Dialog open={open} title={`Add a model · ${taskLabel}`} onClose={onClose}>
      <div className="stack-lg" data-testid="model-picker">
        <div className="filters">
          <div className="field" style={{ flex: '2 1 240px', maxWidth: 'none' }}>
            <label htmlFor={`${uid}-q`}>Search models</label>
            <input
              id={`${uid}-q`}
              type="search"
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={active >= 0 ? opt(active) : undefined}
              aria-describedby={`${uid}-help`}
              autoComplete="off"
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setActive(-1);
              }}
              onKeyDown={onKey}
              placeholder="for example qwen, gpt, vision"
              data-autofocus
            />
          </div>
          <div className="field">
            <label htmlFor={`${uid}-where`}>Where it runs</label>
            <select id={`${uid}-where`} value={where} onChange={(e) => setWhere(e.target.value as 'any' | 'local' | 'cloud')}>
              <option value="any">Local and cloud</option>
              <option value="local">Local only (on this PC)</option>
              <option value="cloud">Cloud only</option>
            </select>
          </div>
        </div>
        <p className="small muted" id={`${uid}-help`}>
          Models come from your probed connections. Use the arrow keys to move through the list and Enter to add one.
        </p>
        {models.error ? <ErrorCallout tone="warn" error={models.error} title="Model discovery is not available" /> : null}
        <ul id={listId} role="listbox" aria-label="Matching models" className="picker-list">
          {shown.map((m, i) => {
            const dup = existing.has(rungKey(m));
            return (
              <li key={rungKey(m)} id={opt(i)} role="option" aria-selected={i === active} aria-disabled={dup || undefined} className={`picker-opt${i === active ? ' active' : ''}${dup ? ' dup' : ''}`} onClick={() => pick(m)} data-testid={`pick-${m.model}`}>
                <div className="row">
                  <strong className="mono wrap-any">{m.model}</strong>
                  <LocalityBadge locality={m.locality} />
                  <span className="small muted">{m.connection_label ?? m.provider}</span>
                  {dup && <span className="small muted">Already in this ladder</span>}
                </div>
                <div className="row small">
                  <Capabilities entry={m} />
                  <span aria-hidden="true">·</span>
                  <PriceLabel entry={m} />
                  <Availability a={m.availability} />
                </div>
              </li>
            );
          })}
        </ul>
        {!models.loading && !models.error && shown.length === 0 && (
          <p className="small muted" role="status">
            {all.length === 0 ? 'No models were found yet. Probe a connection on this page to discover its models, or enter a model ID below.' : 'No model matches this search and filter.'}
          </p>
        )}
        <fieldset>
          <legend>Enter a model ID yourself</legend>
          <p className="small muted" style={{ marginBottom: 8 }}>Use this when the model is not listed, for example before a connection was probed or when a provider cannot list its models. Capabilities and price stay unknown until they are probed.</p>
          <div className="field-row" style={{ alignItems: 'end' }}>
            <div className="field">
              <label htmlFor={`${uid}-mc`}>Connection</label>
              <select id={`${uid}-mc`} value={mConn} onChange={(e) => setMConn(e.target.value)}>
                <option value="">Choose a connection</option>
                {connections.map((c) => (
                  <option key={c.connection_id} value={c.connection_id}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor={`${uid}-mm`}>Model ID</label>
              <input id={`${uid}-mm`} type="text" value={mModel} onChange={(e) => setMModel(e.target.value)} spellCheck={false} placeholder="for example qwen2.5-coder:14b" />
            </div>
            <div>
              <button
                type="button"
                className="btn primary"
                disabled={!manualOk}
                aria-describedby={!manualOk ? `${uid}-mwhy` : undefined}
                onClick={() => {
                  if (!manualConn) return;
                  onPick(entryFromManual(manualConn, mModel.trim()));
                  onClose();
                }}
              >
                Add this model
              </button>
            </div>
          </div>
          {!manualOk && (
            <p className="small muted" id={`${uid}-mwhy`}>
              {manualWhy}
            </p>
          )}
        </fieldset>
      </div>
    </Dialog>
  );
}
