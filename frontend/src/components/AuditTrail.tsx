import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { ModelCall } from '../types';

interface Props {
  ideaId: string;
}

// ---------------------------------------------------------------------------
// Filter tabs
// ---------------------------------------------------------------------------
type Filter = 'ALL' | 'STAGE' | 'SCRIPT_EXECUTION' | 'WEB_SEARCH' | 'FAILURE_ANALYSIS';

const FILTER_LABELS: Record<Filter, string> = {
  ALL: 'All',
  STAGE: 'Model Calls',
  SCRIPT_EXECUTION: 'Script Runs',
  WEB_SEARCH: 'Web Searches',
  FAILURE_ANALYSIS: 'Failure Analysis',
};

// ---------------------------------------------------------------------------
// Row label helpers
// ---------------------------------------------------------------------------
function callLabel(c: ModelCall): string {
  if (c.call_type === 'SCRIPT_EXECUTION') return 'Python Script';
  if (c.call_type === 'WEB_SEARCH') return 'Web Search';
  if (c.call_type === 'FAILURE_ANALYSIS') return 'Failure Analysis';
  return `${c.model_name}  ·  call #${c.call_index}`;
}

function callAccentColor(c: ModelCall): string {
  if (c.call_type === 'SCRIPT_EXECUTION') return 'var(--yellow)';
  if (c.call_type === 'WEB_SEARCH') return 'var(--green)';
  if (c.call_type === 'FAILURE_ANALYSIS') return 'var(--red)';
  return 'var(--blue)';
}

// ---------------------------------------------------------------------------
// Preview line shown in the collapsed row
// ---------------------------------------------------------------------------
function buildPreview(c: ModelCall): string {
  try {
    if (c.call_type === 'SCRIPT_EXECUTION') {
      const p = JSON.parse(c.prompt_json) as { script?: string };
      const firstLine = (p.script ?? '').split('\n')[0].trim();
      return firstLine.length > 0 ? firstLine : '(empty script)';
    }
    if (c.call_type === 'WEB_SEARCH') {
      const p = JSON.parse(c.prompt_json) as { query?: string };
      return p.query ?? '(no query)';
    }
    // For STAGE / FAILURE_ANALYSIS extract the assistant content from the raw response
    const r = JSON.parse(c.response_json);
    const content: string =
      r?.message?.content ??
      r?.choices?.[0]?.message?.content ??
      '';
    const snippet = content.replace(/\s+/g, ' ').trim().slice(0, 140);
    return snippet || '(no content)';
  } catch {
    return c.response_json.slice(0, 140);
  }
}

// ---------------------------------------------------------------------------
// Expanded body per call type
// ---------------------------------------------------------------------------
function ScriptBody({ c }: { c: ModelCall }) {
  let script = '';
  let stdout = '';
  let stderr = '';
  let exitCode: number | null = null;
  let timedOut = false;

  try {
    const p = JSON.parse(c.prompt_json) as { script?: string };
    script = p.script ?? '';
  } catch { /* ignore */ }

  try {
    const r = JSON.parse(c.response_json) as {
      stdout?: string; stderr?: string; exit_code?: number; timed_out?: boolean;
    };
    stdout = r.stdout ?? '';
    stderr = r.stderr ?? '';
    exitCode = r.exit_code ?? null;
    timedOut = r.timed_out ?? false;
  } catch { /* ignore */ }

  const preStyle: React.CSSProperties = {
    background: 'var(--bg3)', borderRadius: 6, padding: '10px 12px',
    fontSize: 11, overflowX: 'auto', maxHeight: 240, overflowY: 'auto',
    whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text)', lineHeight: 1.6,
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div>
        <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>SCRIPT</p>
        <pre style={preStyle}>{script || '(empty)'}</pre>
      </div>
      <div style={{ display: 'flex', gap: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>STDOUT</p>
          <pre style={{ ...preStyle, color: stdout ? 'var(--green)' : 'var(--text2)' }}>
            {stdout || '(empty)'}
          </pre>
        </div>
        {stderr && (
          <div style={{ flex: 1, minWidth: 0 }}>
            <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>STDERR</p>
            <pre style={{ ...preStyle, color: 'var(--red)' }}>{stderr}</pre>
          </div>
        )}
      </div>
      <div style={{ display: 'flex', gap: 16, fontSize: 11 }}>
        <span style={{ color: exitCode === 0 ? 'var(--green)' : 'var(--red)' }}>
          exit {exitCode ?? '?'}
        </span>
        {timedOut && <span style={{ color: 'var(--yellow)' }}>TIMED OUT</span>}
        {c.duration_ms != null && (
          <span style={{ color: 'var(--text2)' }}>{c.duration_ms}ms</span>
        )}
      </div>
    </div>
  );
}

function WebSearchBody({ c }: { c: ModelCall }) {
  let query = '';
  let results: Array<{ title: string; url: string; snippet: string }> = [];
  let error: string | undefined;

  try { query = (JSON.parse(c.prompt_json) as { query?: string }).query ?? ''; } catch { /* ignore */ }
  try {
    const r = JSON.parse(c.response_json) as { results?: typeof results; error?: string };
    results = r.results ?? [];
    error = r.error;
  } catch { /* ignore */ }

  const preStyle: React.CSSProperties = {
    background: 'var(--bg3)', borderRadius: 6, padding: '10px 12px',
    fontSize: 11, overflowX: 'auto', maxHeight: 240, overflowY: 'auto',
    whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.6,
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div>
        <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>QUERY</p>
        <pre style={{ ...preStyle, color: 'var(--text)' }}>{query}</pre>
      </div>
      {error && (
        <p style={{ fontSize: 12, color: 'var(--red)' }}>{error}</p>
      )}
      {results.length > 0 && (
        <div>
          <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 6 }}>RESULTS ({results.length})</p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {results.map((r, i) => (
              <div key={i} style={{ background: 'var(--bg3)', borderRadius: 6, padding: '8px 12px' }}>
                <p style={{ fontSize: 12, fontWeight: 600, marginBottom: 2 }}>{r.title}</p>
                <p style={{ fontSize: 11, color: 'var(--accent)', marginBottom: 4, wordBreak: 'break-all' }}>{r.url}</p>
                <p style={{ fontSize: 11, color: 'var(--text2)', lineHeight: 1.5 }}>{r.snippet}</p>
              </div>
            ))}
          </div>
        </div>
      )}
      {c.duration_ms != null && (
        <span style={{ fontSize: 11, color: 'var(--text2)' }}>{c.duration_ms}ms</span>
      )}
    </div>
  );
}

function ModelCallBody({ c }: { c: ModelCall }) {
  const preStyle: React.CSSProperties = {
    background: 'var(--bg3)', borderRadius: 6, padding: '10px 12px',
    fontSize: 11, overflowX: 'auto', maxHeight: 240, overflowY: 'auto',
    whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text)', lineHeight: 1.6,
  };

  let promptText = '';
  try {
    const msgs = JSON.parse(c.prompt_json) as Array<{ role: string; content: string; tool_calls?: unknown }>;
    promptText = msgs.map((m) => `[${m.role.toUpperCase()}]\n${m.content || (m.tool_calls ? '(tool call)' : '')}`).join('\n\n');
  } catch {
    promptText = c.prompt_json;
  }

  let responseText = '';
  try {
    // Extract just the content from the Ollama response envelope to keep it readable
    const r = JSON.parse(c.response_json);
    const content: string = r?.message?.content ?? '';
    if (content) {
      try {
        responseText = JSON.stringify(JSON.parse(content), null, 2);
      } catch {
        responseText = content;
      }
    } else {
      responseText = JSON.stringify(r, null, 2);
    }
  } catch {
    responseText = c.response_json;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div>
        <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>PROMPT</p>
        <pre style={preStyle}>{promptText}</pre>
      </div>
      <div>
        <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>RESPONSE</p>
        <pre style={preStyle}>{responseText}</pre>
      </div>
      <div style={{ display: 'flex', gap: 16, fontSize: 11, color: 'var(--text2)' }}>
        {c.tokens_prompt != null && <span>{c.tokens_prompt} prompt tok</span>}
        {c.tokens_completion != null && <span>{c.tokens_completion} completion tok</span>}
        {c.duration_ms != null && <span>{c.duration_ms}ms</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function AuditTrail({ ideaId }: Props) {
  const [calls, setCalls] = useState<ModelCall[]>([]);
  const [filter, setFilter] = useState<Filter>('ALL');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    api.listModelCalls(ideaId)
      .then(setCalls)
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [ideaId]);

  const toggle = (id: string) =>
    setExpanded((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const visible = filter === 'ALL' ? calls : calls.filter((c) => c.call_type === filter);

  // Counts per type for filter labels
  const counts: Record<Filter, number> = {
    ALL: calls.length,
    STAGE: calls.filter((c) => c.call_type === 'STAGE').length,
    SCRIPT_EXECUTION: calls.filter((c) => c.call_type === 'SCRIPT_EXECUTION').length,
    WEB_SEARCH: calls.filter((c) => c.call_type === 'WEB_SEARCH').length,
    FAILURE_ANALYSIS: calls.filter((c) => c.call_type === 'FAILURE_ANALYSIS').length,
  };

  return (
    <div>
      {/* Filter bar */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div className="tabs" style={{ margin: 0, border: 'none' }}>
          {(Object.keys(FILTER_LABELS) as Filter[]).map((f) => (
            <button
              key={f}
              className={`tab ${filter === f ? 'active' : ''}`}
              style={{ paddingBottom: 6 }}
              onClick={() => setFilter(f)}
            >
              {FILTER_LABELS[f]}
              <span style={{ marginLeft: 5, fontSize: 10, color: 'var(--text2)' }}>
                {counts[f]}
              </span>
            </button>
          ))}
        </div>
        <button className="btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }} onClick={load}>
          Refresh
        </button>
      </div>

      {loading && <p style={{ color: 'var(--text2)' }}>Loading…</p>}

      {!loading && visible.length === 0 && (
        <p style={{ color: 'var(--text2)' }}>
          {calls.length === 0 ? 'No calls recorded yet.' : `No ${FILTER_LABELS[filter].toLowerCase()} entries.`}
        </p>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {visible.map((c) => {
          const isOpen = expanded.has(c.id);
          const accent = callAccentColor(c);
          const preview = buildPreview(c);

          return (
            <div key={c.id} className="card" style={{ padding: '10px 14px' }}>
              {/* Collapsed row */}
              <div
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer', gap: 12 }}
                onClick={() => toggle(c.id)}
              >
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', minWidth: 0 }}>
                  {/* Call type pill */}
                  <span style={{
                    fontSize: 10, fontWeight: 600, padding: '2px 7px', borderRadius: 4,
                    background: 'var(--bg3)', color: accent, flexShrink: 0, letterSpacing: '0.04em',
                  }}>
                    {c.call_type === 'SCRIPT_EXECUTION' ? 'SCRIPT' : c.call_type.replace('_', ' ')}
                  </span>
                  {/* Label */}
                  <span style={{ fontSize: 12, color: 'var(--text2)', flexShrink: 0 }}>
                    {callLabel(c)}
                  </span>
                  {/* Preview */}
                  <span style={{ fontSize: 12, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {preview}
                  </span>
                </div>
                <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexShrink: 0 }}>
                  {c.duration_ms != null && (
                    <span style={{ fontSize: 11, color: 'var(--text2)' }}>{c.duration_ms}ms</span>
                  )}
                  <span style={{ fontSize: 11, color: 'var(--text2)' }}>{isOpen ? '▲' : '▼'}</span>
                </div>
              </div>

              {/* Expanded body */}
              {isOpen && (
                <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
                  {c.call_type === 'SCRIPT_EXECUTION' ? <ScriptBody c={c} /> :
                   c.call_type === 'WEB_SEARCH' ? <WebSearchBody c={c} /> :
                   <ModelCallBody c={c} />}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
