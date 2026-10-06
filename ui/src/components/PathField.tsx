import { useId } from 'react';
import { hasTauri, pickFolder } from '../lib/tauri';
import { useToast } from './Toasts';

export function PathField({ label, value, onChange, hint, error, name, required }: { label: string; value: string; onChange: (v: string) => void; hint?: string; error?: string | null; name: string; required?: boolean }) {
  const id = useId();
  const toast = useToast();
  const native = hasTauri();
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  return (
    <div className="field">
      <label htmlFor={id}>
        {label}
        {required && <span aria-hidden="true"> *</span>}
      </label>
      <div className="input-with-btn">
        <input
          id={id}
          name={name}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={native ? 'Choose a folder…' : 'C:\\path\\to\\folder or /path/to/folder'}
          spellCheck={false}
          autoComplete="off"
          aria-invalid={!!error}
          aria-describedby={[hintId, error ? errId : ''].filter(Boolean).join(' ')}
          required={required}
        />
        {native && (
          <button
            type="button"
            className="btn"
            data-tooltip="Open the system folder picker"
            onClick={async () => {
              try {
                const p = await pickFolder(label, value || undefined);
                if (p) onChange(p);
              } catch (e) {
                toast.error('Folder picker failed', e);
              }
            }}
          >
            Browse…
          </button>
        )}
      </div>
      <span className="hint" id={hintId}>
        {hint}
        {!native && ' Type the full path; the native folder picker is available in the desktop app.'}
      </span>
      {error && (
        <span className="field-error" id={errId}>
          {error}
        </span>
      )}
    </div>
  );
}
