import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { useIdeaStore } from '../store/ideaStore';
import type { IdeaSummary } from '../types';
import { IdeaForm } from './IdeaForm';
import { StatusBadge } from './StatusBadge';

const ACTIVE_STATUSES = new Set(['QUEUED', 'RUNNING', 'PAUSED']);

const TERMINAL_PHASE_LABELS = new Set(['Phase 3 · Complete', 'Phase 3 · Failed']);

function isActive(idea: IdeaSummary): boolean {
  if (ACTIVE_STATUSES.has(idea.status)) return true;
  if (idea.status === 'SELECTED' && !TERMINAL_PHASE_LABELS.has(idea.phase_label)) return true;
  return false;
}


const PHASE_BADGE_STYLE: Record<string, { color: string; background: string }> = {
  'Phase 2 · Clarifying': { color: '#a78bfa', background: 'rgba(167,139,250,0.12)' },
  'Phase 2 · Ready':      { color: '#a78bfa', background: 'rgba(167,139,250,0.12)' },
  'Phase 2 · Implementing': { color: '#a78bfa', background: 'rgba(167,139,250,0.12)' },
  'Phase 2 · Complete':   { color: '#a78bfa', background: 'rgba(167,139,250,0.12)' },
  'Phase 3 · Planning':   { color: 'var(--blue)', background: 'rgba(96,165,250,0.12)' },
  'Phase 3 · Building':   { color: 'var(--blue)', background: 'rgba(96,165,250,0.12)' },
  'Phase 3 · Waiting':    { color: 'var(--orange, #fb923c)', background: 'rgba(251,146,60,0.12)' },
  'Phase 3 · Complete':   { color: 'var(--green)', background: 'rgba(34,197,94,0.12)' },
  'Phase 3 · Failed':     { color: 'var(--red)', background: 'rgba(239,68,68,0.12)' },
};

function buildForkTags(ideas: IdeaSummary[]): Map<string, string> {
  const ideaIds = new Set(ideas.map(i => i.id));
  const byParent = new Map<string, IdeaSummary[]>();
  for (const idea of ideas) {
    if (idea.parent_idea_id) {
      if (!byParent.has(idea.parent_idea_id)) byParent.set(idea.parent_idea_id, []);
      byParent.get(idea.parent_idea_id)!.push(idea);
    }
  }
  for (const children of byParent.values()) {
    children.sort((a, b) => a.created_at.localeCompare(b.created_at));
  }
  const tags = new Map<string, string>();
  // Seed with real roots + deleted parents (orphaned forks treated as virtual roots)
  const roots = ideas.filter(i => !i.parent_idea_id).map(i => i.id);
  const deletedParents = [...byParent.keys()].filter(pid => !ideaIds.has(pid));
  const queue = [...roots, ...deletedParents];
  while (queue.length > 0) {
    const parentId = queue.shift()!;
    const children = byParent.get(parentId) ?? [];
    children.forEach((child, i) => {
      const parentTag = tags.get(parentId);
      tags.set(child.id, parentTag ? `${parentTag}-${i + 1}` : `F-${i + 1}`);
      queue.push(child.id);
    });
  }
  return tags;
}

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  return `${Math.floor(diffH / 24)}d ago`;
}

function PhaseBadge({ label }: { label: string }) {
  const style = PHASE_BADGE_STYLE[label];
  if (!style) return <StatusBadge status={label} />;
  return (
    <span style={{
      fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 4,
      color: style.color, background: style.background,
      whiteSpace: 'nowrap',
    }}>
      {label}
    </span>
  );
}

function IdeaCard({ idea, forkTag }: { idea: IdeaSummary; forkTag?: string }) {
  const active = isActive(idea);
  const showPhaseBadge = idea.phase > 1 || idea.status === 'SELECTED';
  const borderColor = idea.phase === 3 && !TERMINAL_PHASE_LABELS.has(idea.phase_label)
    ? 'var(--blue)'
    : idea.phase === 2
      ? '#a78bfa'
      : active ? 'var(--blue)' : undefined;

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
          borderLeft: borderColor ? `3px solid ${borderColor}` : undefined,
        }}
        onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--accent)')}
        onMouseLeave={(e) => (e.currentTarget.style.borderColor = borderColor ?? 'var(--border)')}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ fontWeight: 600, fontSize: 15, marginBottom: 3 }}>{idea.name}</p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ color: 'var(--text2)', fontSize: 12 }}>{timeAgo(idea.updated_at)}</span>
            {idea.parent_idea_name && (
              <span style={{ color: 'var(--text2)', fontSize: 11, display: 'flex', alignItems: 'center', gap: 5 }}>
                {forkTag && (
                  <span style={{
                    fontSize: 10, fontWeight: 700, padding: '1px 5px', borderRadius: 3,
                    background: 'rgba(167,139,250,0.15)', color: '#a78bfa',
                    letterSpacing: '0.03em', flexShrink: 0,
                  }}>
                    {forkTag}
                  </span>
                )}
                ↳ fork of <span style={{ fontStyle: 'italic' }}>{idea.parent_idea_name}</span>
              </span>
            )}
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0, marginLeft: 16 }}>
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
          {showPhaseBadge
            ? <PhaseBadge label={idea.phase_label} />
            : idea.status === 'CONVERGED' && idea.viable_branch_count === 0
              ? <span className="badge" style={{ background: '#3a1a1a', color: 'var(--red)' }}>No Solution</span>
              : <StatusBadge status={idea.status} />
          }
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

  const hasActive = ideas.some(isActive);
  useEffect(() => {
    if (!hasActive) return;
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [hasActive]);

  const forkTags = buildForkTags(ideas);

  const sorted = [...ideas].sort(
    (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
  );

  const running = sorted.filter(isActive);
  const done = sorted.filter((i) => !isActive(i));

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
            {running.map((idea) => <IdeaCard key={idea.id} idea={idea} forkTag={forkTags.get(idea.id)} />)}
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
            {done.map((idea) => <IdeaCard key={idea.id} idea={idea} forkTag={forkTags.get(idea.id)} />)}
          </div>
        </>
      )}

      {showForm && <IdeaForm onClose={() => setShowForm(false)} />}
    </div>
  );
}
