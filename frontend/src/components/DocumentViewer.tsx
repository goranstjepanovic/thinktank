import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../api/client';
import type { BranchSummary, DocumentMeta } from '../types';
import { MermaidDiagram } from './MermaidDiagram';

const DOC_LABELS: Record<string, string> = {
  EXECUTIVE_SUMMARY: 'Executive Summary',
  ARCHITECTURE_OVERVIEW: 'Architecture',
  COMPONENT_SPECS: 'Components',
  REQUIREMENTS_TRACEABILITY: 'Requirements',
  RISK_REGISTER: 'Risks',
  IMPLEMENTATION_ROADMAP: 'Roadmap',
  OPEN_QUESTIONS: 'Open Questions',
};

interface Props {
  ideaId: string;
  viableBranches: BranchSummary[];
  initialBranchId?: string;
}

export function DocumentViewer({ ideaId, viableBranches, initialBranchId }: Props) {
  const [selectedBranch, setSelectedBranch] = useState<string | null>(initialBranchId ?? null);
  const [docs, setDocs] = useState<DocumentMeta[]>([]);
  const [selectedDoc, setSelectedDoc] = useState<string | null>(null);
  const [content, setContent] = useState<string>('');
  const [loadingDoc, setLoadingDoc] = useState(false);

  const branchId = selectedBranch ?? viableBranches[0]?.id ?? null;

  useEffect(() => {
    if (!branchId) return;
    api.listDocuments(ideaId, branchId).then((data) => {
      setDocs(data);
      if (data.length > 0 && !selectedDoc) setSelectedDoc(data[0].doc_type);
    });
  }, [branchId]);

  useEffect(() => {
    if (!branchId || !selectedDoc) return;
    setLoadingDoc(true);
    api.getDocument(ideaId, branchId, selectedDoc)
      .then((d) => setContent(d.content))
      .catch(() => setContent('*Failed to load document.*'))
      .finally(() => setLoadingDoc(false));
  }, [branchId, selectedDoc]);

  if (viableBranches.length === 0) {
    return <p style={{ color: 'var(--text2)' }}>No viable branches yet. Documents are generated when a branch completes all stages.</p>;
  }

  return (
    <div>
      {/* Branch selector */}
      {viableBranches.length > 1 && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
          <span style={{ color: 'var(--text2)', fontSize: 12, alignSelf: 'center' }}>Solution:</span>
          {viableBranches.map((b) => (
            <button
              key={b.id}
              className={branchId === b.id ? 'btn-primary' : 'btn-ghost'}
              style={{ fontSize: 12, padding: '4px 10px' }}
              onClick={() => { setSelectedBranch(b.id); setSelectedDoc(null); setContent(''); }}
            >
              Branch {b.branch_index}
            </button>
          ))}
        </div>
      )}

      {/* Doc type tabs */}
      <div className="tabs" style={{ marginBottom: 0 }}>
        {docs.map((d) => (
          <button
            key={d.doc_type}
            className={`tab ${selectedDoc === d.doc_type ? 'active' : ''}`}
            onClick={() => setSelectedDoc(d.doc_type)}
          >
            {DOC_LABELS[d.doc_type] ?? d.doc_type}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="card" style={{ marginTop: 0, borderTopLeftRadius: 0, borderTopRightRadius: 0, minHeight: 300 }}>
        {loadingDoc ? (
          <p style={{ color: 'var(--text2)' }}>Loading…</p>
        ) : (
          <div className="markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                code({ className, children }) {
                  const lang = /language-(\w+)/.exec(className || '')?.[1];
                  const code = String(children).trim();
                  if (lang === 'mermaid') return <MermaidDiagram chart={code} />;
                  return <code className={className}>{children}</code>;
                },
              }}
            >
              {content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}
