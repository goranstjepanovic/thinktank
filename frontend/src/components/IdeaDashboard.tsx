import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { useIdeaStore } from '../store/ideaStore';
import type { IdeaSummary } from '../types';
import { IdeaForm } from './IdeaForm';
import { StatusBadge } from './StatusBadge';

const STATUS_SORT: Record<string, number> = {
  RUNNING: 0,
  PAUSED: 1,
  QUEUED: 2,
  SELECTED: 3,
  CONVERGED: 4,
  ABANDONED: 5,
};

const ACTIVE_STATUSES = new Set(['QUEUED', 'RUNNING', 'PAUSED']);

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  return `${Math.floor(diffH / 24)}d ago`;
}

function IdeaCard({ idea }: { idea: IdeaSummary }) {
  const isActive = ACTIVE_STATUSES.has(idea.status);

  return (
    <Link to={`/ideas/${idea.id}`} style={{ textDecoration: 'none' }}>
      <div
        className="card"
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          cursor: 'pointer',
          transition: 'border-color 0.15s',
          borderLeft: isActive ? '3px solid var(--blue)' : undefined,
        }}
        onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--accent)')}
        onMouseLeave={(e) => (e.currentTarget.style.borderColor = isActive ? 'var(--blue)' : 'var(--border)')}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ fontWeight: 600, fontSize: 15, marginBottom: 3 }}>{idea.name}</p>
          <p style={{ color: 'var(--text2)', fontSize: 12 }}>{timeAgo(idea.updated_at)}</p>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0, marginLeft: 16 }}>
          {/* Branch counts */}
          <div style={{ textAlign: 'right', fontSize: 12 }}>
            {idea.active_branch_count > 0 && (
              <span style={{ color: 'var(--blue)' }}>
                {idea.active_branch_count} branch{idea.active_branch_count !== 1 ? 'es' : ''} running
              </span>
            )}
            {idea.viable_branch_count > 0 && (
              <span style={{ color: 'var(--green)', marginLeft: idea.active_branch_count > 0 ? 8 : 0 }}>
                {idea.viable_branch_count} viable
              </span>
            )}
          </div>
          <StatusBadge status={idea.status} />
        </div>
      </div>
    </Link>
  );
}

export function IdeaDashboard() {
  const { ideas, setIdeas } = useIdeaStore();
  const [showForm, setShowForm] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = () => api.listIdeas().then(setIdeas).catch(() => {});

  useEffect(() => {
    api.listIdeas().then((data) => { setIdeas(data); setLoading(false); }).catch(() => setLoading(false));
  }, []);

  // Poll every 4 s while any idea is in-progress
  const hasActive = ideas.some((i) => ACTIVE_STATUSES.has(i.status));
  useEffect(() => {
    if (!hasActive) return;
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [hasActive]);

  const sorted = [...ideas].sort((a, b) => {
    const aOrd = STATUS_SORT[a.status] ?? 9;
    const bOrd = STATUS_SORT[b.status] ?? 9;
    if (aOrd !== bOrd) return aOrd - bOrd;
    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });

  const running = sorted.filter((i) => ACTIVE_STATUSES.has(i.status));
  const done = sorted.filter((i) => !ACTIVE_STATUSES.has(i.status));

  return (
    <div className="page">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700 }}>Ideas</h1>
          <p style={{ color: 'var(--text2)', marginTop: 2, fontSize: 13 }}>
            {ideas.length} idea{ideas.length !== 1 ? 's' : ''}
            {running.length > 0 && (
              <span style={{ color: 'var(--blue)', marginLeft: 8 }}>· {running.length} active</span>
            )}
          </p>
        </div>
        <button className="btn-primary" onClick={() => setShowForm(true)}>+ New Idea</button>
      </div>

      {loading && <p style={{ color: 'var(--text2)' }}>Loading…</p>}

      {!loading && ideas.length === 0 && (
        <div className="card" style={{ textAlign: 'center', padding: '56px 24px' }}>
          <p style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>No ideas yet</p>
          <p style={{ color: 'var(--text2)', fontSize: 13, marginBottom: 20, maxWidth: 400, margin: '0 auto 20px' }}>
            Submit an idea and the system will analyse it across multiple solution branches,
            producing a full documentation package for every viable approach.
          </p>
          <button className="btn-primary" onClick={() => setShowForm(true)}>+ New Idea</button>
        </div>
      )}

      {running.length > 0 && (
        <>
          <p style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
            Active
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 20 }}>
            {running.map((idea) => <IdeaCard key={idea.id} idea={idea} />)}
          </div>
        </>
      )}

      {done.length > 0 && (
        <>
          {running.length > 0 && (
            <p style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
              Past
            </p>
          )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {done.map((idea) => <IdeaCard key={idea.id} idea={idea} />)}
          </div>
        </>
      )}

      {showForm && <IdeaForm onClose={() => setShowForm(false)} />}
    </div>
  );
}
