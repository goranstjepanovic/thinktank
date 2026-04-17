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
    ref.current.innerHTML = '';
    mermaid.render(`mermaid-${id}`, chart.trim()).then(({ svg }) => {
      if (ref.current) ref.current.innerHTML = svg;
    }).catch((err) => {
      if (ref.current) {
        ref.current.innerHTML = `<pre style="color:var(--red);font-size:11px">${String(err)}</pre>`;
      }
    });
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
