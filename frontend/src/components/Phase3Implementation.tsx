import React, { useEffect, useRef, useState } from 'react';
import hljs from 'highlight.js/lib/core';
import langBash from 'highlight.js/lib/languages/bash';
import langCss from 'highlight.js/lib/languages/css';
import langDockerfile from 'highlight.js/lib/languages/dockerfile';
import langGo from 'highlight.js/lib/languages/go';
import langHtml from 'highlight.js/lib/languages/xml';
import langJs from 'highlight.js/lib/languages/javascript';
import langJson from 'highlight.js/lib/languages/json';
import langMarkdown from 'highlight.js/lib/languages/markdown';
import langPython from 'highlight.js/lib/languages/python';
import langRust from 'highlight.js/lib/languages/rust';
import langSql from 'highlight.js/lib/languages/sql';
import langTs from 'highlight.js/lib/languages/typescript';
import langToml from 'highlight.js/lib/languages/ini';
import langYaml from 'highlight.js/lib/languages/yaml';
import 'highlight.js/styles/github-dark.css';
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

// Register only the languages we need to keep the bundle small
hljs.registerLanguage('python', langPython);
hljs.registerLanguage('javascript', langJs);
hljs.registerLanguage('typescript', langTs);
hljs.registerLanguage('html', langHtml);
hljs.registerLanguage('css', langCss);
hljs.registerLanguage('json', langJson);
hljs.registerLanguage('yaml', langYaml);
hljs.registerLanguage('bash', langBash);
hljs.registerLanguage('go', langGo);
hljs.registerLanguage('rust', langRust);
hljs.registerLanguage('sql', langSql);
hljs.registerLanguage('dockerfile', langDockerfile);
hljs.registerLanguage('markdown', langMarkdown);
hljs.registerLanguage('toml', langToml);

function hljsLanguage(filename: string): string | undefined {
  const ext = filename.includes('.') ? filename.split('.').pop()!.toLowerCase() : '';
  const base = filename.toLowerCase();
  if (base === 'dockerfile') return 'dockerfile';
  if (base === 'makefile') return 'bash';
  const map: Record<string, string> = {
    py: 'python', js: 'javascript', jsx: 'javascript',
    ts: 'typescript', tsx: 'typescript',
    html: 'html', htm: 'html',
    css: 'css', scss: 'css',
    json: 'json', yaml: 'yaml', yml: 'yaml',
    sh: 'bash', bash: 'bash',
    go: 'go', rs: 'rust', sql: 'sql',
    md: 'markdown', toml: 'toml', ini: 'toml',
  };
  return map[ext];
}

const WS_BASE = 'ws://localhost:8000';

// ---------------------------------------------------------------------------
// Activity log entry types
// ---------------------------------------------------------------------------

type SubAgentUpdate = { updateType: string; detail: string };

type ActivityEntry =
  | { kind: 'thinking'; id: number }
  | { kind: 'tool_use'; id: number; tool: string; detail: string }
  | { kind: 'verifying'; id: number; fileCount: number }
  | { kind: 'plan_ready'; id: number; fileCount: number; message: string }
  | { kind: 'writing'; id: number; filePath: string; fileIndex: number; totalFiles: number }
  | { kind: 'file'; id: number; path: string; sizeBytes: number; success: boolean }
  | { kind: 'file_failed'; id: number; path: string; detail: string }
  | { kind: 'shell'; id: number; command: string; exitCode: number; stdout: string; stderr: string; timedOut: boolean; durationMs: number }
  | { kind: 'shell_stop'; id: number; handle: string; pid: number | null; stopped: boolean; message: string }
  | { kind: 'error'; id: number; message: string }
  | { kind: 'complete'; id: number; summary: string; outputDir: string }
  | { kind: 'user_msg'; id: number; messageId: string; content: string }
  | { kind: 'assistant_msg'; id: number; messageId: string; content: string }
  | { kind: 'orchestrator_thinking'; id: number }
  | { kind: 'orchestrator_message'; id: number; content: string }
  | { kind: 'sub_agent_block'; id: number; taskId: string; title: string; status: 'running' | 'done' | 'blocked'; summary: string; filesWritten: string[]; blocker: string | null; updates: SubAgentUpdate[] };

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

const TOOL_ICONS: Record<string, string> = {
  list_files: '📂',
  read_file: '📄',
  grep_files: '🔍',
  web_search: '🌐',
};

const TOOL_LABELS: Record<string, string> = {
  list_files: 'Listing',
  read_file: 'Reading',
  grep_files: 'Searching',
  web_search: 'Web search',
};

function ToolUseEntry({ tool, detail }: { tool: string; detail: string }) {
  const icon = TOOL_ICONS[tool] ?? '⚙';
  const label = TOOL_LABELS[tool] ?? tool;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', color: 'var(--text2)', fontSize: 12 }}>
      <div style={{ display: 'flex', gap: 3 }}>
        <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
      </div>
      <span>{icon} {label}</span>
      <code style={{ color: 'var(--text2)', fontFamily: 'monospace', opacity: 0.75 }}>{detail}</code>
    </div>
  );
}

function VerifyingEntry({ fileCount }: { fileCount: number }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0 4px', fontSize: 12 }}>
      <span style={{ color: 'var(--yellow)', fontWeight: 700 }}>🔎</span>
      <span style={{ color: 'var(--text2)' }}>Verifying {fileCount} generated file{fileCount !== 1 ? 's' : ''}…</span>
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

function FileFailedEntry({ path, detail }: { path: string; detail: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '5px 0', fontSize: 12 }}>
      <span style={{ color: 'var(--red)', fontWeight: 700, flexShrink: 0 }}>✗</span>
      <code style={{ color: 'var(--red)', fontFamily: 'monospace', flex: 1, wordBreak: 'break-all' }}>{path}</code>
      {detail && <span style={{ color: 'var(--text2)', flexShrink: 0, fontStyle: 'italic' }}>{detail}</span>}
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

function OrchestratorThinkingEntry() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 0', color: 'var(--text2)' }}>
      <div style={{ display: 'flex', gap: 3 }}>
        <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
      </div>
      <span style={{ fontSize: 12 }}>🧭 Orchestrator planning…</span>
    </div>
  );
}

function OrchestratorMessageEntry({ content }: { content: string }) {
  return (
    <div style={{ margin: '10px 0 4px' }}>
      <div style={{ fontSize: 10, color: 'var(--text2)', marginBottom: 4, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        🧭 Orchestrator
      </div>
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

const UPDATE_ICONS: Record<string, string> = {
  file_edit: '✓',
  run_shell: '$',
  list_files: '📂',
  read_file: '📄',
  grep_files: '🔍',
  web_search: '🌐',
};

function SubAgentBlock({ taskId: _taskId, title, status, summary, filesWritten, blocker, updates }: {
  taskId: string; title: string; status: 'running' | 'done' | 'blocked';
  summary: string; filesWritten: string[]; blocker: string | null; updates: SubAgentUpdate[];
}) {
  const [expanded, setExpanded] = useState(false);
  const isRunning = status === 'running';

  const statusColor = status === 'done' ? 'var(--green)' : status === 'blocked' ? 'var(--yellow)' : 'var(--text2)';
  const statusIcon = status === 'done' ? '✓' : status === 'blocked' ? '⚠' : null;

  return (
    <div style={{
      margin: '6px 0',
      border: '1px solid var(--border)',
      borderRadius: 8,
      overflow: 'hidden',
      background: 'var(--bg2)',
    }}>
      {/* Header */}
      <div
        onClick={() => setExpanded(e => !e)}
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '8px 12px', cursor: 'pointer',
          userSelect: 'none',
        }}
      >
        <span style={{ color: statusColor, fontWeight: 700, flexShrink: 0, fontSize: 13, minWidth: 16 }}>
          {isRunning ? (
            <span style={{ display: 'flex', gap: 2 }}>
              <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
            </span>
          ) : statusIcon}
        </span>
        <span style={{ fontSize: 13, fontWeight: 600, flex: 1, color: 'var(--text)', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {title}
        </span>
        {filesWritten.length > 0 && (
          <span style={{ fontSize: 11, color: 'var(--text2)', flexShrink: 0 }}>
            {filesWritten.length}f
          </span>
        )}
        <span style={{ fontSize: 10, color: 'var(--text2)', flexShrink: 0 }}>
          {expanded ? '▲' : '▼'}
        </span>
      </div>

      {/* Summary line (when done) */}
      {!isRunning && summary && (
        <div style={{ padding: '0 12px 6px', fontSize: 12, color: 'var(--text2)', borderTop: '1px solid var(--border)' }}>
          {blocker && <span style={{ color: 'var(--yellow)', marginRight: 6 }}>⚠ Blocker:</span>}
          {blocker || summary}
        </div>
      )}

      {/* Expanded activity */}
      {expanded && updates.length > 0 && (
        <div style={{ borderTop: '1px solid var(--border)', padding: '6px 12px', maxHeight: 240, overflowY: 'auto' }}>
          {updates.map((u, i) => {
            const icon = UPDATE_ICONS[u.updateType] ?? '·';
            const isFile = u.updateType === 'file_edit';
            return (
              <div key={i} style={{ display: 'flex', alignItems: 'baseline', gap: 6, padding: '2px 0', fontSize: 11 }}>
                <span style={{ color: isFile ? 'var(--green)' : 'var(--text2)', flexShrink: 0 }}>{icon}</span>
                <code style={{ color: 'var(--text2)', fontFamily: 'monospace', wordBreak: 'break-all' }}>{u.detail}</code>
              </div>
            );
          })}
        </div>
      )}
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

function HighlightedCode({ content, filename }: { content: string; filename: string }) {
  const lang = hljsLanguage(filename);
  const highlighted = lang && hljs.getLanguage(lang)
    ? hljs.highlight(content, { language: lang }).value
    : hljs.highlightAuto(content, Object.keys(hljs.listLanguages())).value;
  return (
    <pre style={{ margin: 0, padding: 0, background: 'transparent', overflowX: 'auto' }}>
      <code
        className={`hljs language-${lang ?? 'plaintext'}`}
        style={{ display: 'block', padding: '14px', fontSize: 12, lineHeight: 1.6, fontFamily: 'monospace' }}
        dangerouslySetInnerHTML={{ __html: highlighted }}
      />
    </pre>
  );
}

function FileBrowser({ ideaId, refreshKey }: { ideaId: string; refreshKey: number }) {
  const [files, setFiles] = useState<Phase3FileEntry[]>([]);
  const [outputDir, setOutputDir] = useState<string | null>(null);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [loadingFile, setLoadingFile] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    // On initial load show the spinner; on live refreshes keep existing content visible
    if (files.length === 0) setLoading(true);
    api.listPhase3Files(ideaId)
      .then((data) => {
        setFiles(data.files);
        setOutputDir(data.output_dir ?? null);
        const t = buildTree(data.files);
        setTree(t);
        setCollapsed(prev => {
          // Preserve existing collapsed state; collapse any new dirs
          const newDirs = new Set(collectDirPaths(t));
          const next = new Set(prev);
          for (const d of newDirs) if (!next.has(d)) next.add(d);
          return next;
        });
        // Only auto-select if nothing is selected yet
        setSelectedPath(prev => {
          if (prev) return prev;
          const firstFile = data.files.find(f => !f.path.endsWith('/'));
          return firstFile ? firstFile.path : null;
        });
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [ideaId, refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

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

  const totalSize = files.reduce((sum, f) => sum + f.size, 0);
  const totalSizeLabel = totalSize >= 1024 * 1024
    ? `${(totalSize / (1024 * 1024)).toFixed(1)} MB`
    : `${(totalSize / 1024).toFixed(1)} KB`;

  const handleCopy = () => {
    if (!fileContent) return;
    navigator.clipboard.writeText(fileContent).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const vsCodeHref = outputDir && selectedPath
    ? `vscode://file/${outputDir.replace(/\\/g, '/')}/${selectedPath}`
    : null;

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
          {files.length} file{files.length !== 1 ? 's' : ''} · {totalSizeLabel}
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
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {selectedFile && (
              <span style={{ fontSize: 11, color: 'var(--text2)' }}>
                {(selectedFile.size / 1024).toFixed(1)} KB
              </span>
            )}
            {vsCodeHref && (
              <a
                href={vsCodeHref}
                title="Open in VS Code"
                style={{
                  fontSize: 11, color: 'var(--text2)', textDecoration: 'none',
                  padding: '2px 7px', borderRadius: 4,
                  border: '1px solid var(--border)', lineHeight: '18px',
                }}
              >
                VS Code
              </a>
            )}
            <button
              onClick={handleCopy}
              disabled={!fileContent || loadingFile}
              title="Copy file content"
              style={{
                fontSize: 11, color: copied ? 'var(--green)' : 'var(--text2)',
                padding: '2px 7px', borderRadius: 4,
                border: '1px solid var(--border)', background: 'none',
                cursor: fileContent ? 'pointer' : 'default', lineHeight: '18px',
              }}
            >
              {copied ? 'Copied!' : 'Copy'}
            </button>
          </div>
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
              <HighlightedCode content={fileContent} filename={selectedPath ?? ''} />
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
  const [regenPrd, setRegenPrd] = useState(false);
  const [fileRefreshKey, setFileRefreshKey] = useState(0);
  const [selectedMode, setSelectedMode] = useState<'classic' | 'multi_agent'>('classic');
  const fileRefreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const logEndRef = useRef<HTMLDivElement>(null);

  const addEntry = (entry: ActivityEntry) =>
    setLog(prev => {
      if (entry.kind === 'thinking') {
        if (prev.some(e => e.kind === 'thinking')) return prev;
        return [...prev, entry];
      }
      if (entry.kind === 'orchestrator_thinking') {
        // Replace previous orchestrator_thinking
        return [...prev.filter(e => e.kind !== 'orchestrator_thinking' && e.kind !== 'thinking'), entry];
      }
      if (entry.kind === 'tool_use') {
        return [...prev.filter(e => e.kind !== 'tool_use' && e.kind !== 'thinking'), entry];
      }
      if (entry.kind === 'writing') {
        return [...prev.filter(e => e.kind !== 'writing' && e.kind !== 'tool_use' && e.kind !== 'thinking'), entry];
      }
      // orchestrator_message and sub_agent_block clear transient thinking
      if (entry.kind === 'orchestrator_message' || entry.kind === 'sub_agent_block') {
        return [...prev.filter(e => e.kind !== 'orchestrator_thinking' && e.kind !== 'thinking' && e.kind !== 'tool_use'), entry];
      }
      return [...prev.filter(e => e.kind !== 'thinking' && e.kind !== 'tool_use'), entry];
    });

  const updateSubAgentBlock = (taskId: string, updater: (e: Extract<ActivityEntry, { kind: 'sub_agent_block' }>) => Extract<ActivityEntry, { kind: 'sub_agent_block' }>) =>
    setLog(prev => prev.map(e => e.kind === 'sub_agent_block' && e.taskId === taskId ? updater(e) : e));

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
          } else if (e.event_type === 'file_failed') {
            return [{ kind: 'file_failed', id: nextId(), path: e.payload.path as string, detail: e.payload.detail as string, ts: e.created_at }];
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

          case 'phase3.tool_use':
            addEntry({
              kind: 'tool_use', id: nextId(),
              tool: event.payload.tool as string,
              detail: event.payload.detail as string,
            });
            break;

          case 'phase3.verifying':
            addEntry({ kind: 'verifying', id: nextId(), fileCount: event.payload.file_count as number });
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
            // Debounce file list refresh — at most once every 4 s during active generation
            if (fileRefreshTimer.current) clearTimeout(fileRefreshTimer.current);
            fileRefreshTimer.current = setTimeout(() => setFileRefreshKey(k => k + 1), 4000);
            break;

          case 'phase3.file_failed':
            addEntry({
              kind: 'file_failed', id: nextId(),
              path: event.payload.path as string,
              detail: event.payload.detail as string,
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

          case 'phase3.shell_stop':
            addEntry({
              kind: 'shell_stop', id: nextId(),
              handle: event.payload.handle as string,
              pid: event.payload.pid as number | null,
              stopped: event.payload.stopped as boolean,
              message: event.payload.message as string,
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
              if (s.summary && !event.payload.is_iteration) {
                addEntry({ kind: 'complete', id: nextId(), summary: s.summary, outputDir: s.output_dir ?? '' });
              }
            }).catch(() => {});
            // Final refresh of file list when generation finishes
            if (fileRefreshTimer.current) clearTimeout(fileRefreshTimer.current);
            setFileRefreshKey(k => k + 1);
            break;

          case 'phase3.message': {
            const role = event.payload.role as string;
            const content = event.payload.content as string;
            const messageId = event.payload.message_id as string;
            // Skip user-role echo — we add user messages optimistically in doSend
            if (role === 'assistant') {
              setLog(prev => prev.filter(e => e.kind !== 'thinking' && e.kind !== 'writing'));
              addEntry({ kind: 'assistant_msg', id: nextId(), messageId, content });
            }
            break;
          }

          // ── Multi-agent events ────────────────────────────────────────────
          case 'phase3.waiting':
            api.getPhase3(id!).then(setSession).catch(() => {});
            break;

          case 'phase3.orchestrator_thinking':
            addEntry({ kind: 'orchestrator_thinking', id: nextId() });
            break;

          case 'phase3.orchestrator_message':
            addEntry({
              kind: 'orchestrator_message', id: nextId(),
              content: event.payload.content as string,
            });
            break;

          case 'phase3.sub_agent_started':
            addEntry({
              kind: 'sub_agent_block', id: nextId(),
              taskId: event.payload.task_id as string,
              title: event.payload.title as string,
              status: 'running',
              summary: '',
              filesWritten: [],
              blocker: null,
              updates: [],
            });
            break;

          case 'phase3.sub_agent_update': {
            const taskId = event.payload.task_id as string;
            const updateType = event.payload.update_type as string;
            const detail = event.payload.detail as string;
            updateSubAgentBlock(taskId, e => ({
              ...e,
              updates: [...e.updates, { updateType, detail }],
            }));
            break;
          }

          case 'phase3.sub_agent_complete': {
            const taskId = event.payload.task_id as string;
            const summary = event.payload.summary as string;
            const filesWritten = (event.payload.files_written as string[]) ?? [];
            const success = event.payload.success as boolean;
            const blocker = event.payload.blocker as string | null;
            updateSubAgentBlock(taskId, e => ({
              ...e,
              status: success && !blocker ? 'done' : 'blocked',
              summary,
              filesWritten,
              blocker,
            }));
            // Debounce file list refresh on sub-agent completion
            if (fileRefreshTimer.current) clearTimeout(fileRefreshTimer.current);
            fileRefreshTimer.current = setTimeout(() => setFileRefreshKey(k => k + 1), 4000);
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
      const s = await api.startPhase3(id, selectedMode);
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

  const doRegenPrd = async () => {
    if (!id || regenPrd) return;
    setRegenPrd(true);
    setError(null);
    try {
      await api.regeneratePrd(id);
      addEntry({ kind: 'thinking', id: nextId() });
    } catch (e: unknown) {
      setError(`Failed to regenerate PRD: ${(e as Error).message}`);
    } finally {
      setRegenPrd(false);
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
          {/* Mode selector */}
          <div style={{ display: 'flex', gap: 12, marginBottom: 24, justifyContent: 'center' }}>
            {(['classic', 'multi_agent'] as const).map(m => (
              <button
                key={m}
                onClick={() => setSelectedMode(m)}
                style={{
                  padding: '10px 20px',
                  borderRadius: 8,
                  border: `2px solid ${selectedMode === m ? 'var(--accent)' : 'var(--border)'}`,
                  background: selectedMode === m ? 'color-mix(in srgb, var(--accent) 12%, transparent)' : 'var(--bg2)',
                  color: selectedMode === m ? 'var(--text)' : 'var(--text2)',
                  cursor: 'pointer',
                  textAlign: 'left',
                  minWidth: 200,
                  transition: 'all 0.15s',
                }}
              >
                <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 4 }}>
                  {m === 'classic' ? '⚡ Classic' : '🧭 Multi-Agent'}
                </div>
                <div style={{ fontSize: 11, lineHeight: 1.5, color: selectedMode === m ? 'var(--text2)' : 'var(--text2)', opacity: 0.85 }}>
                  {m === 'classic'
                    ? 'Direct generation — one file at a time, fast and predictable'
                    : 'Orchestrated — planner delegates tasks to specialized sub-agents'}
                </div>
              </button>
            ))}
          </div>

          <div className="card" style={{ textAlign: 'left', marginBottom: 28 }}>
            {selectedMode === 'classic' ? (
              <>
                <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Classic mode</p>
                <ol style={{ fontSize: 13, color: 'var(--text2)', paddingLeft: 20, lineHeight: 1.8 }}>
                  <li>Generates the PRD and all project files in sequence</li>
                  <li>Runs setup commands (install, build, test) automatically</li>
                  <li>Post-generation verification pass fixes import errors</li>
                  <li>You can browse files and request changes when done</li>
                </ol>
              </>
            ) : (
              <>
                <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Multi-agent mode</p>
                <ol style={{ fontSize: 13, color: 'var(--text2)', paddingLeft: 20, lineHeight: 1.8 }}>
                  <li>Generates the PRD first to anchor all decisions</li>
                  <li>An orchestrator reads the PRD and plans tasks autonomously</li>
                  <li>Sub-agents execute each task: writing files, running commands</li>
                  <li>The orchestrator can ask you questions if it needs input</li>
                </ol>
              </>
            )}
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
  const isWaiting = session.status === 'WAITING';
  const isComplete = session.status === 'COMPLETE';
  const isFailed = session.status === 'FAILED' || (!isRunning && !isWaiting && !isComplete);
  const hasActivity = log.length > 0;
  const wasCancelled = isFailed && session.summary === 'Cancelled by user';
  const hasTimedOut = log.some(e => e.kind === 'shell' && (e as Extract<ActivityEntry, { kind: 'shell' }>).timedOut);
  const showChatInput = isComplete || isFailed || isWaiting || (isRunning && (log.some(e => e.kind === 'complete') || hasTimedOut));
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
            {fileCount > 0 && (
              <button
                className={`tab ${mainTab === 'files' ? 'active' : ''}`}
                onClick={() => setMainTab('files')}
                style={{ fontSize: 12 }}
              >
                Files <span style={{ marginLeft: 4, color: isComplete ? 'var(--green)' : 'var(--text2)', fontSize: 11 }}>{fileCount}</span>
              </button>
            )}
          </div>
          {isComplete && !isRunning && (
            <button
              className="btn-ghost"
              style={{ fontSize: 11, padding: '3px 10px', color: 'var(--text2)' }}
              disabled={regenPrd}
              onClick={doRegenPrd}
              title="Re-generate docs/PRD.md from Phase 2 documents"
            >
              {regenPrd ? 'Regenerating…' : 'Regenerate PRD'}
            </button>
          )}
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
                  case 'tool_use': return <ToolUseEntry key={entry.id} tool={entry.tool} detail={entry.detail} />;
                  case 'verifying': return <VerifyingEntry key={entry.id} fileCount={entry.fileCount} />;
                  case 'plan_ready': return <PlanReadyEntry key={entry.id} fileCount={entry.fileCount} message={entry.message} />;
                  case 'writing': return <WritingEntry key={entry.id} filePath={entry.filePath} fileIndex={entry.fileIndex} totalFiles={entry.totalFiles} />;
                  case 'file': return <FileEntry key={entry.id} path={entry.path} sizeBytes={entry.sizeBytes} success={entry.success} />;
                  case 'file_failed': return <FileFailedEntry key={entry.id} path={entry.path} detail={entry.detail} />;
                  case 'shell': return (
                    <ShellEntry key={entry.id}
                      command={entry.command} exitCode={entry.exitCode}
                      stdout={entry.stdout} stderr={entry.stderr}
                      timedOut={entry.timedOut} durationMs={entry.durationMs}
                    />
                  );
                  case 'shell_stop': return (
                    <div key={entry.id} style={{ fontSize: 12, color: entry.stopped ? 'var(--green)' : 'var(--red)', padding: '2px 0' }}>
                      {entry.stopped ? '■ stopped' : '✗ stop failed'} {entry.handle}{entry.pid ? ` (pid ${entry.pid})` : ''}{entry.message ? ` — ${entry.message}` : ''}
                    </div>
                  );
                  case 'error': return <ErrorEntry key={entry.id} message={entry.message} />;
                  case 'complete': return <CompleteEntry key={entry.id} summary={entry.summary} outputDir={entry.outputDir} onBrowse={() => setMainTab('files')} />;
                  case 'user_msg': return <UserMsgEntry key={entry.id} content={entry.content} />;
                  case 'assistant_msg': return <AssistantMsgEntry key={entry.id} content={entry.content} />;
                  case 'orchestrator_thinking': return <OrchestratorThinkingEntry key={entry.id} />;
                  case 'orchestrator_message': return <OrchestratorMessageEntry key={entry.id} content={entry.content} />;
                  case 'sub_agent_block': return (
                    <SubAgentBlock
                      key={entry.id}
                      taskId={entry.taskId}
                      title={entry.title}
                      status={entry.status}
                      summary={entry.summary}
                      filesWritten={entry.filesWritten}
                      blocker={entry.blocker}
                      updates={entry.updates}
                    />
                  );
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
                  placeholder={isRunning ? 'Agent is working…' : isWaiting ? 'Orchestrator is waiting for your reply…' : 'Request a change or addition…'}
                  disabled={isRunning}
                  rows={1}
                  style={{
                    flex: 1,
                    background: 'var(--bg)',
                    border: `1px solid ${isWaiting ? 'var(--accent)' : 'var(--border)'}`,
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
                    {sending ? '…' : isWaiting ? 'Reply' : 'Send'}
                  </button>
                )}
              </div>
            )}

            {/* Failed before useful context exists: start over or view audit trail */}
            {showRetryButton && (
              <div style={{ flexShrink: 0, borderTop: '1px solid var(--border)', padding: '10px 16px', background: 'var(--bg2)', display: 'flex', alignItems: 'center', gap: 12 }}>
                <button className="btn-primary" style={{ fontSize: 12 }} disabled={starting} onClick={doStart}>
                  {starting ? 'Starting…' : 'Try again →'}
                </button>
                <button
                  className="btn-ghost"
                  style={{ fontSize: 12, color: 'var(--text2)' }}
                  onClick={() => navigate(`/ideas/${id}`, { state: { tab: 'audit', skipRedirect: true } })}
                >
                  View audit trail →
                </button>
                {session.summary && (
                  <span style={{ fontSize: 12, color: 'var(--red)', marginLeft: 4 }}>{session.summary}</span>
                )}
              </div>
            )}
          </>
        )}

        {/* Files tab */}
        {mainTab === 'files' && fileCount > 0 && (
          <div style={{ flex: 1, overflow: 'hidden', padding: '12px 20px' }}>
            <FileBrowser ideaId={id!} refreshKey={fileRefreshKey} />
          </div>
        )}
      </div>
    </div>
  );
}
