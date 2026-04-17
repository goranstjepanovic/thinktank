import React, { useEffect, useRef, useState } from 'react';
import {
  VscFolder, VscFolderOpened,
  VscFile, VscCode, VscJson, VscMarkdown, VscSymbolParameter, VscDatabase,
} from 'react-icons/vsc';
import {
  SiPython, SiTypescript, SiJavascript, SiHtml5, SiCss,
  SiRust, SiGo, SiDocker, SiYaml,
} from 'react-icons/si';
import ReactMarkdown from 'react-markdown';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import type { IdeaDetail, Phase3ActivityEvent, Phase3ChatMessage, Phase3FileEntry, Phase3Session, PipelineEvent } from '../types';
import { PhaseNav } from './PhaseNav';

const WS_BASE = 'ws://localhost:8000';

// ---------------------------------------------------------------------------
// Activity log entry types
// ---------------------------------------------------------------------------

type ActivityEntry =
  | { kind: 'thinking'; id: number }
  | { kind: 'plan_ready'; id: number; fileCount: number; message: string }
  | { kind: 'writing'; id: number; filePath: string; fileIndex: number; totalFiles: number }
  | { kind: 'file'; id: number; path: string; sizeBytes: number; success: boolean }
  | { kind: 'shell'; id: number; command: string; exitCode: number; stdout: string; stderr: string; timedOut: boolean; durationMs: number }
  | { kind: 'error'; id: number; message: string }
  | { kind: 'complete'; id: number; summary: string; outputDir: string }
  | { kind: 'user_msg'; id: number; messageId: string; content: string }
  | { kind: 'assistant_msg'; id: number; messageId: string; content: string };

let _entryId = 0;
const nextId = () => ++_entryId;

// ---------------------------------------------------------------------------
// Activity entry renderers
// ---------------------------------------------------------------------------

function ThinkingEntry() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 0', color: 'var(--text2)' }}>
      <div style={{ display: 'flex', gap: 3 }}>
        <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
      </div>
      <span style={{ fontSize: 12 }}>Thinking…</span>
    </div>
  );
}

function PlanReadyEntry({ fileCount, message }: { fileCount: number; message: string }) {
  return (
    <div>
      {message && (
        <div style={{ margin: '10px 0 6px' }}>
          <div style={{
            maxWidth: '88%',
            background: 'var(--bg3)',
            border: '1px solid var(--border)',
            borderRadius: '2px 12px 12px 12px',
            padding: '10px 14px',
            fontSize: 13,
            lineHeight: 1.5,
            color: 'var(--text)',
            wordBreak: 'break-word',
          }}>
            <div className="markdown" style={{ fontSize: 13 }}>
              <ReactMarkdown>{message}</ReactMarkdown>
            </div>
          </div>
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0 6px', fontSize: 12 }}>
        <span style={{ color: 'var(--blue)', fontWeight: 700 }}>📋</span>
        <span style={{ color: 'var(--text2)' }}>{fileCount} file{fileCount !== 1 ? 's' : ''} to generate</span>
      </div>
    </div>
  );
}

function WritingEntry({ filePath, fileIndex, totalFiles }: { filePath: string; fileIndex: number; totalFiles: number }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', color: 'var(--text2)', fontSize: 12 }}>
      <div style={{ display: 'flex', gap: 3 }}>
        <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
      </div>
      <code style={{ color: 'var(--text2)', fontFamily: 'monospace' }}>{filePath}</code>
      <span style={{ marginLeft: 'auto', flexShrink: 0 }}>{fileIndex + 1}/{totalFiles}</span>
    </div>
  );
}

function FileEntry({ path, sizeBytes, success }: { path: string; sizeBytes: number; success: boolean }) {
  const kb = sizeBytes > 0 ? `${(sizeBytes / 1024).toFixed(1)} KB` : '';
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '5px 0', fontSize: 12 }}>
      <span style={{ color: success ? 'var(--green)' : 'var(--red)', fontWeight: 700, flexShrink: 0 }}>
        {success ? '✓' : '✗'}
      </span>
      <code style={{ color: 'var(--blue)', fontFamily: 'monospace', flex: 1, wordBreak: 'break-all' }}>{path}</code>
      {kb && <span style={{ color: 'var(--text2)', flexShrink: 0 }}>{kb}</span>}
    </div>
  );
}

function ShellEntry({ command, exitCode, stdout, stderr, timedOut, durationMs }: {
  command: string; exitCode: number; stdout: string; stderr: string; timedOut: boolean; durationMs: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const ok = exitCode === 0 && !timedOut;
  const hasOutput = stdout.trim() || stderr.trim();
  const duration = durationMs >= 1000 ? `${(durationMs / 1000).toFixed(1)}s` : `${durationMs}ms`;

  return (
    <div style={{ padding: '5px 0' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, fontSize: 12 }}>
        <span style={{ color: ok ? 'var(--green)' : 'var(--red)', fontWeight: 700, flexShrink: 0 }}>
          {timedOut ? '⏱' : ok ? '▶' : '✗'}
        </span>
        <code style={{ color: 'var(--text)', fontFamily: 'monospace', flex: 1, wordBreak: 'break-all' }}>
          $ {command}
        </code>
        <span style={{ color: 'var(--text2)', flexShrink: 0 }}>{duration}</span>
        {hasOutput && (
          <button
            onClick={() => setExpanded(e => !e)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--text2)', fontSize: 11, flexShrink: 0, padding: 0,
            }}
          >
            {expanded ? '▲ hide' : '▼ output'}
          </button>
        )}
      </div>
      {expanded && hasOutput && (
        <pre style={{
          marginTop: 6, padding: '8px 10px',
          background: 'var(--bg2)', borderRadius: 4,
          fontSize: 11, fontFamily: 'monospace',
          color: ok ? 'var(--text)' : 'var(--red)',
          maxHeight: 240, overflowY: 'auto',
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}>
          {(stdout + stderr).trim()}
        </pre>
      )}
    </div>
  );
}

function ErrorEntry({ message }: { message: string }) {
  return (
    <div style={{ padding: '6px 0', fontSize: 12, color: 'var(--red)' }}>
      ✗ {message}
    </div>
  );
}

function CompleteEntry({ summary, outputDir, onBrowse }: { summary: string; outputDir: string; onBrowse: () => void }) {
  return (
    <div style={{
      margin: '12px 0 4px',
      padding: '14px 16px',
      background: '#0e1a0e',
      border: '1px solid #1a3a1a',
      borderRadius: 8,
    }}>
      <p style={{ fontSize: 13, color: 'var(--green)', fontWeight: 600, marginBottom: 6 }}>✓ Implementation complete</p>
      {outputDir && (
        <p style={{ fontSize: 11, color: 'var(--text2)', marginBottom: summary ? 6 : 8, fontFamily: 'monospace' }}>
          {outputDir}
        </p>
      )}
      {summary && (
        <div className="markdown" style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 10 }}>
          <ReactMarkdown>{summary}</ReactMarkdown>
        </div>
      )}
      <button className="btn-ghost" style={{ fontSize: 12 }} onClick={onBrowse}>Browse files →</button>
    </div>
  );
}

function UserMsgEntry({ content }: { content: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', margin: '10px 0 4px' }}>
      <div style={{
        maxWidth: '72%',
        background: 'var(--accent)',
        color: '#fff',
        borderRadius: '12px 12px 2px 12px',
        padding: '8px 12px',
        fontSize: 13,
        lineHeight: 1.5,
        wordBreak: 'break-word',
      }}>
        {content}
      </div>
    </div>
  );
}

function AssistantMsgEntry({ content }: { content: string }) {
  return (
    <div style={{ margin: '10px 0 4px' }}>
      <div style={{
        maxWidth: '88%',
        background: 'var(--bg3)',
        border: '1px solid var(--border)',
        borderRadius: '2px 12px 12px 12px',
        padding: '10px 14px',
        fontSize: 13,
        lineHeight: 1.5,
        color: 'var(--text)',
        wordBreak: 'break-word',
      }}>
        <div className="markdown" style={{ fontSize: 13 }}>
          <ReactMarkdown>{content}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// File browser
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// File tree helpers
// ---------------------------------------------------------------------------

interface TreeNode {
  name: string;
  fullPath: string;
  type: 'file' | 'dir';
  children: TreeNode[];
  size?: number;
}

function buildTree(files: Phase3FileEntry[]): TreeNode[] {
  const root: TreeNode = { name: '', fullPath: '', type: 'dir', children: [] };

  for (const f of files) {
    const parts = f.path.split('/');
    let node = root;
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      const isLast = i === parts.length - 1;
      let child = node.children.find(c => c.name === part);
      if (!child) {
        const fullPath = parts.slice(0, i + 1).join('/');
        child = { name: part, fullPath, type: isLast ? 'file' : 'dir', children: [], size: isLast ? f.size : undefined };
        node.children.push(child);
      }
      node = child;
    }
  }

  // Sort: dirs first, then files, both alphabetically
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const n of nodes) if (n.type === 'dir') sort(n.children);
  };
  sort(root.children);

  return root.children;
}

function collectDirPaths(nodes: TreeNode[]): string[] {
  const paths: string[] = [];
  const walk = (ns: TreeNode[]) => {
    for (const n of ns) {
      if (n.type === 'dir') { paths.push(n.fullPath); walk(n.children); }
    }
  };
  walk(nodes);
  return paths;
}

function fileIcon(name: string): React.ReactNode {
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : '';
  const base = name.toLowerCase();

  if (base === 'dockerfile') return <SiDocker size={13} color="#2496ed" />;
  if (base === 'makefile') return <VscCode size={13} color="var(--text2)" />;

  switch (ext) {
    case 'py':    return <SiPython     size={13} color="#3572a5" />;
    case 'ts':
    case 'tsx':   return <SiTypescript size={13} color="#3178c6" />;
    case 'js':
    case 'jsx':   return <SiJavascript size={13} color="#f7df1e" />;
    case 'html':  return <SiHtml5      size={13} color="#e34c26" />;
    case 'css':
    case 'scss':  return <SiCss        size={13} color="#264de4" />;
    case 'rs':    return <SiRust       size={13} color="#dea584" />;
    case 'go':    return <SiGo         size={13} color="#00add8" />;
    case 'json':  return <VscJson      size={13} color="#f4c518" />;
    case 'md':    return <VscMarkdown  size={13} color="var(--text2)" />;
    case 'yaml':
    case 'yml':   return <SiYaml       size={13} color="#cb171e" />;
    case 'env':   return <VscSymbolParameter size={13} color="var(--text2)" />;
    case 'sql':
    case 'db':    return <VscDatabase  size={13} color="var(--text2)" />;
    default:      return <VscFile      size={13} color="var(--text2)" />;
  }
}

function TreeNodeRow({ node, depth, selectedPath, collapsed, onToggle, onSelect }: {
  node: TreeNode; depth: number; selectedPath: string | null;
  collapsed: Set<string>; onToggle: (p: string) => void; onSelect: (p: string) => void;
}) {
  const isCollapsed = collapsed.has(node.fullPath);
  const indent = 8 + depth * 14;

  if (node.type === 'dir') {
    return (
      <>
        <div
          onClick={() => onToggle(node.fullPath)}
          style={{
            display: 'flex', alignItems: 'center', gap: 5,
            padding: '4px 10px 4px 0',
            paddingLeft: indent,
            cursor: 'pointer',
            fontSize: 12,
            color: 'var(--text2)',
            userSelect: 'none',
          }}
        >
          <span style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>
            {isCollapsed
              ? <VscFolder size={14} color="#dcb67a" />
              : <VscFolderOpened size={14} color="#dcb67a" />}
          </span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {node.name}
          </span>
        </div>
        {!isCollapsed && node.children.map(child => (
          <TreeNodeRow key={child.fullPath} node={child} depth={depth + 1}
            selectedPath={selectedPath} collapsed={collapsed}
            onToggle={onToggle} onSelect={onSelect} />
        ))}
      </>
    );
  }

  const isSelected = node.fullPath === selectedPath;
  return (
    <div
      onClick={() => onSelect(node.fullPath)}
      style={{
        display: 'flex', alignItems: 'center', gap: 5,
        padding: '3px 10px 3px 0',
        paddingLeft: indent,
        cursor: 'pointer',
        fontSize: 12,
        background: isSelected ? 'var(--bg3)' : undefined,
        color: isSelected ? 'var(--text)' : 'var(--text2)',
        borderLeft: isSelected ? '2px solid var(--accent)' : '2px solid transparent',
        overflow: 'hidden',
      }}
    >
      <span style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>
        {fileIcon(node.name)}
      </span>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {node.name}
      </span>
    </div>
  );
}

function FileBrowser({ ideaId }: { ideaId: string }) {
  const [files, setFiles] = useState<Phase3FileEntry[]>([]);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [loadingFile, setLoadingFile] = useState(false);
  const [truncated, setTruncated] = useState(false);

  useEffect(() => {
    api.listPhase3Files(ideaId)
      .then((data) => {
        setFiles(data.files);
        const t = buildTree(data.files);
        setTree(t);
        // All folders collapsed by default
        setCollapsed(new Set(collectDirPaths(t)));
        // Select first file
        const firstFile = data.files.find(f => !f.path.endsWith('/'));
        if (firstFile) setSelectedPath(firstFile.path);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [ideaId]);

  useEffect(() => {
    if (!selectedPath) return;
    setLoadingFile(true);
    setFileContent('');
    api.getPhase3File(ideaId, selectedPath)
      .then((d) => { setFileContent(d.content); setTruncated(d.truncated); })
      .catch(() => setFileContent('(failed to load file)'))
      .finally(() => setLoadingFile(false));
  }, [selectedPath]);

  const toggleDir = (path: string) => {
    setCollapsed(prev => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  };

  // When a file is selected, expand all its ancestor folders
  const selectFile = (path: string) => {
    setSelectedPath(path);
    const parts = path.split('/');
    if (parts.length > 1) {
      setCollapsed(prev => {
        const next = new Set(prev);
        for (let i = 1; i < parts.length; i++) {
          next.delete(parts.slice(0, i).join('/'));
        }
        return next;
      });
    }
  };

  if (loading) return <p style={{ color: 'var(--text2)', padding: '12px 0', fontSize: 13 }}>Loading files…</p>;

  if (files.length === 0) {
    return (
      <div className="card" style={{ padding: '32px 24px', textAlign: 'center' }}>
        <p style={{ color: 'var(--text2)', fontSize: 13 }}>No files found in output directory.</p>
      </div>
    );
  }

  const selectedFile = files.find((f) => f.path === selectedPath);

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 220px)', gap: 0, border: '1px solid var(--border)', borderRadius: 'var(--radius)', overflow: 'hidden' }}>
      {/* File tree sidebar */}
      <div style={{
        width: 240, flexShrink: 0,
        borderRight: '1px solid var(--border)',
        background: 'var(--bg2)',
        overflowY: 'auto',
        padding: '8px 0',
      }}>
        <p style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.06em', padding: '4px 14px 8px' }}>
          {files.length} file{files.length !== 1 ? 's' : ''}
        </p>
        {tree.map(node => (
          <TreeNodeRow key={node.fullPath} node={node} depth={0}
            selectedPath={selectedPath} collapsed={collapsed}
            onToggle={toggleDir} onSelect={selectFile} />
        ))}
      </div>

      {/* File content */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, background: 'var(--bg)' }}>
        {/* File header */}
        <div style={{
          padding: '8px 14px',
          borderBottom: '1px solid var(--border)',
          background: 'var(--bg2)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          flexShrink: 0,
        }}>
          <code style={{ fontSize: 12, color: 'var(--text2)', fontFamily: 'monospace' }}>
            {selectedPath ?? '—'}
          </code>
          {selectedFile && (
            <span style={{ fontSize: 11, color: 'var(--text2)' }}>
              {(selectedFile.size / 1024).toFixed(1)} KB
            </span>
          )}
        </div>

        {/* Content area */}
        <div style={{ flex: 1, overflowY: 'auto', position: 'relative' }}>
          {loadingFile ? (
            <p style={{ color: 'var(--text2)', padding: '16px', fontSize: 12 }}>Loading…</p>
          ) : (
            <>
              {truncated && (
                <div style={{ padding: '6px 14px', background: '#2a2a14', borderBottom: '1px solid var(--border)', fontSize: 11, color: 'var(--yellow)' }}>
                  ⚠ File truncated at 256 KB for display
                </div>
              )}
              <pre style={{
                padding: '14px',
                fontSize: 12,
                fontFamily: 'monospace',
                lineHeight: 1.6,
                color: 'var(--text)',
                margin: 0,
                whiteSpace: 'pre',
                overflowX: 'auto',
              }}>
                {fileContent}
              </pre>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

type MainTab = 'log' | 'files';

export function Phase3Implementation() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [idea, setIdea] = useState<IdeaDetail | null>(null);
  const [session, setSession] = useState<Phase3Session | null>(null);
  const [log, setLog] = useState<ActivityEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [mainTab, setMainTab] = useState<MainTab>('log');
  const [chatInput, setChatInput] = useState('');
  const [sending, setSending] = useState(false);

  const logEndRef = useRef<HTMLDivElement>(null);

  const addEntry = (entry: ActivityEntry) =>
    setLog(prev => {
      if (entry.kind === 'thinking') {
        if (prev.some(e => e.kind === 'thinking')) return prev;
        return [...prev, entry];
      }
      if (entry.kind === 'writing') {
        // Replace previous writing entry to show only current file
        return [...prev.filter(e => e.kind !== 'writing' && e.kind !== 'thinking'), entry];
      }
      return [...prev.filter(e => e.kind !== 'thinking'), entry];
    });

  // Auto-scroll log on new entries or when switching back to the chat tab
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [log]);

  useEffect(() => {
    if (mainTab === 'log') logEndRef.current?.scrollIntoView({ behavior: 'instant' });
  }, [mainTab]);

  // Load idea, existing session, activity log, and chat messages
  useEffect(() => {
    if (!id) return;
    api.getIdea(id).then(setIdea).catch(() => {});
    api.getPhase3(id)
      .then(async (s) => {
        setSession(s);

        const [events, messages] = await Promise.all([
          api.getPhase3Activity(id).catch(() => [] as Phase3ActivityEvent[]),
          api.getPhase3Messages(id).catch(() => [] as Phase3ChatMessage[]),
        ]);

        // Build activity entries from persisted events
        const activityEntries: (ActivityEntry & { ts: string })[] = events.flatMap((e: Phase3ActivityEvent): (ActivityEntry & { ts: string })[] => {
          if (e.event_type === 'plan_ready') {
            return [{ kind: 'plan_ready', id: nextId(), fileCount: e.payload.file_count as number, message: (e.payload.message as string) || '', ts: e.created_at }];
          } else if (e.event_type === 'pass_started') {
            return [];
          } else if (e.event_type === 'file_written') {
            return [{ kind: 'file', id: nextId(), path: e.payload.path as string, sizeBytes: e.payload.size_bytes as number, success: true, ts: e.created_at }];
          } else if (e.event_type === 'command_executed') {
            return [{ kind: 'shell', id: nextId(), command: e.payload.command as string, exitCode: e.payload.exit_code as number, stdout: e.payload.stdout as string, stderr: e.payload.stderr as string, timedOut: e.payload.timed_out as boolean, durationMs: e.payload.duration_ms as number, ts: e.created_at }];
          } else {
            return [{ kind: 'error', id: nextId(), message: e.payload.message as string, ts: e.created_at }];
          }
        });

        // Merge activity events and chat messages, sorted by timestamp
        const allTimestamped = [
          ...activityEntries,
          ...messages.map((m: Phase3ChatMessage) => ({
            kind: m.role === 'user' ? 'user_msg' as const : 'assistant_msg' as const,
            id: nextId(),
            messageId: m.id,
            content: m.content,
            ts: m.created_at,
          })),
        ].sort((a, b) => a.ts.localeCompare(b.ts));

        const allEntries: ActivityEntry[] = allTimestamped.map(({ ts: _ts, ...e }) => e as ActivityEntry);

        // Insert complete bubble just before the first user message (or at the end if no messages)
        if (s.status === 'COMPLETE' && s.summary) {
          const firstUserIdx = allEntries.findIndex(e => e.kind === 'user_msg');
          const insertAt = firstUserIdx === -1 ? allEntries.length : firstUserIdx;
          allEntries.splice(insertAt, 0, { kind: 'complete', id: nextId(), summary: s.summary, outputDir: s.output_dir ?? '' });
        }

        if (allEntries.length > 0) setLog(allEntries);
      })
      .catch(() => {});
  }, [id]);

  // WebSocket — real-time activity
  useEffect(() => {
    if (!id) return;
    const ws = new WebSocket(`${WS_BASE}/ws/ideas/${id}`);

    ws.onmessage = (ev) => {
      try {
        const event: PipelineEvent = JSON.parse(ev.data);
        if (!event.event_type.startsWith('phase3.')) return;

        switch (event.event_type) {
          case 'phase3.started':
          case 'phase3.running':
            api.getPhase3(id!).then(setSession).catch(() => {});
            break;

          case 'phase3.thinking':
            addEntry({ kind: 'thinking', id: nextId() });
            break;

          case 'phase3.plan_ready':
            addEntry({
              kind: 'plan_ready', id: nextId(),
              fileCount: event.payload.artifact_count as number,
              message: (event.payload.message as string) || '',
            });
            break;

          case 'phase3.pass_started':
            addEntry({
              kind: 'writing', id: nextId(),
              filePath: event.payload.file_path as string,
              fileIndex: event.payload.file_index as number,
              totalFiles: event.payload.total_files as number,
            });
            break;

          case 'phase3.file_written':
            addEntry({
              kind: 'file', id: nextId(),
              path: event.payload.path as string,
              sizeBytes: event.payload.size_bytes as number,
              success: true,
            });
            break;

          case 'phase3.command_executed':
            addEntry({
              kind: 'shell', id: nextId(),
              command: event.payload.command as string,
              exitCode: event.payload.exit_code as number,
              stdout: event.payload.stdout as string,
              stderr: event.payload.stderr as string,
              timedOut: event.payload.timed_out as boolean,
              durationMs: event.payload.duration_ms as number,
            });
            break;

          case 'phase3.error':
            setLog(prev => prev.filter(e => e.kind !== 'thinking' && e.kind !== 'writing'));
            addEntry({ kind: 'error', id: nextId(), message: event.payload.error as string });
            api.getPhase3(id!).then(setSession).catch(() => {});
            break;

          case 'phase3.complete':
            setLog(prev => prev.filter(e => e.kind !== 'thinking' && e.kind !== 'writing'));
            api.getPhase3(id!).then((s) => {
              setSession(s);
              if (s.summary) {
                addEntry({ kind: 'complete', id: nextId(), summary: s.summary, outputDir: s.output_dir ?? '' });
              }
            }).catch(() => {});
            break;

          case 'phase3.message': {
            const role = event.payload.role as string;
            const content = event.payload.content as string;
            const messageId = event.payload.message_id as string;
            // Skip user-role echo — we add user messages optimistically in doSend
            if (role === 'assistant') {
              setLog(prev => prev.filter(e => e.kind !== 'thinking' && e.kind !== 'writing'));
              addEntry({ kind: 'assistant_msg', id: nextId(), messageId, content });
              api.getPhase3(id!).then(setSession).catch(() => {});
            }
            break;
          }
        }
      } catch { /* ignore */ }
    };

    return () => ws.close();
  }, [id]);

  const doStart = async () => {
    if (!id) return;
    setStarting(true);
    setError(null);
    try {
      const s = await api.startPhase3(id);
      setSession(s);
      setLog([]);
    } catch (e: unknown) {
      setError(`Failed to start: ${(e as Error).message}`);
    } finally {
      setStarting(false);
    }
  };

  const doSend = async () => {
    if (!id || !chatInput.trim() || sending) return;
    const content = chatInput.trim();
    setChatInput('');
    setSending(true);
    try {
      await api.sendPhase3Message(id, content);
      // Optimistically add user message; WS will confirm
      addEntry({ kind: 'user_msg', id: nextId(), messageId: '', content });
      addEntry({ kind: 'thinking', id: nextId() });
    } catch (e: unknown) {
      setError(`Failed to send: ${(e as Error).message}`);
    } finally {
      setSending(false);
    }
  };

  const doCancel = async () => {
    if (!id) return;
    setCancelling(true);
    try {
      await api.cancelPhase3(id);
      const s = await api.getPhase3(id);
      setSession(s);
    } catch (e: unknown) {
      setError(`Failed to cancel: ${(e as Error).message}`);
    } finally {
      setCancelling(false);
    }
  };

  if (!idea) {
    return <div className="page"><p style={{ color: 'var(--text2)' }}>Loading…</p></div>;
  }

  const selectedBranch = idea.branches.find((b) => b.id === idea.selected_branch_id);

  // No session yet — start screen
  if (!session) {
    return (
      <div className="page">
        <button className="btn-ghost" style={{ fontSize: 12, marginBottom: 16 }}
          onClick={() => navigate(`/ideas/${id}/phase2`)}>
          ← Back to Q&A
        </button>
        <div style={{ maxWidth: 640, margin: '60px auto', textAlign: 'center' }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 12 }}>Phase 3 — Implementation</h1>
          <p style={{ fontSize: 14, color: 'var(--text2)', marginBottom: 8 }}>
            <strong style={{ color: 'var(--text)' }}>{idea.name}</strong>
            {selectedBranch && <> · Branch {selectedBranch.branch_index}</>}
          </p>
          {selectedBranch?.approach_summary && (
            <p style={{ fontSize: 13, color: 'var(--text2)', marginBottom: 28 }}>{selectedBranch.approach_summary}</p>
          )}
          <div className="card" style={{ textAlign: 'left', marginBottom: 28 }}>
            <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>What happens in Phase 3</p>
            <ol style={{ fontSize: 13, color: 'var(--text2)', paddingLeft: 20, lineHeight: 1.8 }}>
              <li>The agent reads your resolved requirements and architecture documents</li>
              <li>It generates all project files and writes them to disk</li>
              <li>Setup commands (install, build, test) are run automatically</li>
              <li>You can browse the generated files when complete</li>
            </ol>
          </div>
          {error && <p style={{ color: 'var(--red)', fontSize: 13, marginBottom: 16 }}>{error}</p>}
          <button
            className="btn-primary"
            style={{ fontSize: 14, padding: '10px 28px' }}
            disabled={starting}
            onClick={doStart}
          >
            {starting ? 'Starting…' : 'Begin Implementation'}
          </button>
        </div>
      </div>
    );
  }

  const isRunning = session.status === 'PLANNING' || session.status === 'RUNNING';
  const isComplete = session.status === 'COMPLETE';
  const isFailed = session.status === 'FAILED' || (!isRunning && !isComplete);
  const hasActivity = log.length > 0;
  const wasCancelled = isFailed && session.summary === 'Cancelled by user';
  // Keep chat available after stop/failure so the user can redirect the agent.
  // Also show it while running if there's been a timeout — user needs to be able to intervene.
  const hasTimedOut = log.some(e => e.kind === 'shell' && (e as Extract<ActivityEntry, { kind: 'shell' }>).timedOut);
  const showChatInput = isComplete || isFailed || (isRunning && (log.some(e => e.kind === 'complete') || hasTimedOut));
  const showRetryButton = isFailed && !wasCancelled && !hasActivity;

  const fileCount = log.filter(e => e.kind === 'file').length;
  const shellCount = log.filter(e => e.kind === 'shell').length;

  return (
    <div className="page" style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 56px)', padding: 0 }}>
      {/* Header */}
      <div style={{
        padding: '10px 20px',
        borderBottom: '1px solid var(--border)',
        flexShrink: 0,
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        background: 'var(--bg2)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <PhaseNav ideaId={id!} currentPhase={3} />
          {selectedBranch && (
            <span style={{ fontSize: 12, color: 'var(--text2)' }}>· Branch {selectedBranch.branch_index}</span>
          )}
          {session.project_root && (
            <code style={{ fontSize: 11, color: 'var(--text2)', fontFamily: 'monospace' }}>
              /{session.project_root}
            </code>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className={`badge badge-${session.status.toLowerCase()}`} style={{ fontSize: 11 }}>
            {session.status}
          </span>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div style={{
          padding: '8px 20px', background: '#1e0e0e', borderBottom: '1px solid var(--red)',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0,
        }}>
          <span style={{ fontSize: 12, color: 'var(--red)' }}>{error}</span>
          <button className="btn-ghost" style={{ fontSize: 11 }} onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      {/* Main content */}
      <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>

        {/* Stats + tab bar */}
        <div style={{
          padding: '0 20px',
          borderBottom: '1px solid var(--border)',
          background: 'var(--bg2)',
          flexShrink: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <div className="tabs" style={{ marginBottom: 0, borderBottom: 'none', gap: 0 }}>
            <button
              className={`tab ${mainTab === 'log' ? 'active' : ''}`}
              onClick={() => setMainTab('log')}
              style={{ fontSize: 12 }}
            >
              Activity
              {(fileCount > 0 || shellCount > 0) && (
                <span style={{ marginLeft: 6, color: 'var(--text2)', fontSize: 11 }}>
                  {fileCount > 0 && `${fileCount}f`}{fileCount > 0 && shellCount > 0 ? ' ' : ''}{shellCount > 0 && `${shellCount}c`}
                </span>
              )}
            </button>
            {isComplete && (
              <button
                className={`tab ${mainTab === 'files' ? 'active' : ''}`}
                onClick={() => setMainTab('files')}
                style={{ fontSize: 12 }}
              >
                Files {fileCount > 0 && <span style={{ marginLeft: 4, color: 'var(--green)', fontSize: 11 }}>{fileCount}</span>}
              </button>
            )}
          </div>
        </div>

        {/* Activity log tab */}
        {mainTab === 'log' && (
          <>
            <div style={{ flex: 1, overflowY: 'auto', padding: '12px 20px' }}>
              {isRunning && log.length === 0 && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text2)', fontSize: 12 }}>
                  <div style={{ display: 'flex', gap: 3 }}>
                    <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
                  </div>
                  <span>{session.status === 'PLANNING' ? 'Preparing implementation…' : 'Waiting for agent to start…'}</span>
                </div>
              )}

              {log.map((entry) => {
                switch (entry.kind) {
                  case 'thinking': return <ThinkingEntry key={entry.id} />;
                  case 'plan_ready': return <PlanReadyEntry key={entry.id} fileCount={entry.fileCount} message={entry.message} />;
                  case 'writing': return <WritingEntry key={entry.id} filePath={entry.filePath} fileIndex={entry.fileIndex} totalFiles={entry.totalFiles} />;
                  case 'file': return <FileEntry key={entry.id} path={entry.path} sizeBytes={entry.sizeBytes} success={entry.success} />;
                  case 'shell': return (
                    <ShellEntry key={entry.id}
                      command={entry.command} exitCode={entry.exitCode}
                      stdout={entry.stdout} stderr={entry.stderr}
                      timedOut={entry.timedOut} durationMs={entry.durationMs}
                    />
                  );
                  case 'error': return <ErrorEntry key={entry.id} message={entry.message} />;
                  case 'complete': return <CompleteEntry key={entry.id} summary={entry.summary} outputDir={entry.outputDir} onBrowse={() => setMainTab('files')} />;
                  case 'user_msg': return <UserMsgEntry key={entry.id} content={entry.content} />;
                  case 'assistant_msg': return <AssistantMsgEntry key={entry.id} content={entry.content} />;
                }
              })}

              <div ref={logEndRef} />
            </div>

            {/* Chat input — visible once session has completed at least once */}
            {showChatInput && (
              <div style={{
                flexShrink: 0,
                borderTop: '1px solid var(--border)',
                padding: '10px 16px',
                background: 'var(--bg2)',
                display: 'flex',
                gap: 8,
                alignItems: 'flex-end',
              }}>
                <textarea
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !isRunning) { e.preventDefault(); doSend(); } }}
                  placeholder={isRunning ? 'Agent is working…' : 'Request a change or addition…'}
                  disabled={isRunning}
                  rows={1}
                  style={{
                    flex: 1,
                    background: 'var(--bg)',
                    border: '1px solid var(--border)',
                    borderRadius: 8,
                    color: isRunning ? 'var(--text2)' : 'var(--text)',
                    padding: '8px 12px',
                    fontSize: 13,
                    resize: 'none',
                    outline: 'none',
                    lineHeight: 1.5,
                    maxHeight: 120,
                    overflowY: 'auto',
                    opacity: isRunning ? 0.5 : 1,
                  }}
                />
                {isRunning ? (
                  <button
                    className="btn-ghost"
                    style={{ fontSize: 12, padding: '8px 14px', flexShrink: 0, color: 'var(--red)' }}
                    disabled={cancelling}
                    onClick={doCancel}
                  >
                    {cancelling ? 'Stopping…' : 'Stop'}
                  </button>
                ) : (
                  <button
                    className="btn-primary"
                    style={{ fontSize: 12, padding: '8px 14px', flexShrink: 0 }}
                    disabled={sending || !chatInput.trim()}
                    onClick={doSend}
                  >
                    {sending ? '…' : 'Send'}
                  </button>
                )}
              </div>
            )}

            {/* Failed before useful context exists: start over */}
            {showRetryButton && (
              <div style={{ flexShrink: 0, borderTop: '1px solid var(--border)', padding: '10px 16px', background: 'var(--bg2)' }}>
                <button className="btn-primary" style={{ fontSize: 12 }} disabled={starting} onClick={doStart}>
                  {starting ? 'Starting…' : 'Try again →'}
                </button>
              </div>
            )}
          </>
        )}

        {/* Files tab */}
        {mainTab === 'files' && isComplete && (
          <div style={{ flex: 1, overflow: 'hidden', padding: '12px 20px' }}>
            <FileBrowser ideaId={id!} />
          </div>
        )}
      </div>
    </div>
  );
}
