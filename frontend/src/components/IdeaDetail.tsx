import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { usePipelineEvents } from '../hooks/usePipelineEvents';
import { useIdeaStore } from '../store/ideaStore';
import type { BranchDetail, BranchSummary } from '../types';
import { STAGE_NAMES } from '../types';
import { AuditTrail } from './AuditTrail';
import { DocumentViewer } from './DocumentViewer';
import { IdeaForm } from './IdeaForm';
import { PhaseNav } from './PhaseNav';
import { StatusBadge } from './StatusBadge';

type Tab = 'branches' | 'documents' | 'audit';

// ---------------------------------------------------------------------------
// Branch tree
// ---------------------------------------------------------------------------

interface BranchNode extends BranchSummary {
  children: BranchNode[];
}

function buildTree(branches: BranchSummary[]): BranchNode[] {
  const nodes: BranchNode[] = branches.map((b) => ({ ...b, children: [] }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const roots: BranchNode[] = [];
  for (const node of nodes) {
    if (node.parent_branch_id && byId.has(node.parent_branch_id)) {
      byId.get(node.parent_branch_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

function BranchTreeNode({
  node,
  ideaId,
  selectedBranchId,
  selectingBranchId,
  selectionNotes,
  phase2BranchId,
  actionLoading,
  allBranches,
  isConverged,
  isIdeaSelected,
  depth = 0,
  setSelectingBranchId,
  setSelectionNotes,
  doSelect,
}: {
  node: BranchNode;
  ideaId: string;
  selectedBranchId: string | null;
  selectingBranchId: string | null;
  selectionNotes: string;
  phase2BranchId: string | null | undefined;
  actionLoading: string;
  allBranches: BranchSummary[];
  isConverged: boolean;
  isIdeaSelected: boolean;
  depth?: number;
  setSelectingBranchId: (id: string | null) => void;
  setSelectionNotes: (notes: string) => void;
  doSelect: (branchId: string) => Promise<void>;
}) {
  const navigate = useNavigate();
  const isSelected = node.id === selectedBranchId;
  const isSelectingThis = selectingBranchId === node.id;
  const canSelect = (isConverged || isIdeaSelected) && node.status === 'VIABLE' && !isSelected;
  const phase2WillReset = canSelect && phase2BranchId != null && phase2BranchId !== node.id;
  const phase2BranchIndex = phase2WillReset
    ? allBranches.find((x) => x.id === phase2BranchId)?.branch_index
    : null;
  const currentStageName = STAGE_NAMES[node.current_stage] ?? 'Unknown stage';

  const statusColor =
    node.status === 'VIABLE' ? 'var(--green)'
    : node.status === 'FAILED' ? 'var(--red)'
    : node.status === 'RUNNING' ? 'var(--blue)'
    : node.status === 'PAUSED' ? 'var(--yellow)'
    : 'var(--text2)';

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          paddingLeft: depth > 0 ? depth * 24 : 0,
          marginBottom: 6,
        }}
      >
        {/* Connector line for children */}
        {depth > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', marginRight: 0, marginTop: 2, flexShrink: 0 }}>
            <div style={{ width: 16, height: 14, borderLeft: '2px solid var(--border)', borderBottom: '2px solid var(--border)', borderBottomLeftRadius: 4, marginRight: 6 }} />
          </div>
        )}

        {/* Node card */}
        <div
          className="card"
          style={{
            flex: 1,
            padding: '10px 14px',
            cursor: 'pointer',
            borderColor: isSelected ? '#2d4a7a' : undefined,
            background: isSelected ? '#141820' : undefined,
            transition: 'border-color 0.15s',
          }}
          onClick={() => navigate(`/ideas/${ideaId}/solutions/${node.id}`)}
          onMouseEnter={(e) => { if (!isSelected) e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = isSelected ? '#2d4a7a' : 'var(--border)'; }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3 }}>
                {/* Status dot */}
                <div style={{
                  width: 8, height: 8, borderRadius: '50%', background: statusColor, flexShrink: 0,
                  boxShadow: node.status === 'RUNNING' ? `0 0 6px ${statusColor}` : undefined,
                }} />
                <span style={{ fontWeight: 600, fontSize: 13 }}>Branch {node.branch_index}</span>
                {isSelected && (
                  <span style={{ fontSize: 11, color: '#7eb8f7', background: '#1e2d4a', padding: '1px 6px', borderRadius: 4 }}>Selected</span>
                )}
              </div>
              {node.approach_summary && (
                <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: node.failure_reason ? 4 : 0 }}>
                  {node.approach_summary.slice(0, 140)}{node.approach_summary.length > 140 ? '…' : ''}
                </p>
              )}
              {node.failure_reason && (
                <p style={{ fontSize: 11, color: 'var(--red)' }}>
                  Failed: {node.failure_reason.slice(0, 100)}{node.failure_reason.length > 100 ? '…' : ''}
                </p>
              )}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0, marginLeft: 12 }}>
              {node.status === 'RUNNING' && (
                <span style={{ fontSize: 11, color: 'var(--text2)', whiteSpace: 'nowrap' }}>
                  Stage {node.current_stage}: {currentStageName}
                </span>
              )}
              <StatusBadge status={isSelected ? 'SELECTED' : node.status} />
              <span style={{ fontSize: 11, color: 'var(--text2)' }}>→</span>
            </div>
          </div>

          {(canSelect || isSelectingThis) && (
            <div
              style={{
                marginTop: 12,
                paddingTop: 12,
                borderTop: '1px solid var(--border)',
              }}
              onClick={(e) => e.stopPropagation()}
            >
              {canSelect && !isSelectingThis && (
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <button
                    className="btn-primary"
                    style={{ fontSize: 12 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectingBranchId(node.id);
                      setSelectionNotes('');
                    }}
                  >
                    Select Branch {node.branch_index}
                  </button>
                </div>
              )}

              {isSelectingThis && (
                <div>
                  <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
                    Confirm: Select Branch {node.branch_index}
                  </p>
                  {phase2WillReset && (
                    <div style={{
                      background: '#2a1500', border: '1px solid #7a4500',
                      borderRadius: 4, padding: '8px 10px', marginBottom: 10,
                    }}>
                      <p style={{ fontSize: 12, color: '#f0a050', fontWeight: 600, marginBottom: 2 }}>
                        âš  Phase 2 in progress for Branch {phase2BranchIndex}
                      </p>
                      <p style={{ fontSize: 12, color: '#c07030' }}>
                        Selecting a different solution will permanently delete the Phase 2 session.
                      </p>
                    </div>
                  )}
                  <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 6 }}>Notes (optional)</p>
                  <textarea
                    style={{
                      width: '100%', background: 'var(--bg)', border: '1px solid var(--border)',
                      borderRadius: 4, color: 'var(--text)', padding: '8px 10px',
                      fontSize: 13, resize: 'vertical', minHeight: 64, boxSizing: 'border-box',
                    }}
                    placeholder="Why this solution? Any tradeoffs or decisionsâ€¦"
                    value={selectionNotes}
                    onChange={(e) => setSelectionNotes(e.target.value)}
                  />
                  <div style={{ display: 'flex', gap: 8, marginTop: 10, justifyContent: 'flex-end' }}>
                    <button
                      className="btn-ghost"
                      onClick={(e) => {
                        e.stopPropagation();
                        setSelectingBranchId(null);
                        setSelectionNotes('');
                      }}
                    >
                      Cancel
                    </button>
                    <button
                      className={phase2WillReset ? 'btn-danger' : 'btn-primary'}
                      disabled={actionLoading === 'select-' + node.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        doSelect(node.id);
                      }}
                    >
                      {actionLoading === 'select-' + node.id
                        ? 'Selectingâ€¦'
                        : phase2WillReset
                        ? 'Switch & reset Phase 2'
                        : 'Confirm selection'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Children */}
      {node.children.map((child) => (
        <BranchTreeNode
          key={child.id}
          node={child}
          ideaId={ideaId}
          selectedBranchId={selectedBranchId}
          selectingBranchId={selectingBranchId}
          selectionNotes={selectionNotes}
          phase2BranchId={phase2BranchId}
          actionLoading={actionLoading}
          allBranches={allBranches}
          isConverged={isConverged}
          isIdeaSelected={isIdeaSelected}
          depth={depth + 1}
          setSelectingBranchId={setSelectingBranchId}
          setSelectionNotes={setSelectionNotes}
          doSelect={doSelect}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function IdeaDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { ideaDetails, setIdeaDetail, setIdeas, removeIdea } = useIdeaStore();
  const [, setBranchDetails] = useState<Record<string, BranchDetail>>({});
  const [actionLoading, setActionLoading] = useState('');
  const [showFork, setShowFork] = useState(false);
  const [selectingBranchId, setSelectingBranchId] = useState<string | null>(null);
  const [selectionNotes, setSelectionNotes] = useState('');
  const [phase2BranchId, setPhase2BranchId] = useState<string | null | undefined>(undefined);
  const [phase2Status, setPhase2Status] = useState<string | null>(null);
  const [phase3Status, setPhase3Status] = useState<string | null>(null);

  // Tab — read from navigation state on first render, else default to 'branches'
  const navState = location.state as { skipRedirect?: boolean; tab?: Tab; branchId?: string } | null;
  const skipRedirect = navState?.skipRedirect ?? false;
  const [tab, setTab] = useState<Tab>(navState?.tab ?? 'branches');
  // Pre-select branch from navigation state (for doc viewer)
  const [docBranchId] = useState<string | undefined>(navState?.branchId);
  const redirectedRef = useRef(false);

  usePipelineEvents(id);

  const idea = id ? ideaDetails[id] : null;

  useEffect(() => {
    if (!id) return;
    api.getIdea(id).then(setIdeaDetail);
    api.listBranches(id).then((branches) => {
      const map: Record<string, BranchDetail> = {};
      branches.forEach((b) => { map[b.id] = b; });
      setBranchDetails(map);
    });
  }, [id]);

  // Load phase session statuses; redirect to deepest active phase
  useEffect(() => {
    if (!id || !idea) return;
    if (!['CONVERGED', 'SELECTED'].includes(idea.status)) return;

    Promise.all([
      api.getPhase2(id).catch(() => null),
      api.getPhase3(id).catch(() => null),
    ]).then(([p2, p3]) => {
      setPhase2BranchId(p2?.branch_id ?? null);
      setPhase2Status(p2?.status ?? null);
      setPhase3Status(p3?.status ?? null);

      if (skipRedirect || redirectedRef.current) return;
      redirectedRef.current = true;

      if (p3 && p3.status !== 'FAILED') {
        navigate(`/ideas/${id}/phase3`, { replace: true });
      } else if (p2) {
        navigate(`/ideas/${id}/phase2`, { replace: true });
      }
    });
  }, [id, idea?.status]);

  // Refresh branch details every 5 s while running
  useEffect(() => {
    if (!id || !idea || !['RUNNING', 'PAUSED'].includes(idea.status)) return;
    const t = setInterval(() => {
      api.listBranches(id).then((branches) => {
        const map: Record<string, BranchDetail> = {};
        branches.forEach((b) => { map[b.id] = b; });
        setBranchDetails(map);
      });
    }, 5000);
    return () => clearInterval(t);
  }, [id, idea?.status]);

  const doAction = async (action: 'pause' | 'resume' | 'abandon') => {
    if (!id) return;
    setActionLoading(action);
    try {
      if (action === 'pause') await api.pauseIdea(id);
      else if (action === 'resume') await api.resumeIdea(id);
      else if (action === 'abandon') {
        if (!confirm('Abandon this idea? All active branches will be cancelled.')) return;
        await api.abandonIdea(id);
      }
      const updated = await api.getIdea(id);
      setIdeaDetail(updated);
    } finally {
      setActionLoading('');
    }
  };

  const doDelete = async () => {
    if (!id) return;
    if (!confirm('Delete this idea and all its data? This cannot be undone.')) return;
    setActionLoading('delete');
    try {
      await api.deleteIdea(id);
      removeIdea(id);
      api.listIdeas().then(setIdeas).catch(() => {});
      navigate('/');
    } finally {
      setActionLoading('');
    }
  };

  if (!idea) return <div className="page"><p style={{ color: 'var(--text2)' }}>Loading…</p></div>;

  const isActive = ['RUNNING', 'PAUSED'].includes(idea.status);
  const viableBranches = idea.branches.filter((b) => b.status === 'VIABLE');
  const isConverged = idea.status === 'CONVERGED';
  const isSelected = idea.status === 'SELECTED';

  const doSelect = async (branchId: string) => {
    if (!id) return;
    setActionLoading('select-' + branchId);
    try {
      const updated = await api.selectSolution(id, branchId, selectionNotes);
      setIdeaDetail(updated);
      setSelectingBranchId(null);
      setSelectionNotes('');
      if (phase2BranchId && phase2BranchId !== branchId) {
        setPhase2BranchId(null);
      } else {
        setPhase2BranchId(branchId);
      }
    } finally {
      setActionLoading('');
    }
  };

  const tree = buildTree(idea.branches);

  return (
    <div className="page">
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <button className="btn-ghost" style={{ fontSize: 12, marginBottom: 12 }} onClick={() => navigate('/')}>← Back</button>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 6 }}>{idea.name}</h1>
            <StatusBadge status={idea.status} />
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            {isSelected && (
              <button className="btn-primary" onClick={() => navigate(`/ideas/${id}/phase2`)}>
                Phase 2 →
              </button>
            )}
            <button className="btn-ghost" onClick={() => setShowFork(true)}>Fork</button>
            {idea.status === 'RUNNING' && (
              <button className="btn-ghost" disabled={!!actionLoading} onClick={() => doAction('pause')}>
                {actionLoading === 'pause' ? '…' : 'Pause'}
              </button>
            )}
            {idea.status === 'PAUSED' && (
              <button className="btn-primary" disabled={!!actionLoading} onClick={() => doAction('resume')}>
                {actionLoading === 'resume' ? '…' : 'Resume'}
              </button>
            )}
            {isActive && (
              <button className="btn-danger" disabled={!!actionLoading} onClick={() => doAction('abandon')}>
                {actionLoading === 'abandon' ? '…' : 'Abandon'}
              </button>
            )}
            <button className="btn-danger" disabled={!!actionLoading} onClick={doDelete} style={{ opacity: 0.8 }}>
              {actionLoading === 'delete' ? '…' : 'Delete'}
            </button>
          </div>
        </div>

        {/* Idea meta */}
        <div className="card" style={{ marginTop: 16, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <div>
            <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 4 }}>DESCRIPTION</p>
            <div className="idea-md" style={{ fontSize: 13 }}>
              <ReactMarkdown>{idea.description || ''}</ReactMarkdown>
            </div>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div>
              <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 2 }}>REQUIREMENTS</p>
              <div className="idea-md" style={{ fontSize: 13 }}>
                <ReactMarkdown>{idea.requirements || ''}</ReactMarkdown>
              </div>
            </div>
            <div>
              <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 2 }}>CONSTRAINTS</p>
              <div className="idea-md" style={{ fontSize: 13 }}>
                <ReactMarkdown>{idea.constraints || ''}</ReactMarkdown>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Phase progress nav */}
      {isSelected && (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '8px 0', marginBottom: 4,
        }}>
          <PhaseNav ideaId={id!} currentPhase={1} />
          <div style={{ display: 'flex', gap: 8, fontSize: 12, color: 'var(--text2)' }}>
            {phase2Status && (
              <span>Phase 2: <span style={{ color: 'var(--text)' }}>{phase2Status}</span></span>
            )}
            {phase3Status && (
              <span>· Phase 3: <span style={{ color: 'var(--text)' }}>{phase3Status}</span></span>
            )}
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="tabs">
        {(['branches', 'documents', 'audit'] as Tab[]).map((t) => (
          <button key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
            {t === 'branches' && `Branches (${idea.branches.length})`}
            {t === 'documents' && `Documents (${viableBranches.length})`}
            {t === 'audit' && 'Audit Trail'}
          </button>
        ))}
      </div>

      {/* Branches tab */}
      {tab === 'branches' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {/* Selection prompt banner */}
          {isConverged && viableBranches.length > 0 && (
            <div className="card" style={{ background: '#1a2a1a', borderColor: 'var(--green)', padding: '12px 16px' }}>
              <p style={{ fontSize: 13, color: 'var(--green)', fontWeight: 600, marginBottom: 4 }}>
                Analysis complete — {viableBranches.length} viable solution{viableBranches.length > 1 ? 's' : ''} found
              </p>
              <p style={{ fontSize: 12, color: 'var(--text2)' }}>
                Review the viable branches below and select one to proceed with.
              </p>
            </div>
          )}
          {isSelected && idea.selected_branch_id && (
            <div className="card" style={{ background: '#1a1e2a', borderColor: '#2d4a7a', padding: '12px 16px' }}>
              <p style={{ fontSize: 13, color: '#7eb8f7', fontWeight: 600, marginBottom: 4 }}>
                Solution selected — Branch {idea.branches.find((b) => b.id === idea.selected_branch_id)?.branch_index}
              </p>
              {idea.selection_notes && (
                <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 4 }}>{idea.selection_notes}</p>
              )}
              {idea.selected_at && (
                <p style={{ fontSize: 11, color: 'var(--text2)' }}>
                  Selected {new Date(idea.selected_at).toLocaleString()}
                </p>
              )}
            </div>
          )}

          {idea.branches.length === 0 && (
            <div className="card" style={{ textAlign: 'center', padding: '32px 24px' }}>
              <p style={{ color: 'var(--text2)', fontSize: 13 }}>
                {isActive ? 'Starting branches…' : 'No branches.'}
              </p>
            </div>
          )}

          {/* Tree view */}
          {tree.map((node) => (
            <BranchTreeNode
              key={node.id}
              node={node}
              ideaId={id!}
              selectedBranchId={idea.selected_branch_id}
              selectingBranchId={selectingBranchId}
              selectionNotes={selectionNotes}
              phase2BranchId={phase2BranchId}
              actionLoading={actionLoading}
              allBranches={idea.branches}
              isConverged={isConverged}
              isIdeaSelected={isSelected}
              setSelectingBranchId={setSelectingBranchId}
              setSelectionNotes={setSelectionNotes}
              doSelect={doSelect}
            />
          ))}

          {false && (idea?.branches ?? []).map((b) => {
            const isThisSelected = idea?.selected_branch_id === b.id;
            const isSelectingThis = selectingBranchId === b.id;
            const canSelect = (isConverged || isSelected) && b.status === 'VIABLE' && !isThisSelected;
            const phase2WillReset = canSelect && phase2BranchId != null && phase2BranchId !== b.id;
            const phase2BranchIndex = phase2WillReset
              ? (idea?.branches ?? []).find((x) => x.id === phase2BranchId)?.branch_index
              : null;

            if (!canSelect && !isSelectingThis) return null;

            return (
              <div key={b.id + '-actions'} style={{ paddingLeft: 0 }}>
                {/* Select button */}
                {canSelect && !isSelectingThis && (
                  <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: -4, marginBottom: 4 }}>
                    <button
                      className="btn-primary"
                      style={{ fontSize: 12 }}
                      onClick={() => { setSelectingBranchId(b.id); setSelectionNotes(''); }}
                    >
                      Select Branch {b.branch_index}
                    </button>
                  </div>
                )}

                {/* Inline selection confirmation */}
                {isSelectingThis && (
                  <div
                    className="card"
                    style={{ borderColor: '#2d4a7a', background: '#141820', padding: '14px 16px' }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
                      Confirm: Select Branch {b.branch_index}
                    </p>
                    {phase2WillReset && (
                      <div style={{
                        background: '#2a1500', border: '1px solid #7a4500',
                        borderRadius: 4, padding: '8px 10px', marginBottom: 10,
                      }}>
                        <p style={{ fontSize: 12, color: '#f0a050', fontWeight: 600, marginBottom: 2 }}>
                          ⚠ Phase 2 in progress for Branch {phase2BranchIndex}
                        </p>
                        <p style={{ fontSize: 12, color: '#c07030' }}>
                          Selecting a different solution will permanently delete the Phase 2 session.
                        </p>
                      </div>
                    )}
                    <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 6 }}>Notes (optional)</p>
                    <textarea
                      style={{
                        width: '100%', background: 'var(--bg)', border: '1px solid var(--border)',
                        borderRadius: 4, color: 'var(--text)', padding: '8px 10px',
                        fontSize: 13, resize: 'vertical', minHeight: 64, boxSizing: 'border-box',
                      }}
                      placeholder="Why this solution? Any tradeoffs or decisions…"
                      value={selectionNotes}
                      onChange={(e) => setSelectionNotes(e.target.value)}
                    />
                    <div style={{ display: 'flex', gap: 8, marginTop: 10, justifyContent: 'flex-end' }}>
                      <button className="btn-ghost" onClick={() => { setSelectingBranchId(null); setSelectionNotes(''); }}>
                        Cancel
                      </button>
                      <button
                        className={phase2WillReset ? 'btn-danger' : 'btn-primary'}
                        disabled={actionLoading === 'select-' + b.id}
                        onClick={() => doSelect(b.id)}
                      >
                        {actionLoading === 'select-' + b.id
                          ? 'Selecting…'
                          : phase2WillReset
                          ? 'Switch & reset Phase 2'
                          : 'Confirm selection'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Documents tab */}
      {tab === 'documents' && (
        <DocumentViewer ideaId={idea.id} viableBranches={viableBranches} initialBranchId={docBranchId} />
      )}

      {/* Audit tab */}
      {tab === 'audit' && <AuditTrail ideaId={idea.id} />}

      {showFork && (
        <IdeaForm
          onClose={() => setShowFork(false)}
          onCreated={(newId) => navigate(`/ideas/${newId}`)}
          parentIdeaId={idea.id}
          initialValues={{
            name: idea.name,
            description: idea.description,
            requirements: idea.requirements,
            constraints: idea.constraints,
          }}
        />
      )}
    </div>
  );
}
