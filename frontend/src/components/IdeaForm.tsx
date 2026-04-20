import { useState } from 'react';
import { api } from '../api/client';
import { useIdeaStore } from '../store/ideaStore';

interface FormValues { name: string; description: string; requirements: string; constraints: string; }
interface Props { onClose: () => void; onCreated?: (id: string) => void; initialValues?: Partial<FormValues>; parentIdeaId?: string; }

export function IdeaForm({ onClose, onCreated, initialValues, parentIdeaId }: Props) {
  const [form, setForm] = useState<FormValues>({
    name: initialValues?.name ?? '',
    description: initialValues?.description ?? '',
    requirements: initialValues?.requirements ?? '',
    constraints: initialValues?.constraints ?? '',
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const setIdeaDetail = useIdeaStore((s) => s.setIdeaDetail);
  const setIdeas = useIdeaStore((s) => s.setIdeas);
  const ideas = useIdeaStore((s) => s.ideas);

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim() || !form.description.trim()) return;
    setLoading(true);
    setError('');
    try {
      const idea = await api.createIdea({ ...form, ...(parentIdeaId ? { parent_idea_id: parentIdeaId } : {}) });
      setIdeaDetail(idea);
      setIdeas([
        {
          id: idea.id, name: idea.name, status: idea.status,
          active_branch_count: idea.branches.length,
          viable_branch_count: 0,
          phase: 1, phase_label: 'Queued',
          parent_idea_id: parentIdeaId ?? null,
          parent_idea_name: null,
          created_at: idea.created_at, updated_at: idea.updated_at,
        },
        ...ideas,
      ]);
      onClose();
      onCreated?.(idea.id);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
      <div className="card" style={{ width: 560, maxHeight: '90vh', overflowY: 'auto' }}>
        <h2 style={{ marginBottom: 20, fontSize: 16 }}>{initialValues ? 'Fork Idea' : 'New Idea'}</h2>
        <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label>Name</label>
            <input value={form.name} onChange={set('name')} placeholder="What's the idea?" required />
          </div>
          <div>
            <label>Description</label>
            <textarea value={form.description} onChange={set('description')} placeholder="Describe the idea in detail..." required style={{ minHeight: 100 }} />
          </div>
          <div>
            <label>Requirements</label>
            <textarea value={form.requirements} onChange={set('requirements')} placeholder="What must this achieve? List key requirements..." />
          </div>
          <div>
            <label>Constraints</label>
            <textarea value={form.constraints} onChange={set('constraints')} placeholder="What are the hard limits? (budget, tech stack, timeline, hardware...)" />
          </div>
          {error && <p style={{ color: 'var(--red)', fontSize: 13 }}>{error}</p>}
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary" disabled={loading}>
              {loading ? 'Submitting…' : 'Analyse Idea'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
