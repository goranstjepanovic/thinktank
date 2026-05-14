import mermaid from 'mermaid';
import { useEffect, useId, useRef } from 'react';

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

  useEffect(() => {
    if (!ref.current) return;
    let cancelled = false;
    ref.current.innerHTML = '';

    const renderId = `mermaid-${id}`;
    // Remove any leftover temp element mermaid may have left from a previous render
    document.getElementById(`d${renderId}`)?.remove();

    mermaid.render(renderId, chart.trim()).then(({ svg }) => {
      if (!cancelled && ref.current) ref.current.innerHTML = svg;
    }).catch((err) => {
      if (!cancelled && ref.current) {
        ref.current.innerHTML = `<pre style="color:var(--red);font-size:11px">${String(err)}</pre>`;
      }
    }).finally(() => {
      // Mermaid appends a temp div to document.body on error and doesn't always remove it
      document.getElementById(`d${renderId}`)?.remove();
    });

    return () => {
      cancelled = true;
      document.getElementById(`d${renderId}`)?.remove();
    };
  }, [chart, id]);

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
