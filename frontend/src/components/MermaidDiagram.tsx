import mermaid from 'mermaid';
import { useEffect, useId, useRef, useState } from 'react';

mermaid.initialize({
  startOnLoad: false,
  theme: 'dark',
  darkMode: true,
  themeVariables: {
    background: '#0d1117',
    primaryColor: '#1a2a4a',
    primaryTextColor: '#c9d1d9',
    lineColor: '#4a5568',
    edgeLabelBackground: '#161b22',
    clusterBkg: '#161b22',
    titleColor: '#c9d1d9',
  },
  flowchart: { curve: 'basis', htmlLabels: true },
});

export function MermaidDiagram({ chart }: { chart: string }) {
  const id = useId().replace(/:/g, '');
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);

  useEffect(() => {
    if (!ref.current) return;
    let cancelled = false;
    setError(null);
    ref.current.innerHTML = '';

    const renderId = `mermaid-${id}`;
    document.getElementById(`d${renderId}`)?.remove();

    mermaid.render(renderId, chart.trim()).then(({ svg }) => {
      if (!cancelled && ref.current) {
        ref.current.innerHTML = svg;
        setError(null);
      }
    }).catch((err) => {
      if (!cancelled) setError(String(err));
    }).finally(() => {
      document.getElementById(`d${renderId}`)?.remove();
    });

    return () => {
      cancelled = true;
      document.getElementById(`d${renderId}`)?.remove();
    };
  }, [chart, id]);

  if (error) {
    return (
      <div style={{ border: '1px solid var(--border)', borderRadius: 6, margin: '12px 0', overflow: 'hidden' }}>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '6px 12px', background: '#1a1015', borderBottom: '1px solid var(--border)',
        }}>
          <span style={{ fontSize: 11, color: 'var(--red)' }}>Diagram render failed</span>
          <button
            className="btn-ghost"
            style={{ fontSize: 11, padding: '2px 8px' }}
            onClick={() => setShowSource((s) => !s)}
          >
            {showSource ? 'Hide source' : 'Show source'}
          </button>
        </div>
        {showSource && (
          <pre style={{
            margin: 0, padding: '10px 14px', fontSize: 11, color: 'var(--text2)',
            background: '#0d1117', overflowX: 'auto', whiteSpace: 'pre-wrap',
          }}>
            {chart.trim()}
          </pre>
        )}
      </div>
    );
  }

  return (
    <div
      ref={ref}
      style={{
        background: '#0d1117',
        borderRadius: 6,
        padding: '12px 16px',
        margin: '12px 0',
        overflowX: 'auto',
        textAlign: 'center',
      }}
    />
  );
}
