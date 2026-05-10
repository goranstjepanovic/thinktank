import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api } from '../api/client';
import type { BackendStat, ModelStat, TelemetryCall, TimeBucket } from '../types';

// ---------------------------------------------------------------------------
// Types & helpers
// ---------------------------------------------------------------------------

const RANGE_OPTIONS = [
  { label: '1h',  hours: 1 },
  { label: '6h',  hours: 6 },
  { label: '24h', hours: 24 },
  { label: '7d',  hours: 168 },
  { label: '30d', hours: 720 },
];

const MODEL_COLORS = [
  '#60a5fa', '#34d399', '#fbbf24', '#e8823a', '#a78bfa',
  '#f87171', '#2dd4bf', '#fb923c', '#818cf8', '#4ade80',
];

function sinceFromHours(hours: number): string {
  return new Date(Date.now() - hours * 3600 * 1000).toISOString();
}

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)}m`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

function fmtBucket(iso: string, periodHours: number): string {
  const d = new Date(iso);
  if (periodHours <= 24) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) +
    (periodHours <= 168 ? ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '');
}

function truncate(s: string, n = 22): string {
  // Show the part after the last '/' (e.g. "mrthp/omnicoder2" → "omnicoder2")
  const base = s.includes('/') ? s.split('/').pop()! : s;
  return base.length > n ? base.slice(0, n - 1) + '…' : base;
}

const CHART_STYLE = {
  background: 'transparent',
  fontSize: 11,
};

const TOOLTIP_STYLE = {
  backgroundColor: 'var(--bg2)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  color: 'var(--text)',
  fontSize: 12,
};

const AXIS_STYLE = { fill: 'var(--text2)', fontSize: 11 };
const GRID_STROKE = 'var(--border)';

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatCard({ label, value, sub, color }: {
  label: string; value: string; sub?: string; color?: string;
}) {
  return (
    <div style={{
      background: 'var(--bg2)', border: '1px solid var(--border)',
      borderRadius: 8, padding: '14px 18px', flex: 1, minWidth: 130,
    }}>
      <div style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color || 'var(--text)' }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--text2)', marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function FilterSelect({ label, value, options, onChange }: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{ fontSize: 11, color: 'var(--text2)', whiteSpace: 'nowrap' }}>{label}</span>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        style={{
          background: 'var(--bg2)', border: '1px solid var(--border)',
          borderRadius: 6, color: 'var(--text)', fontSize: 12,
          padding: '4px 8px', cursor: 'pointer',
        }}
      >
        <option value="">All</option>
        {options.map(o => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text2)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
      {children}
    </div>
  );
}

function ChartCard({ title, children, minHeight = 260 }: {
  title: string; children: React.ReactNode; minHeight?: number;
}) {
  return (
    <div style={{
      background: 'var(--bg2)', border: '1px solid var(--border)',
      borderRadius: 8, padding: '16px 18px', minHeight,
    }}>
      <SectionTitle>{title}</SectionTitle>
      {children}
    </div>
  );
}

// Timeline: calls + success over time
function TimelineChart({ data, periodHours }: { data: TimeBucket[]; periodHours: number }) {
  const formatted = data.map(d => ({
    ...d,
    label: fmtBucket(d.bucket, periodHours),
    failed: d.calls - d.success,
  }));
  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={formatted} style={CHART_STYLE}>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
        <XAxis dataKey="label" tick={AXIS_STYLE} interval="preserveStartEnd" />
        <YAxis tick={AXIS_STYLE} allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Line type="monotone" dataKey="calls" name="Total" stroke="#60a5fa" dot={false} strokeWidth={2} />
        <Line type="monotone" dataKey="success" name="Success" stroke="#34d399" dot={false} strokeWidth={2} />
        <Line type="monotone" dataKey="failed" name="Failed" stroke="#f87171" dot={false} strokeWidth={1.5} strokeDasharray="4 3" />
      </LineChart>
    </ResponsiveContainer>
  );
}

// Horizontal bar chart for model success rate
function ModelSuccessChart({ data }: { data: ModelStat[] }) {
  const top = data.slice(0, 12).map(d => ({
    ...d,
    label: truncate(d.model),
    pct: Math.round(d.success_rate * 100),
  }));
  const barH = Math.max(200, top.length * 32);
  return (
    <ResponsiveContainer width="100%" height={barH}>
      <BarChart data={top} layout="vertical" style={CHART_STYLE} barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} horizontal={false} />
        <XAxis type="number" domain={[0, 100]} tick={AXIS_STYLE} tickFormatter={v => `${v}%`} />
        <YAxis type="category" dataKey="label" tick={AXIS_STYLE} width={130} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v, _name, props) => [`${v}%  (${props.payload.success}/${props.payload.calls})`, 'Success']}
        />
        <Bar dataKey="pct" name="Success %" radius={[0, 3, 3, 0]}>
          {top.map((entry, i) => (
            <Cell key={i} fill={entry.pct >= 90 ? '#34d399' : entry.pct >= 70 ? '#fbbf24' : '#f87171'} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// Horizontal bar chart for model avg duration
function ModelDurationChart({ data }: { data: ModelStat[] }) {
  const top = data.filter(d => d.avg_duration_ms != null).slice(0, 12).map((d, i) => ({
    ...d,
    label: truncate(d.model),
    color: MODEL_COLORS[i % MODEL_COLORS.length],
  }));
  const barH = Math.max(200, top.length * 32);
  return (
    <ResponsiveContainer width="100%" height={barH}>
      <BarChart data={top} layout="vertical" style={CHART_STYLE} barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} horizontal={false} />
        <XAxis type="number" tick={AXIS_STYLE} tickFormatter={v => fmtMs(v)} />
        <YAxis type="category" dataKey="label" tick={AXIS_STYLE} width={130} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v) => [fmtMs(v as number), 'Avg duration']}
        />
        <Bar dataKey="avg_duration_ms" name="Avg duration" radius={[0, 3, 3, 0]}>
          {top.map((entry, i) => (
            <Cell key={i} fill={entry.color} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// Backend comparison: grouped bars for calls + success rate + avg duration
function BackendCompareChart({ data }: { data: BackendStat[] }) {
  const rows = data.map(d => ({
    ...d,
    pct: Math.round(d.success_rate * 100),
  }));
  return (
    <ResponsiveContainer width="100%" height={160}>
      <BarChart data={rows} style={CHART_STYLE} barCategoryGap="30%" barGap={4}>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
        <XAxis dataKey="backend" tick={AXIS_STYLE} />
        <YAxis yAxisId="calls" tick={AXIS_STYLE} allowDecimals={false} />
        <YAxis yAxisId="pct" orientation="right" tick={AXIS_STYLE} domain={[0, 100]} tickFormatter={v => `${v}%`} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Bar yAxisId="calls" dataKey="calls" name="Calls" fill="#60a5fa" radius={[3, 3, 0, 0]} />
        <Bar yAxisId="pct" dataKey="pct" name="Success %" fill="#34d399" radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// Recent calls table
function CallsTable({ calls }: { calls: TelemetryCall[] }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            {['Time', 'Project', 'Stage', 'Model', 'Backend', 'Duration', 'Status'].map(h => (
              <th key={h} style={{ textAlign: 'left', padding: '6px 10px', color: 'var(--text2)', fontWeight: 600, whiteSpace: 'nowrap' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {calls.map((c, i) => (
            <tr key={i} style={{ borderBottom: '1px solid var(--border)', opacity: c.success ? 1 : 0.75 }}>
              <td style={{ padding: '6px 10px', color: 'var(--text2)', whiteSpace: 'nowrap' }}>
                {new Date(c.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
              </td>
              <td style={{ padding: '6px 10px', maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.project_name}>
                {c.project_name || '—'}
              </td>
              <td style={{ padding: '6px 10px', color: 'var(--text2)' }}>{c.stage}</td>
              <td style={{ padding: '6px 10px', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.model}>
                {c.model}
                {c.is_fallback && (
                  <span style={{ marginLeft: 6, fontSize: 10, color: 'var(--yellow)', background: 'rgba(251,191,36,0.12)', padding: '1px 5px', borderRadius: 3 }}>
                    fallback
                  </span>
                )}
              </td>
              <td style={{ padding: '6px 10px', color: 'var(--text2)' }}>{c.backend}</td>
              <td style={{ padding: '6px 10px', color: 'var(--text2)', fontVariantNumeric: 'tabular-nums' }}>{fmtMs(c.duration_ms)}</td>
              <td style={{ padding: '6px 10px' }}>
                {c.success ? (
                  <span style={{ color: 'var(--green)', fontWeight: 600 }}>✓</span>
                ) : (
                  <span style={{ color: 'var(--red)', fontWeight: 600 }} title={c.error || ''}>✗</span>
                )}
              </td>
            </tr>
          ))}
          {calls.length === 0 && (
            <tr>
              <td colSpan={7} style={{ padding: '24px 10px', textAlign: 'center', color: 'var(--text2)' }}>
                No calls in this time range
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main dashboard
// ---------------------------------------------------------------------------

export function OpsDashboard() {
  const [rangeHours, setRangeHours] = useState(168); // 7d default
  const [filterModel, setFilterModel] = useState('');
  const [filterBackend, setFilterBackend] = useState('');
  const [filterProject, setFilterProject] = useState('');
  const [filterStage, setFilterStage] = useState('');
  const [showCalls, setShowCalls] = useState(false);

  const since = useMemo(() => sinceFromHours(rangeHours), [rangeHours]);

  const summaryQ = useQuery({
    queryKey: ['telemetry-summary', since, filterModel, filterBackend, filterProject, filterStage],
    queryFn: () => api.getTelemetrySummary({
      since,
      model: filterModel || undefined,
      project_id: filterProject || undefined,
      backend: filterBackend || undefined,
      stage: filterStage || undefined,
    }),
    refetchInterval: 30_000,
  });

  const callsQ = useQuery({
    queryKey: ['telemetry-calls', since, filterModel, filterBackend, filterProject, filterStage],
    queryFn: () => api.getTelemetryCalls({
      since,
      model: filterModel || undefined,
      project_id: filterProject || undefined,
      backend: filterBackend || undefined,
      stage: filterStage || undefined,
      limit: 100,
    }),
    enabled: showCalls,
  });

  const data = summaryQ.data;

  // Aggregate stats for cards
  const totalCalls = data?.total_calls ?? 0;
  const successRate = totalCalls > 0
    ? Math.round(data!.by_model.reduce((sum, m) => sum + m.success, 0) / totalCalls * 100)
    : 0;
  const avgDuration = (() => {
    const all = data?.by_model.filter(m => m.avg_duration_ms != null) ?? [];
    if (!all.length) return null;
    return Math.round(all.reduce((sum, m) => sum + (m.avg_duration_ms ?? 0) * m.calls, 0) /
      all.reduce((sum, m) => sum + m.calls, 0));
  })();
  const fallbackRate = totalCalls > 0
    ? Math.round(data!.by_model.reduce((sum, m) => sum + m.fallbacks, 0) / totalCalls * 100)
    : 0;

  const availableModels = (data?.available_models ?? []).map(m => ({ value: m, label: m }));
  const availableBackends = (data?.available_backends ?? []).map(b => ({ value: b, label: b }));
  const availableProjects = (data?.available_projects ?? []).map(p => ({ value: p.id, label: p.name || p.id }));
  const availableStages = (data?.available_stages ?? []).map(s => ({ value: s, label: s }));

  const resetFilters = () => {
    setFilterModel('');
    setFilterBackend('');
    setFilterProject('');
    setFilterStage('');
  };
  const hasFilters = filterModel || filterBackend || filterProject || filterStage;

  return (
    <div style={{ padding: '24px 28px' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>Operations</h1>
          <div style={{ fontSize: 12, color: 'var(--text2)', marginTop: 2 }}>Model usage &amp; performance telemetry</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {RANGE_OPTIONS.map(opt => (
            <button
              key={opt.hours}
              onClick={() => setRangeHours(opt.hours)}
              className={rangeHours === opt.hours ? 'btn-primary' : 'btn-ghost'}
              style={{ padding: '4px 12px', fontSize: 12 }}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Filters */}
      <div style={{
        display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 12,
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 8, padding: '10px 14px', marginBottom: 20,
      }}>
        <FilterSelect label="Backend" value={filterBackend} options={availableBackends} onChange={setFilterBackend} />
        <FilterSelect label="Model" value={filterModel} options={availableModels} onChange={setFilterModel} />
        <FilterSelect label="Project" value={filterProject} options={availableProjects} onChange={setFilterProject} />
        <FilterSelect label="Stage" value={filterStage} options={availableStages} onChange={setFilterStage} />
        {hasFilters && (
          <button className="btn-ghost" style={{ fontSize: 11, padding: '3px 10px' }} onClick={resetFilters}>
            Clear filters
          </button>
        )}
        {summaryQ.isFetching && (
          <span style={{ fontSize: 11, color: 'var(--text2)', marginLeft: 'auto' }}>Refreshing…</span>
        )}
        {summaryQ.isError && (
          <span style={{ fontSize: 11, color: 'var(--red)', marginLeft: 'auto' }}>
            {(summaryQ.error as Error).message.includes('404') ? 'No telemetry data yet — run a project first.' : 'Failed to load telemetry'}
          </span>
        )}
      </div>

      {/* Stat cards */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
        <StatCard label="Total Calls" value={totalCalls.toLocaleString()} sub={`last ${RANGE_OPTIONS.find(o => o.hours === rangeHours)?.label}`} />
        <StatCard label="Success Rate" value={totalCalls ? `${successRate}%` : '—'} color={successRate >= 90 ? 'var(--green)' : successRate >= 70 ? 'var(--yellow)' : 'var(--red)'} />
        <StatCard label="Avg Duration" value={fmtMs(avgDuration)} sub="weighted by calls" />
        <StatCard label="Fallback Rate" value={totalCalls ? `${fallbackRate}%` : '—'} color={fallbackRate > 10 ? 'var(--red)' : fallbackRate > 3 ? 'var(--yellow)' : 'var(--green)'} sub="calls routed to fallback" />
        <StatCard label="Backends" value={String(data?.by_backend.length ?? 0)} sub={data?.by_backend.map(b => b.backend).join(', ') || '—'} />
      </div>

      {/* Timeline */}
      {data && data.over_time.some(b => b.calls > 0) && (
        <div style={{ marginBottom: 20 }}>
          <ChartCard title="Calls Over Time" minHeight={240}>
            <TimelineChart data={data.over_time} periodHours={data.period_hours} />
          </ChartCard>
        </div>
      )}

      {/* Model charts — two side by side */}
      {data && data.by_model.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }}>
          <ChartCard title="Model Success Rate" minHeight={280}>
            <ModelSuccessChart data={data.by_model} />
          </ChartCard>
          <ChartCard title="Model Avg Duration" minHeight={280}>
            <ModelDurationChart data={data.by_model} />
          </ChartCard>
        </div>
      )}

      {/* Backend comparison */}
      {data && data.by_backend.length > 1 && (
        <div style={{ marginBottom: 20 }}>
          <ChartCard title={`Backend Comparison  ·  use the Backend filter above to focus on one`} minHeight={200}>
            <BackendCompareChart data={data.by_backend} />
          </ChartCard>
        </div>
      )}

      {/* Per-model detail table */}
      {data && data.by_model.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          <ChartCard title="Model Detail" minHeight={0}>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border)' }}>
                    {['Model', 'Backend', 'Calls', 'Success', 'Fallbacks', 'Rate', 'Avg', 'p95'].map(h => (
                      <th key={h} style={{ textAlign: 'left', padding: '6px 10px', color: 'var(--text2)', fontWeight: 600, whiteSpace: 'nowrap' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.by_model.map((m, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td style={{ padding: '7px 10px', fontFamily: 'monospace', fontSize: 12 }} title={m.model}>{m.model}</td>
                      <td style={{ padding: '7px 10px', color: 'var(--text2)' }}>{m.backend}</td>
                      <td style={{ padding: '7px 10px', fontVariantNumeric: 'tabular-nums' }}>{m.calls}</td>
                      <td style={{ padding: '7px 10px', color: 'var(--green)', fontVariantNumeric: 'tabular-nums' }}>{m.success}</td>
                      <td style={{ padding: '7px 10px', color: m.fallbacks > 0 ? 'var(--yellow)' : 'var(--text2)', fontVariantNumeric: 'tabular-nums' }}>{m.fallbacks}</td>
                      <td style={{ padding: '7px 10px', color: m.success_rate >= 0.9 ? 'var(--green)' : m.success_rate >= 0.7 ? 'var(--yellow)' : 'var(--red)' }}>
                        {Math.round(m.success_rate * 100)}%
                      </td>
                      <td style={{ padding: '7px 10px', color: 'var(--text2)', fontVariantNumeric: 'tabular-nums' }}>{fmtMs(m.avg_duration_ms)}</td>
                      <td style={{ padding: '7px 10px', color: 'var(--text2)', fontVariantNumeric: 'tabular-nums' }}>{fmtMs(m.p95_duration_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </ChartCard>
        </div>
      )}

      {/* Recent calls */}
      <div>
        <div
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', cursor: 'pointer', marginBottom: 8 }}
          onClick={() => setShowCalls(v => !v)}
        >
          <SectionTitle>Recent Calls {callsQ.data ? `(${callsQ.data.total})` : ''}</SectionTitle>
          <span style={{ fontSize: 11, color: 'var(--text2)' }}>{showCalls ? '▲ hide' : '▼ show'}</span>
        </div>
        {showCalls && (
          <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
            {callsQ.isLoading ? (
              <div style={{ padding: 24, color: 'var(--text2)', textAlign: 'center' }}>Loading…</div>
            ) : (
              <CallsTable calls={callsQ.data?.calls ?? []} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
