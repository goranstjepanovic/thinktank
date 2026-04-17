import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { usePipelineEvents } from '../hooks/usePipelineEvents';
import { useIdeaStore } from '../store/ideaStore';
import type { BranchDetail, IdeaDetail, StageResult } from '../types';
import { STAGE_NAMES } from '../types';
import { StatusBadge } from './StatusBadge';

// ---------------------------------------------------------------------------
// Stage pipeline — full vertical list with expandable detail per stage
// ---------------------------------------------------------------------------

const STAGE_STATUS_LABELS: Record<string, string> = {
  completed: 'Completed',
  running: 'Running',
  failed: 'Failed',
  pending: 'Pending',
  skipped: 'Skipped',
};

function stageState(index: number, branch: BranchDetail): string {
  const result = branch.stage_results.find((s) => s.stage_index === index);
  if (result) {
    if (result.status === 'COMPLETED') return 'completed';
    if (result.status === 'FAILED') return 'failed';
    if (result.status === 'RUNNING') return 'running';
    if (result.status === 'SKIPPED') return 'skipped';
  }
  if (branch.status === 'VIABLE') return 'completed';
  if (index < branch.current_stage) return 'completed';
  if (index === branch.current_stage && branch.status === 'RUNNING') return 'running';
  return 'pending';
}

function formatJson(raw: string | null | undefined): string {
  if (!raw) return '';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function durationLabel(sr: StageResult): string {
  if (!sr.started_at || !sr.completed_at) return '';
  const ms = new Date(sr.completed_at).getTime() - new Date(sr.started_at).getTime();
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function StagePipeline({ branch }: { branch: BranchDetail }) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});

  const toggle = (i: number) =>
    setExpanded((prev) => ({ ...prev, [i]: !prev[i] }));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {STAGE_NAMES.map((name, i) => {
        const state = stageState(i, branch);
        const sr = branch.stage_results.find((s) => s.stage_index === i);
        const isOpen = expanded[i];
        const hasDetail = sr && (sr.output_json || sr.failure_reason);

        return (
          <div key={i} className="card" style={{ padding: '10px 14px' }}>
            {/* Stage row */}
            <div
              style={{
                display: 'flex', alignItems: 'center', gap: 10, cursor: hasDetail ? 'pointer' : 'default',
              }}
              onClick={() => hasDetail && toggle(i)}
            >
              {/* Index dot */}
              <div className={`stage-dot stage-dot-${state}`} style={{ flexShrink: 0 }}>
                {i}
              </div>

              {/* Name + status */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <span style={{ fontWeight: 500, fontSize: 13 }}>{name}</span>
                {sr?.failure_reason && (
                  <span style={{ fontSize: 12, color: 'var(--red)', marginLeft: 8 }}>
                    — {sr.failure_reason.slice(0, 120)}{sr.failure_reason.length > 120 ? '…' : ''}
                  </span>
                )}
              </div>

              {/* Meta */}
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexShrink: 0 }}>
                {sr && durationLabel(sr) && (
                  <span style={{ fontSize: 11, color: 'var(--text2)' }}>{durationLabel(sr)}</span>
                )}
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: state === 'completed' ? 'var(--green)'
                      : state === 'failed' ? 'var(--red)'
                      : state === 'running' ? 'var(--blue)'
                      : 'var(--text2)',
                  }}
                >
                  {STAGE_STATUS_LABELS[state] ?? state}
                </span>
                {hasDetail && (
                  <span style={{ fontSize: 11, color: 'var(--text2)', userSelect: 'none' }}>
                    {isOpen ? '▲' : '▼'}
                  </span>
                )}
              </div>
            </div>

            {/* Expanded output */}
            {isOpen && sr && (
              <div style={{ marginTop: 10, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
                {sr.failure_reason && (
                  <div style={{ marginBottom: 8 }}>
                    <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>FAILURE REASON</p>
                    <p style={{ fontSize: 12, color: 'var(--red)' }}>{sr.failure_reason}</p>
                  </div>
                )}
                {sr.output_json && (
                  <div>
                    <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>STAGE OUTPUT</p>
                    <pre style={{
                      background: 'var(--bg3)', borderRadius: 6, padding: '10px 12px',
                      fontSize: 11, overflowX: 'auto', maxHeight: 400, overflowY: 'auto',
                      color: 'var(--text)', lineHeight: 1.5,
                    }}>
                      {formatJson(sr.output_json)}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function SolutionDetail() {
  const { ideaId, branchId } = useParams<{ ideaId: string; branchId: string }>();
  const navigate = useNavigate();
  const { ideaDetails, setIdeaDetail } = useIdeaStore();

  const [branch, setBranch] = useState<BranchDetail | null>(null);
  const [idea, setIdea] = useState<IdeaDetail | null>(null);

  usePipelineEvents(ideaId);

  // Load initial data
  useEffect(() => {
    if (!ideaId || !branchId) return;

    api.getIdea(ideaId).then((d) => { setIdeaDetail(d); setIdea(d); });
    api.getBranch(ideaId, branchId).then(setBranch);
  }, [ideaId, branchId]);

  // Sync idea from store (WebSocket updates flow through store)
  const storeIdea = ideaId ? ideaDetails[ideaId] : null;
  useEffect(() => {
    if (storeIdea) setIdea(storeIdea);
  }, [storeIdea]);

  // Poll branch while running so stage results stay fresh
  useEffect(() => {
    if (!ideaId || !branchId || !branch) return;
    if (!['RUNNING', 'PAUSED'].includes(branch.status)) return;
    const t = setInterval(() => {
      api.getBranch(ideaId, branchId).then(setBranch);
    }, 4000);
    return () => clearInterval(t);
  }, [ideaId, branchId, branch?.status]);

  if (!branch || !idea) {
    return <div className="page"><p style={{ color: 'var(--text2)' }}>Loading…</p></div>;
  }

  const parentBranch = branch.parent_branch_id
    ? idea.branches.find((b) => b.id === branch.parent_branch_id)
    : null;

  const childBranches = idea.branches.filter(
    (b) => b.parent_branch_id === branch.id
  );

  return (
    <div className="page">
      {/* Breadcrumb */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text2)', marginBottom: 16 }}>
        <span
          style={{ cursor: 'pointer', color: 'var(--accent2)' }}
          onClick={() => navigate('/')}
        >
          Ideas
        </span>
        <span>/</span>
        <span
          style={{ cursor: 'pointer', color: 'var(--accent2)' }}
          onClick={() => navigate(`/ideas/${ideaId}`)}
        >
          {idea.name}
        </span>
        <span>/</span>
        <span style={{ color: 'var(--text)' }}>Branch {branch.branch_index}</span>
      </div>

      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20 }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
            <h1 style={{ fontSize: 20, fontWeight: 700 }}>Branch {branch.branch_index}</h1>
            <StatusBadge status={branch.status} />
          </div>
          {branch.approach_summary && (
            <p style={{ fontSize: 13, color: 'var(--text2)', maxWidth: 720 }}>{branch.approach_summary}</p>
          )}
          {parentBranch && (
            <p style={{ fontSize: 12, color: 'var(--text2)', marginTop: 6 }}>
              Spawned from{' '}
              <span
                style={{ color: 'var(--accent2)', cursor: 'pointer' }}
                onClick={() => navigate(`/ideas/${ideaId}/solutions/${parentBranch.id}`)}
              >
                Branch {parentBranch.branch_index}
              </span>
              {' '}after failure
            </p>
          )}
        </div>

        {/* Actions */}
        <div style={{ display: 'flex', gap: 8 }}>
          {branch.status === 'VIABLE' && (
            <button
              className="btn-primary"
              onClick={() => navigate(`/ideas/${ideaId}`, { state: { tab: 'documents', branchId } })}
            >
              View Documentation
            </button>
          )}
          <button className="btn-ghost" onClick={() => navigate(`/ideas/${ideaId}`)}>
            ← Back to Idea
          </button>
        </div>
      </div>

      {/* Failure summary card */}
      {branch.status === 'FAILED' && branch.failure_reason && (
        <div className="card" style={{ marginBottom: 20, borderColor: 'var(--red)', background: '#1e0e0e' }}>
          <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 6 }}>FAILED AT STAGE {branch.failure_stage ?? '?'}</p>
          <p style={{ fontSize: 13, color: 'var(--red)' }}>{branch.failure_reason}</p>
          {childBranches.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 6 }}>SPAWNED {childBranches.length} NEW BRANCH{childBranches.length > 1 ? 'ES' : ''}</p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {childBranches.map((cb) => (
                  <button
                    key={cb.id}
                    className="btn-ghost"
                    style={{ fontSize: 12 }}
                    onClick={() => navigate(`/ideas/${ideaId}/solutions/${cb.id}`)}
                  >
                    Branch {cb.branch_index} <StatusBadge status={cb.status} />
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Stage pipeline */}
      <h2 style={{ fontSize: 14, fontWeight: 600, color: 'var(--text2)', marginBottom: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        Pipeline — {branch.stage_results.length} / {STAGE_NAMES.length} stages recorded
      </h2>
      <StagePipeline branch={branch} />
    </div>
  );
}
