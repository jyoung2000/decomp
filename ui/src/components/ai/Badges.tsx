import { availabilityText, capabilityText, localityHint, localityLabel, ORIGIN_INFO, priceIsUnknown, priceLabel } from '../../lib/ai';
import type { LadderEntry, Locality, ModelAvailability, WorkOrigin } from '../../lib/types';
import { StatusChip } from '../StatusChip';

/** Local ("runs on this PC") or Cloud. Text always says which; colour is only a hint. */
export function LocalityBadge({ locality, full }: { locality: Locality | null | undefined; full?: boolean }) {
  return (
    <span className={`chip ${locality === 'local' ? 'info' : locality === 'cloud' ? 'outline' : 'muted'}`} title={localityHint(locality)} data-locality={locality ?? 'unknown'}>
      {localityLabel(locality)}
      {full && locality === 'local' ? ' · runs on this PC' : ''}
    </span>
  );
}

export function PriceLabel({ entry }: { entry: Pick<LadderEntry, 'price' | 'free' | 'locality'> }) {
  const unknown = priceIsUnknown(entry);
  return (
    <span className={unknown ? 'price unknown' : 'price'} data-testid="price">
      {priceLabel(entry)}
    </span>
  );
}

export function Availability({ a }: { a: ModelAvailability | null | undefined }) {
  const t = availabilityText(a);
  return <StatusChip status={t.state} label={t.text} />;
}

export function Capabilities({ entry }: { entry: Pick<LadderEntry, 'capabilities'> }) {
  return <span className="small muted">{capabilityText(entry.capabilities)}</span>;
}

export function OriginBadge({ origin }: { origin: WorkOrigin | null | undefined }) {
  if (!origin) return null;
  const o = ORIGIN_INFO[origin];
  if (!o) return null;
  return (
    <span className={`chip ${o.tone === 'ok' ? 'ok' : o.tone === 'retest' ? 'retest' : 'queue'}`} title={o.hint} data-origin={origin}>
      {o.label}
    </span>
  );
}
