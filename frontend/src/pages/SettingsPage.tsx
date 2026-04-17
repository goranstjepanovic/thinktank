import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';

export function SettingsPage() {
  const navigate = useNavigate();
  const [implementationsDir, setImplementationsDir] = useState('');
  const [savedDir, setSavedDir] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [moving, setMoving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [showMovePrompt, setShowMovePrompt] = useState(false);
  const pendingDirRef = useRef('');

  useEffect(() => {
    api.getSettings()
      .then(s => {
        setImplementationsDir(s.implementations_dir);
        setSavedDir(s.implementations_dir);
      })
      .catch(() => setError('Failed to load settings'))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    const trimmed = implementationsDir.trim();
    if (!trimmed) return;
    if (trimmed === savedDir) { setSuccess('No changes to save.'); return; }

    pendingDirRef.current = trimmed;
    setShowMovePrompt(true);
  };

  const doSave = async (move: boolean) => {
    setShowMovePrompt(false);
    setError(null);
    setSuccess(null);

    if (move) {
      setMoving(true);
      try {
        const result = await api.moveImplementations(pendingDirRef.current);
        setSavedDir(result.implementations_dir);
        setImplementationsDir(result.implementations_dir);
        setSuccess(`Moved ${result.moved_items} project(s) and updated ${result.updated_sessions} session(s).`);
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : 'Move failed');
      } finally {
        setMoving(false);
      }
    } else {
      setSaving(true);
      try {
        const result = await api.updateSettings({ implementations_dir: pendingDirRef.current });
        setSavedDir(result.implementations_dir);
        setImplementationsDir(result.implementations_dir);
        setSuccess('Settings saved. New projects will use the new location.');
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : 'Save failed');
      } finally {
        setSaving(false);
      }
    }
  };

  const busy = saving || moving;

  return (
    <div className="page" style={{ maxWidth: 640, margin: '0 auto', padding: '32px 24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 32 }}>
        <button className="btn-ghost" style={{ fontSize: 13 }} onClick={() => navigate('/')}>
          ← Back
        </button>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>Settings</h1>
      </div>

      {loading ? (
        <p style={{ color: 'var(--text2)' }}>Loading…</p>
      ) : (
        <div className="card" style={{ padding: '24px' }}>
          <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Implementations directory</h2>
          <p style={{ color: 'var(--text2)', fontSize: 13, marginBottom: 16 }}>
            Where generated project files are stored. Must be an absolute path.
            Changing this only affects new projects unless you choose to move existing ones.
          </p>

          <div style={{ display: 'flex', gap: 8, alignItems: 'stretch' }}>
            <input
              type="text"
              value={implementationsDir}
              onChange={e => { setImplementationsDir(e.target.value); setSuccess(null); setError(null); }}
              disabled={busy}
              style={{
                flex: 1,
                background: 'var(--bg)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                color: 'var(--text)',
                padding: '8px 12px',
                fontSize: 13,
                fontFamily: 'monospace',
              }}
            />
            <button
              className="btn-primary"
              onClick={handleSave}
              disabled={busy || !implementationsDir.trim() || implementationsDir.trim() === savedDir}
            >
              {saving ? 'Saving…' : moving ? 'Moving…' : 'Save'}
            </button>
          </div>

          {error && (
            <p style={{ color: 'var(--red)', fontSize: 13, marginTop: 10 }}>{error}</p>
          )}
          {success && (
            <p style={{ color: 'var(--green, #4caf50)', fontSize: 13, marginTop: 10 }}>{success}</p>
          )}

          <p style={{ color: 'var(--text2)', fontSize: 12, marginTop: 12 }}>
            Current saved location: <code style={{ fontSize: 12 }}>{savedDir}</code>
          </p>
        </div>
      )}

      {/* Move prompt */}
      {showMovePrompt && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }}>
          <div className="card" style={{ maxWidth: 440, width: '90%', padding: 24 }}>
            <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>Move existing projects?</h3>
            <p style={{ fontSize: 13, color: 'var(--text2)', marginBottom: 20 }}>
              Do you want to move all existing generated projects from
              <br /><code style={{ fontSize: 12 }}>{savedDir}</code>
              <br />to
              <br /><code style={{ fontSize: 12 }}>{pendingDirRef.current}</code>?
            </p>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button className="btn-ghost" style={{ fontSize: 13 }} onClick={() => doSave(false)}>
                No, just update setting
              </button>
              <button className="btn-primary" style={{ fontSize: 13 }} onClick={() => doSave(true)}>
                Yes, move projects
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
