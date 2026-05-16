import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
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
import { api, BASE, WS_BASE } from '../api/client';
import { useWebSocket } from '../hooks/useWebSocket';
import type { IdeaDetail, Phase3ActivityEvent, Phase3ChatMessage, Phase3DirEntry, Phase3Session, PipelineEvent } from '../types';
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

// ---------------------------------------------------------------------------
// Activity log entry types
// ---------------------------------------------------------------------------

type SubAgentUpdate = { updateType: string; detail: string; issues?: string[] };

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
  | { kind: 'orchestrator_streaming'; id: number; content: string }
  | { kind: 'orchestrator_message'; id: number; content: string }
  | { kind: 'sub_agent_block'; id: number; taskId: string; agentId?: string; title: string; status: 'queued' | 'running' | 'done' | 'blocked' | 'cancelled'; summary: string; filesWritten: string[]; blocker: string | null; updates: SubAgentUpdate[]; streamingText?: string }
  | { kind: 'plan_warnings'; id: number; warnings: string[] }
  | { kind: 'syntax_check'; id: number; path: string; passed: boolean; error: string; retrying: boolean };

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

function CompleteEntry({ summary, outputDir, onBrowse, isPrdOnly }: { summary: string; outputDir: string; onBrowse: () => void; isPrdOnly?: boolean }) {
  return (
    <div style={{
      margin: '12px 0 4px',
      padding: '14px 16px',
      background: '#0e1a0e',
      border: '1px solid #1a3a1a',
      borderRadius: 8,
    }}>
      <p style={{ fontSize: 13, color: 'var(--green)', fontWeight: 600, marginBottom: 6 }}>
        {isPrdOnly ? '✓ PRD generated' : '✓ Implementation complete'}
      </p>
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
      <button className="btn-ghost" style={{ fontSize: 12 }} onClick={onBrowse}>
        {isPrdOnly ? 'View PRD →' : 'Browse files →'}
      </button>
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

function OrchestratorStreamingEntry({ content }: { content: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '8px 0', color: 'var(--text2)' }}>
      <div style={{ display: 'flex', gap: 3, marginTop: 2 }}>
        <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
      </div>
      <span style={{ fontSize: 12, fontStyle: 'italic', maxWidth: '80%', wordBreak: 'break-word', whiteSpace: 'pre-wrap' }}>
        {content}
      </span>
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

function PlanWarningsEntry({ warnings }: { warnings: string[] }) {
  return (
    <div style={{ margin: '8px 0', padding: '10px 14px', background: '#1a1500', border: '1px solid #4a3800', borderRadius: 6 }}>
      <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--yellow)', marginBottom: 6 }}>
        ⚠ Structural issues detected in file plan — will attempt to continue
      </div>
      {warnings.map((w, i) => (
        <div key={i} style={{ fontSize: 11, color: 'var(--yellow)', opacity: 0.85, padding: '2px 0', fontFamily: 'monospace', wordBreak: 'break-word' }}>
          · {w}
        </div>
      ))}
    </div>
  );
}

function SyntaxCheckEntry({ path, passed, error, retrying }: { path: string; passed: boolean; error: string; retrying: boolean }) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '3px 0 3px 16px', fontSize: 11 }}>
      <span style={{ color: passed ? 'var(--green)' : 'var(--red)', fontWeight: 700, flexShrink: 0 }}>
        {passed ? '✓ syntax OK' : retrying ? '⟳ syntax error — retrying' : '⚠ syntax error persists'}
      </span>
      {!passed && error && (
        <code style={{ color: 'var(--red)', fontFamily: 'monospace', opacity: 0.8, wordBreak: 'break-all' }}>{error.split('\n')[0]}</code>
      )}
    </div>
  );
}

const UPDATE_ICONS: Record<string, string> = {
  file_edit: '✓',
  delete_path: '🗑',
  run_shell: '$',
  list_files: '📂',
  read_file: '📄',
  grep_files: '🔍',
  web_search: '🌐',
  verify: '⚑',
};

function TaskBlock({ taskId: _taskId, agentId, title, status, summary, filesWritten, blocker, updates, streamingText, onStop }: {
  taskId: string; agentId?: string; title: string; status: 'queued' | 'running' | 'done' | 'blocked' | 'cancelled';
  summary: string; filesWritten: string[]; blocker: string | null; updates: SubAgentUpdate[]; streamingText?: string;
  onStop?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const isRunning = status === 'running';
  const isQueued = status === 'queued';

  const statusColor = status === 'done' ? 'var(--green)' : status === 'blocked' ? 'var(--yellow)' : status === 'cancelled' ? 'var(--text2)' : 'var(--text2)';
  const statusIcon = status === 'done' ? '✓' : status === 'blocked' ? '⚠' : status === 'cancelled' ? '⊘' : isQueued ? '⏳' : null;

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
          opacity: isQueued ? 0.5 : 1,
        }}
      >
        <span style={{ color: statusColor, fontWeight: 700, flexShrink: 0, fontSize: 13, minWidth: 16 }}>
          {isRunning ? (
            <span style={{ display: 'flex', gap: 2 }}>
              <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
            </span>
          ) : statusIcon}
        </span>
        <span style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {title}
          </span>
          {agentId && (
            <span style={{ fontSize: 10, color: 'var(--text2)', fontFamily: 'monospace', display: 'block' }}>
              #{agentId}
            </span>
          )}
          {isRunning && streamingText && !expanded && (
            <span style={{ fontSize: 11, color: 'var(--text2)', display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontStyle: 'italic', marginTop: 1 }}>
              {streamingText.slice(-120)}
            </span>
          )}
        </span>
        {filesWritten.length > 0 && (
          <span style={{ fontSize: 11, color: 'var(--text2)', flexShrink: 0 }}>
            {filesWritten.length}f
          </span>
        )}
        {isRunning && onStop && (
          <button
            onClick={(e) => { e.stopPropagation(); onStop(); }}
            style={{
              background: 'none', border: '1px solid var(--red)', color: 'var(--red)',
              borderRadius: 4, padding: '2px 7px', fontSize: 11, cursor: 'pointer', flexShrink: 0,
              lineHeight: 1.4,
            }}
          >
            Stop
          </button>
        )}
        <span style={{ fontSize: 10, color: 'var(--text2)', flexShrink: 0 }}>
          {expanded ? '▲' : '▼'}
        </span>
      </div>

      {/* Expanded body */}
      {expanded && (
        <>
          {!isRunning && summary && (
            <div style={{ padding: '0 12px 6px', fontSize: 12, color: 'var(--text2)', borderTop: '1px solid var(--border)' }}>
              {blocker && <span style={{ color: 'var(--yellow)', marginRight: 6 }}>⚠ Blocker:</span>}
              {blocker || summary}
            </div>
          )}
        </>
      )}

      {/* Expanded activity */}
      {expanded && updates.length > 0 && (
        <div style={{ borderTop: '1px solid var(--border)', padding: '6px 12px', maxHeight: 240, overflowY: 'auto' }}>
          {updates.map((u, i) => {
            if (u.updateType === 'message') {
              return (
                <div key={i} style={{ padding: '4px 0', fontSize: 11, color: 'var(--text)', fontStyle: 'italic', lineHeight: 1.5, wordBreak: 'break-word' }}>
                  {u.detail}
                </div>
              );
            }
            if (u.updateType === 'verify') {
              const hasIssues = u.issues && u.issues.length > 0;
              return (
                <div key={i} style={{ padding: '3px 0' }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, fontSize: 11 }}>
                    <span style={{ color: hasIssues ? 'var(--red)' : 'var(--green)', flexShrink: 0 }}>⚑</span>
                    <code style={{ color: hasIssues ? 'var(--red)' : 'var(--green)', fontFamily: 'monospace' }}>{u.detail}</code>
                  </div>
                  {hasIssues && (
                    <div style={{ marginLeft: 18, marginTop: 2 }}>
                      {u.issues!.map((issue, j) => (
                        <div key={j} style={{ fontSize: 11, color: 'var(--red)', opacity: 0.85, fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-word', padding: '1px 0', borderLeft: '2px solid var(--red)', paddingLeft: 6, marginBottom: 2 }}>
                          {issue}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            }
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

type SubAgentBlockEntry = Extract<ActivityEntry, { kind: 'sub_agent_block' }>;

function CompletedGroup({ tasks, allSucceeded, allTotal, onStop }: {
  tasks: SubAgentBlockEntry[];
  allSucceeded: number;
  allTotal: number;
  onStop?: (taskId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div style={{ margin: '4px 0' }}>
      <div
        onClick={() => setExpanded(e => !e)}
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px', cursor: 'pointer', userSelect: 'none',
          background: 'var(--bg2)', borderRadius: 8,
          border: '1px solid var(--border)',
        }}
      >
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.05em', flex: 1 }}>
          Completed
        </span>
        <span style={{ fontSize: 11, color: allSucceeded < allTotal ? 'var(--yellow)' : 'var(--green)', fontWeight: 600, flexShrink: 0 }}>
          {allSucceeded}/{allTotal}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text2)', flexShrink: 0 }}>{expanded ? '▲' : '▼'}</span>
      </div>
      {expanded && tasks.map(t => (
        <TaskBlock
          key={t.id}
          taskId={t.taskId}
          agentId={t.agentId}
          title={t.title}
          status={t.status}
          summary={t.summary}
          filesWritten={t.filesWritten}
          blocker={t.blocker}
          updates={t.updates}
          onStop={t.status === 'running' && onStop ? () => onStop(t.taskId) : undefined}
        />
      ))}
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

function entriesToNodes(entries: Phase3DirEntry[]): TreeNode[] {
  const nodes: TreeNode[] = entries.map(e => ({
    name: e.path.split('/').pop()!,
    fullPath: e.path,
    type: e.type,
    children: [],
    size: e.type === 'file' ? e.size : undefined,
  }));
  nodes.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  return nodes;
}

function insertChildren(nodes: TreeNode[], parentPath: string, children: TreeNode[]): TreeNode[] {
  return nodes.map(n => {
    if (n.fullPath === parentPath) return { ...n, children };
    if (n.type === 'dir' && n.children.length > 0) return { ...n, children: insertChildren(n.children, parentPath, children) };
    return n;
  });
}

function findNode(nodes: TreeNode[], path: string): TreeNode | null {
  for (const n of nodes) {
    if (n.fullPath === path) return n;
    if (n.type === 'dir') { const f = findNode(n.children, path); if (f) return f; }
  }
  return null;
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

function TreeNodeRow({ node, depth, selectedPath, collapsed, loadingDirs, onToggle, onSelect }: {
  node: TreeNode; depth: number; selectedPath: string | null;
  collapsed: Set<string>; loadingDirs: Set<string>;
  onToggle: (p: string) => void; onSelect: (p: string) => void;
}) {
  const isCollapsed = collapsed.has(node.fullPath);
  const isLoadingChildren = loadingDirs.has(node.fullPath);
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
        {!isCollapsed && (
          isLoadingChildren
            ? <div style={{ paddingLeft: indent + 19, padding: `3px 0 3px ${indent + 19}px`, fontSize: 11, color: 'var(--text2)', fontStyle: 'italic' }}>Loading…</div>
            : node.children.map(child => (
                <TreeNodeRow key={child.fullPath} node={child} depth={depth + 1}
                  selectedPath={selectedPath} collapsed={collapsed} loadingDirs={loadingDirs}
                  onToggle={onToggle} onSelect={onSelect} />
              ))
        )}
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

  const lines = content.endsWith('\n') ? content.slice(0, -1).split('\n') : content.split('\n');
  const lineCount = lines.length;
  const gutterWidth = `${String(lineCount).length + 1}ch`;
  const lineNums = Array.from({ length: lineCount }, (_, i) => i + 1).join('\n');

  return (
    <pre style={{ margin: 0, padding: 0, background: 'transparent', overflowX: 'auto', display: 'flex' }}>
      <div style={{
        padding: '14px 10px 14px 14px',
        fontSize: 12, lineHeight: 1.6, fontFamily: 'monospace',
        color: 'var(--text3)', userSelect: 'none', textAlign: 'right',
        minWidth: gutterWidth, whiteSpace: 'pre', flexShrink: 0,
        borderRight: '1px solid var(--border)',
      }}>
        {lineNums}
      </div>
      <code
        className={`hljs language-${lang ?? 'plaintext'}`}
        style={{ display: 'block', padding: '14px', fontSize: 12, lineHeight: 1.6, fontFamily: 'monospace', flex: 1, minWidth: 0 }}
        dangerouslySetInnerHTML={{ __html: highlighted }}
      />
    </pre>
  );
}

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif']);

function isImagePath(path: string): boolean {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  return IMAGE_EXTENSIONS.has(ext);
}

function FileBrowser({ ideaId, refreshKey }: { ideaId: string; refreshKey: number }) {
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [outputDir, setOutputDir] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string>('');
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingFile, setLoadingFile] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [copied, setCopied] = useState(false);
  const [dirFileCounts, setDirFileCounts] = useState<Map<string, number>>(new Map());
  const [dirSizes, setDirSizes] = useState<Map<string, number>>(new Map());
  const fileCount = Array.from(dirFileCounts.values()).reduce((a, b) => a + b, 0);
  const totalSize = Array.from(dirSizes.values()).reduce((a, b) => a + b, 0);
  const [terminalOpening, setTerminalOpening] = useState(false);
  const [explorerOpening, setExplorerOpening] = useState(false);
  const loadedDirsRef = useRef<Set<string>>(new Set());

  const loadDir = useCallback(async (dirPath: string) => {
    setLoadingDirs(prev => new Set([...prev, dirPath]));
    try {
      const data = await api.listPhase3Dir(ideaId, dirPath);
      if (dirPath === '') setOutputDir(data.output_dir ?? null);

      const children = entriesToNodes(data.entries);

      if (dirPath === '') {
        setTree(children);
      } else {
        setTree(prev => insertChildren(prev, dirPath, children));
      }
      loadedDirsRef.current.add(dirPath);

      const newFiles = data.entries.filter(e => e.type === 'file');
      setDirFileCounts(prev => new Map(prev).set(dirPath, newFiles.length));
      setDirSizes(prev => new Map(prev).set(dirPath, newFiles.reduce((s, e) => s + e.size, 0)));

      // Collapse new dirs that haven't been loaded yet
      setCollapsed(prev => {
        const next = new Set(prev);
        for (const e of data.entries) {
          if (e.type === 'dir' && !loadedDirsRef.current.has(e.path)) next.add(e.path);
        }
        return next;
      });
    } catch {}
    finally {
      setLoadingDirs(prev => { const next = new Set(prev); next.delete(dirPath); return next; });
    }
  }, [ideaId]);

  // Reset selection when switching ideas
  useEffect(() => {
    setSelectedPath(null);
    setFileContent('');
    setImageUrl(prev => { if (prev) URL.revokeObjectURL(prev); return null; });
  }, [ideaId]);

  useEffect(() => {
    const prevLoaded = new Set(loadedDirsRef.current);
    setTree([]);
    setDirFileCounts(new Map());
    setDirSizes(new Map());
    setCollapsed(new Set());
    loadedDirsRef.current = new Set();

    if (prevLoaded.size === 0) {
      setLoading(true);
      loadDir('').finally(() => setLoading(false));
    } else {
      // Reload all previously-loaded dirs shallowest-first so parent nodes exist before children
      const sorted = [...prevLoaded].sort((a, b) => {
        const da = a === '' ? 0 : a.split('/').length;
        const db = b === '' ? 0 : b.split('/').length;
        return da - db;
      });
      (async () => { for (const dir of sorted) await loadDir(dir); })();
    }
  }, [ideaId, refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setImageUrl(prev => { if (prev) URL.revokeObjectURL(prev); return null; });
    if (!selectedPath || !isImagePath(selectedPath)) return;
    setLoadingFile(true);
    fetch(`${BASE}/ideas/${ideaId}/phase3/file/raw?path=${encodeURIComponent(selectedPath)}`)
      .then(r => r.blob())
      .then(blob => setImageUrl(URL.createObjectURL(blob)))
      .catch(() => setImageUrl(null))
      .finally(() => setLoadingFile(false));
    return () => setImageUrl(prev => { if (prev) URL.revokeObjectURL(prev); return null; });
  }, [selectedPath, ideaId]);

  useEffect(() => {
    if (!selectedPath || isImagePath(selectedPath)) return;
    setLoadingFile(true);
    setFileContent('');
    api.getPhase3File(ideaId, selectedPath)
      .then(d => { setFileContent(d.content); setTruncated(d.truncated); })
      .catch(() => setFileContent('(failed to load file)'))
      .finally(() => setLoadingFile(false));
  }, [selectedPath, ideaId]);

  const toggleDir = useCallback((path: string) => {
    setCollapsed(prev => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
        if (!loadedDirsRef.current.has(path)) loadDir(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, [loadDir]);

  const selectFile = useCallback((path: string) => {
    setSelectedPath(path);
    const parts = path.split('/');
    if (parts.length > 1) {
      setCollapsed(prev => {
        const next = new Set(prev);
        for (let i = 1; i < parts.length; i++) next.delete(parts.slice(0, i).join('/'));
        return next;
      });
    }
  }, []);

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

  const handleOpenTerminal = async () => {
    setTerminalOpening(true);
    try { await api.openTerminal(ideaId); } catch {}
    finally { setTerminalOpening(false); }
  };

  const handleOpenExplorer = async () => {
    setExplorerOpening(true);
    try { await api.openExplorer(ideaId); } catch {}
    finally { setExplorerOpening(false); }
  };

  if (loading) return <p style={{ color: 'var(--text2)', padding: '12px 0', fontSize: 13 }}>Loading files…</p>;

  if (tree.length === 0) {
    return (
      <div className="card" style={{ padding: '32px 24px', textAlign: 'center' }}>
        <p style={{ color: 'var(--text2)', fontSize: 13 }}>No files found in output directory.</p>
      </div>
    );
  }

  const selectedNode = selectedPath ? findNode(tree, selectedPath) : null;

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
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 10px 8px 14px' }}>
          <p style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.06em', margin: 0 }}>
            {fileCount} file{fileCount !== 1 ? 's' : ''} · {totalSizeLabel}
          </p>
          <div style={{ display: 'flex', gap: 4 }}>
            <button
              onClick={handleOpenExplorer}
              disabled={explorerOpening}
              title="Open folder in file explorer"
              style={{
                fontSize: 11, color: 'var(--text2)', padding: '2px 6px',
                borderRadius: 4, border: '1px solid var(--border)',
                background: 'none', cursor: 'pointer', lineHeight: '18px',
                opacity: explorerOpening ? 0.5 : 1,
              }}
            >
              📁
            </button>
            <button
              onClick={handleOpenTerminal}
              disabled={terminalOpening}
              title="Open terminal in project folder"
              style={{
                fontSize: 11, color: 'var(--text2)', padding: '2px 6px',
                borderRadius: 4, border: '1px solid var(--border)',
                background: 'none', cursor: 'pointer', lineHeight: '18px',
                opacity: terminalOpening ? 0.5 : 1,
              }}
            >
              &gt;_
            </button>
          </div>
        </div>
        {tree.map(node => (
          <TreeNodeRow key={node.fullPath} node={node} depth={0}
            selectedPath={selectedPath} collapsed={collapsed} loadingDirs={loadingDirs}
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
            {selectedNode?.size !== undefined && (
              <span style={{ fontSize: 11, color: 'var(--text2)' }}>
                {(selectedNode.size / 1024).toFixed(1)} KB
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
            {!(selectedPath && isImagePath(selectedPath)) && (
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
            )}
          </div>
        </div>

        {/* Content area */}
        <div style={{ flex: 1, overflowY: 'auto', position: 'relative' }}>
          {!selectedPath ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
              <p style={{ color: 'var(--text2)', fontSize: 13 }}>Select a file to view its contents</p>
            </div>
          ) : loadingFile ? (
            <p style={{ color: 'var(--text2)', padding: '16px', fontSize: 12 }}>Loading…</p>
          ) : (
            <>
              {truncated && (
                <div style={{ padding: '6px 14px', background: '#2a2a14', borderBottom: '1px solid var(--border)', fontSize: 11, color: 'var(--yellow)' }}>
                  ⚠ File truncated at 256 KB for display
                </div>
              )}
              {selectedPath && isImagePath(selectedPath) ? (
                <div style={{ padding: '24px', display: 'flex', justifyContent: 'center' }}>
                  {imageUrl && (
                    <img
                      src={imageUrl}
                      alt={selectedPath}
                      style={{ maxWidth: '100%', maxHeight: 'calc(100vh - 320px)', objectFit: 'contain', borderRadius: 4 }}
                    />
                  )}
                </div>
              ) : (selectedPath ?? '').endsWith('.md') ? (
                <div className="markdown" style={{ padding: '20px 24px', fontSize: 13 }}>
                  <ReactMarkdown>{fileContent}</ReactMarkdown>
                </div>
              ) : (
                <HighlightedCode content={fileContent} filename={selectedPath ?? ''} />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Wake lock — keep screen on while the agent is running
// ---------------------------------------------------------------------------

function useWakeLock(active: boolean) {
  const activeRef = useRef(active);
  useEffect(() => { activeRef.current = active; }, [active]);

  const lockRef = useRef<WakeLockSentinel | null>(null);

  const acquire = useCallback(async () => {
    if (!('wakeLock' in navigator) || !activeRef.current) return;
    // Already holding a live lock — nothing to do
    if (lockRef.current && !lockRef.current.released) return;
    try {
      const sentinel = await navigator.wakeLock.request('screen');
      lockRef.current = sentinel;
      // Re-acquire whenever the browser forcibly releases the lock (tab hidden,
      // device idle policy, etc.) — without this the screen sleeps after the
      // first release even though we still want it on.
      sentinel.addEventListener('release', () => {
        lockRef.current = null;
        if (activeRef.current) setTimeout(acquire, 500);
      }, { once: true });
    } catch { /* permission denied or unsupported — silently ignore */ }
  }, []); // stable — only accesses refs internally

  useEffect(() => {
    if (active) {
      acquire();
    } else {
      lockRef.current?.release().catch(() => {});
      lockRef.current = null;
    }
    return () => {
      lockRef.current?.release().catch(() => {});
      lockRef.current = null;
    };
  }, [active, acquire]);

  // Also re-acquire when the tab becomes visible again (e.g. user switched tabs)
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') acquire();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
  }, [acquire]);
}

// ---------------------------------------------------------------------------
// Reset menu
// ---------------------------------------------------------------------------

function ResetMenu({ disabled, onReset }: {
  disabled: boolean;
  onReset: (depth: 'phase3_only' | 'resolution' | 'conversation', deleteOutputDir: boolean) => void;
}) {
  const [open, setOpen] = useState(false);

  const items: { label: string; sub: string; depth: 'phase3_only' | 'resolution' | 'conversation' }[] = [
    { label: 'Re-run implementation', sub: 'Keep Q&A decisions, redo code gen', depth: 'phase3_only' },
    { label: 'Redo resolution summary', sub: 'Keep conversation, clear decisions', depth: 'resolution' },
    { label: 'Restart Q&A from scratch', sub: 'Delete conversation and decisions', depth: 'conversation' },
  ];

  return (
    <div style={{ position: 'relative' }}>
      <button
        className="btn-ghost"
        style={{ fontSize: 11, padding: '3px 10px', color: 'var(--text2)' }}
        disabled={disabled}
        onClick={() => setOpen(o => !o)}
      >
        {disabled ? 'Resetting…' : 'Reset ▾'}
      </button>
      {open && (
        <>
          <div style={{ position: 'fixed', inset: 0, zIndex: 99 }} onClick={() => setOpen(false)} />
          <div style={{
            position: 'absolute', top: '100%', right: 0, zIndex: 100,
            background: 'var(--bg2)', border: '1px solid var(--border)',
            borderRadius: 8, boxShadow: '0 4px 16px rgba(0,0,0,0.2)',
            minWidth: 260, padding: '6px 0',
          }}>
            {items.map(item => (
              <button
                key={item.depth}
                onClick={() => { setOpen(false); onReset(item.depth, false); }}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  background: 'none', border: 'none', cursor: 'pointer',
                  padding: '8px 14px', color: 'var(--text)',
                }}
                onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg3, var(--bg))')}
                onMouseLeave={e => (e.currentTarget.style.background = 'none')}
              >
                <div style={{ fontSize: 12, fontWeight: 600 }}>{item.label}</div>
                <div style={{ fontSize: 11, color: 'var(--text2)' }}>{item.sub}</div>
              </button>
            ))}
            <div style={{ borderTop: '1px solid var(--border)', margin: '4px 0' }} />
            <button
              onClick={() => { setOpen(false); onReset('phase3_only', true); }}
              style={{
                display: 'block', width: '100%', textAlign: 'left',
                background: 'none', border: 'none', cursor: 'pointer',
                padding: '8px 14px', color: 'var(--red)',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg3, var(--bg))')}
              onMouseLeave={e => (e.currentTarget.style.background = 'none')}
            >
              <div style={{ fontSize: 12, fontWeight: 600 }}>Re-run + delete generated files</div>
              <div style={{ fontSize: 11, color: 'var(--text2)' }}>Removes output folder from disk</div>
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Phase 3 sidebar
// ---------------------------------------------------------------------------

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)}m`;
  if (ms >= 1000)  return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

function fmtTokens(n: number | null | undefined): string {
  if (n == null || n === 0) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
}

function SidebarLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>
      {children}
    </div>
  );
}

const sidebarPill: React.CSSProperties = {
  fontSize: 10,
  padding: '2px 6px',
  borderRadius: 4,
  background: 'var(--bg2)',
  border: '1px solid var(--border)',
  fontVariantNumeric: 'tabular-nums',
};

function Phase3Sidebar({
  idea,
  session,
  log,
  progressMd,
  telemetry,
}: {
  idea: IdeaDetail;
  session: Phase3Session;
  log: ActivityEntry[];
  progressMd: string | null;
  telemetry: import('../types').TelemetrySummary | null;
}) {
  const tasks = useMemo(() => {
    if (!progressMd) return null;
    const done: string[] = [];
    const pending: string[] = [];
    for (const line of progressMd.split('\n')) {
      const m = line.match(/^\s*-\s*\[(x| )\]\s*(.+)/i);
      if (m) (m[1].toLowerCase() === 'x' ? done : pending).push(m[2].trim());
    }
    return { done, pending, total: done.length + pending.length };
  }, [progressMd]);

  const fileCount = new Set(log.filter(e => e.kind === 'file').map(e => (e as Extract<ActivityEntry, { kind: 'file' }>).path)).size;
  const shellCount = log.filter(e => e.kind === 'shell').length;
  const subAgentBlocks = log.filter(e => e.kind === 'sub_agent_block') as Extract<ActivityEntry, { kind: 'sub_agent_block' }>[];
  const doneTasks = subAgentBlocks.filter(b => b.status === 'done').length;
  const totalTasks = subAgentBlocks.length;

  return (
    <div style={{
      width: 220,
      flexShrink: 0,
      borderRight: '1px solid var(--border)',
      overflowY: 'auto',
      padding: '16px 14px',
      display: 'flex',
      flexDirection: 'column',
      gap: 16,
      fontSize: 12,
      background: 'var(--bg)',
    }}>
      {/* Idea */}
      <div>
        <SidebarLabel>Idea</SidebarLabel>
        <div style={{ fontWeight: 600, fontSize: 13, lineHeight: 1.4 }}>{idea.name}</div>
        {idea.description && (
          <div style={{ color: 'var(--text2)', fontSize: 11, marginTop: 4, lineHeight: 1.5 }}>
            {idea.description.length > 140 ? idea.description.slice(0, 140) + '…' : idea.description}
          </div>
        )}
      </div>

      <div style={{ height: 1, background: 'var(--border)' }} />

      {/* Phase 3 */}
      <div>
        <SidebarLabel>Phase 3 — Progress</SidebarLabel>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 10 }}>
          {fileCount > 0 && (
            <span style={{ ...sidebarPill, color: 'var(--green)' }}>{fileCount} files</span>
          )}
          {shellCount > 0 && (
            <span style={{ ...sidebarPill, color: 'var(--text2)' }}>{shellCount} cmds</span>
          )}
          {totalTasks > 0 && (
            <span style={{ ...sidebarPill, color: doneTasks === totalTasks ? 'var(--green)' : 'var(--text2)' }}>
              {doneTasks}/{totalTasks} tasks
            </span>
          )}
          {fileCount === 0 && shellCount === 0 && totalTasks === 0 && (
            <span style={{ fontSize: 11, color: 'var(--text2)' }}>
              {session.status === 'COMPLETE' ? 'Complete' : 'Not started yet'}
            </span>
          )}
        </div>

        {tasks && tasks.total > 0 && (
          <>
            <div style={{ marginBottom: 8 }}>
              <div style={{ height: 3, borderRadius: 2, background: 'var(--border)', overflow: 'hidden' }}>
                <div style={{
                  height: '100%',
                  width: `${Math.round(tasks.done.length / tasks.total * 100)}%`,
                  background: 'var(--green)',
                  borderRadius: 2,
                  transition: 'width 0.4s',
                }} />
              </div>
              <div style={{ fontSize: 10, color: 'var(--text2)', marginTop: 3 }}>
                {tasks.done.length}/{tasks.total} done
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {[
                ...tasks.done.map(t => ({ t, done: true })),
                ...tasks.pending.map(t => ({ t, done: false })),
              ].slice(0, 14).map(({ t, done }, i) => (
                <div key={i} style={{ display: 'flex', gap: 5, alignItems: 'flex-start' }}>
                  <span style={{ color: done ? 'var(--green)' : 'var(--border)', flexShrink: 0, fontSize: 11, marginTop: 1 }}>
                    {done ? '✓' : '○'}
                  </span>
                  <span style={{ fontSize: 11, lineHeight: 1.4, color: done ? 'var(--text2)' : 'var(--text)', textDecoration: done ? 'line-through' : 'none', opacity: done ? 0.65 : 1 }}>
                    {t}
                  </span>
                </div>
              ))}
              {tasks.total > 14 && (
                <div style={{ fontSize: 10, color: 'var(--text2)' }}>+{tasks.total - 14} more</div>
              )}
            </div>
          </>
        )}
      </div>

      {telemetry && telemetry.total_calls > 0 && (
        <>
          <div style={{ height: 1, background: 'var(--border)' }} />
          <div>
            <SidebarLabel>This idea — Telemetry</SidebarLabel>
            {/* Key stats row */}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
              <span style={{ ...sidebarPill }}>
                {telemetry.total_calls} calls
              </span>
              <span style={{
                ...sidebarPill,
                color: telemetry.total_calls === 0 ? 'var(--text2)'
                  : (telemetry.by_model.reduce((s, m) => s + m.success, 0) / telemetry.total_calls) >= 0.9 ? 'var(--green)'
                  : (telemetry.by_model.reduce((s, m) => s + m.success, 0) / telemetry.total_calls) >= 0.7 ? 'var(--yellow)'
                  : 'var(--red)',
              }}>
                {Math.round(telemetry.by_model.reduce((s, m) => s + m.success, 0) / telemetry.total_calls * 100)}% ok
              </span>
              {(() => {
                const all = telemetry.by_model.filter(m => m.avg_duration_ms != null);
                if (!all.length) return null;
                const wavg = Math.round(all.reduce((s, m) => s + (m.avg_duration_ms ?? 0) * m.calls, 0) / all.reduce((s, m) => s + m.calls, 0));
                return <span style={{ ...sidebarPill, color: 'var(--text2)' }}>{fmtMs(wavg)} avg</span>;
              })()}
              {telemetry.total_tokens > 0 && (
                <span style={{ ...sidebarPill, color: 'var(--text2)' }} title={`${fmtTokens(telemetry.total_tokens_prompt)} prompt / ${fmtTokens(telemetry.total_tokens_completion)} completion`}>
                  {fmtTokens(telemetry.total_tokens)} tokens
                </span>
              )}
            </div>
            {/* All models */}
            {telemetry.by_model.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {telemetry.by_model.map((m, i) => {
                  const name = m.model.includes('/') ? m.model.split('/').pop()! : m.model;
                  const pct = Math.round(m.success_rate * 100);
                  const failures = m.calls - m.success;
                  return (
                    <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 4 }}>
                        <span style={{ fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }} title={m.model}>
                          {name}
                        </span>
                        <span style={{ fontSize: 11, color: pct >= 90 ? 'var(--green)' : pct >= 70 ? 'var(--yellow)' : 'var(--red)', flexShrink: 0, fontVariantNumeric: 'tabular-nums' }}>
                          {pct}%
                        </span>
                      </div>
                      <div style={{ display: 'flex', gap: 6, fontSize: 10, color: 'var(--text2)' }}>
                        <span>{m.calls} calls</span>
                        {failures > 0 && <span style={{ color: 'var(--red)' }}>{failures} failed</span>}
                        {m.fallbacks > 0 && <span style={{ color: 'var(--text2)' }}>{m.fallbacks} as fallback</span>}
                        {m.avg_duration_ms != null && <span style={{ marginLeft: 'auto' }}>{fmtMs(m.avg_duration_ms)}</span>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </>
      )}
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
  const [activityVisible, setActivityVisible] = useState(15);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [mainTab, setMainTab] = useState<MainTab>('log');
  const [chatInput, setChatInput] = useState('');
  const [sending, setSending] = useState(false);
  const [regenPrd, setRegenPrd] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [fileRefreshKey, setFileRefreshKey] = useState(0);
  const [selectedMode, setSelectedMode] = useState<'classic' | 'multi_agent' | 'prd_only'>('classic');
  const fileRefreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const logEndRef = useRef<HTMLDivElement>(null);

  const isActivelyRunning = session?.status === 'PLANNING' || session?.status === 'RUNNING' || session?.status === 'WAITING';
  useWakeLock(isActivelyRunning);

  const phase2Q = useQuery({
    queryKey: ['phase2', id],
    queryFn: () => api.getPhase2(id!),
    enabled: !!id,
    staleTime: 60_000,
  });

  const progressQ = useQuery({
    queryKey: ['phase3-progress', id],
    queryFn: () => api.getPhase3File(id!, 'docs/PROGRESS.md'),
    enabled: !!id && !!session?.project_root && session?.mode === 'multi_agent',
    refetchInterval: (query) => query.state.error || !isActivelyRunning ? false : 60_000,
    retry: false,
  });

  const telemetryQ = useQuery({
    queryKey: ['telemetry-idea', id],
    queryFn: () => api.getTelemetrySummary({ project_id: id }),
    enabled: !!id && !!session,
    refetchInterval: isActivelyRunning ? 60_000 : 300_000,
    staleTime: 30_000,
  });

  const addEntry = (entry: ActivityEntry) =>
    setLog(prev => {
      if (entry.kind === 'thinking') {
        if (prev.some(e => e.kind === 'thinking')) return prev;
        return [...prev, entry];
      }
      if (entry.kind === 'orchestrator_thinking') {
        // New round: clear previous thinking spinner and any streaming content
        return [...prev.filter(e => e.kind !== 'orchestrator_thinking' && e.kind !== 'orchestrator_streaming' && e.kind !== 'thinking'), entry];
      }
      if (entry.kind === 'tool_use') {
        return [...prev.filter(e => e.kind !== 'tool_use' && e.kind !== 'thinking'), entry];
      }
      if (entry.kind === 'writing') {
        return [...prev.filter(e => e.kind !== 'writing' && e.kind !== 'tool_use' && e.kind !== 'thinking'), entry];
      }
      if (entry.kind === 'orchestrator_message') {
        // Complete message replaces thinking spinner + any streaming preview
        return [...prev.filter(e => e.kind !== 'orchestrator_thinking' && e.kind !== 'orchestrator_streaming' && e.kind !== 'thinking' && e.kind !== 'tool_use'), entry];
      }
      if (entry.kind === 'sub_agent_block') {
        const filtered = prev.filter(e => e.kind !== 'orchestrator_thinking' && e.kind !== 'orchestrator_streaming' && e.kind !== 'thinking' && e.kind !== 'tool_use');
        // If a block for this task already exists, skip — updateSubAgentBlock handles mutations
        if (filtered.some(e => e.kind === 'sub_agent_block' && e.taskId === entry.taskId)) return filtered;
        return [...filtered, entry];
      }
      return [...prev.filter(e => e.kind !== 'thinking' && e.kind !== 'tool_use'), entry];
    });

  const updateSubAgentBlock = (taskId: string, updater: (e: Extract<ActivityEntry, { kind: 'sub_agent_block' }>) => Extract<ActivityEntry, { kind: 'sub_agent_block' }>) =>
    setLog(prev => prev.map(e => e.kind === 'sub_agent_block' && e.taskId === taskId ? updater(e) : e));

  // Like updateSubAgentBlock but creates a new block if none exists for this taskId.
  // Needed when page loads mid-run and misses the queued/started WS events.
  const upsertSubAgentBlock = (
    taskId: string,
    updater: (e: Extract<ActivityEntry, { kind: 'sub_agent_block' }>) => Extract<ActivityEntry, { kind: 'sub_agent_block' }>,
    factory: () => Extract<ActivityEntry, { kind: 'sub_agent_block' }>,
  ) =>
    setLog(prev => {
      if (prev.some(e => e.kind === 'sub_agent_block' && e.taskId === taskId)) {
        return prev.map(e => e.kind === 'sub_agent_block' && e.taskId === taskId ? updater(e) : e);
      }
      const newBlock = factory();
      return [...prev.filter(e => e.kind !== 'orchestrator_thinking' && e.kind !== 'orchestrator_streaming' && e.kind !== 'thinking' && e.kind !== 'tool_use'), newBlock];
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

        // Pre-scan to know which tasks reached started/completed — used to
        // reconstruct in-flight tasks without creating duplicate blocks.
        const completedTaskIds = new Set<string>();
        const startedTaskIds = new Set<string>();
        for (const e of events) {
          if (e.event_type === 'sub_agent_complete' || e.event_type === 'sub_agent_fix_complete' || e.event_type === 'sub_agent_cancelled') completedTaskIds.add(e.payload.task_id as string);
          if (e.event_type === 'sub_agent_started' || e.event_type === 'sub_agent_fix_started') startedTaskIds.add(e.payload.task_id as string);
        }

        // Build activity entries from persisted events
        const rawActivityEntries: (ActivityEntry & { ts: string })[] = events.flatMap((e: Phase3ActivityEvent): (ActivityEntry & { ts: string })[] => {
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
          } else if (e.event_type === 'sub_agent_queued') {
            const taskId = e.payload.task_id as string;
            if (startedTaskIds.has(taskId) || completedTaskIds.has(taskId)) return [];
            return [{ kind: 'sub_agent_block', id: nextId(), taskId, agentId: e.payload.agent_id as string | undefined, title: (e.payload.title as string) || `Task ${taskId}`, status: 'queued', summary: '', filesWritten: [], blocker: null, updates: [], ts: e.created_at }];
          } else if (e.event_type === 'sub_agent_started') {
            const taskId = e.payload.task_id as string;
            if (completedTaskIds.has(taskId)) return [];
            return [{ kind: 'sub_agent_block', id: nextId(), taskId, agentId: e.payload.agent_id as string | undefined, title: (e.payload.title as string) || `Task ${taskId}`, status: 'running', summary: '', filesWritten: [], blocker: null, updates: [], ts: e.created_at }];
          } else if (e.event_type === 'sub_agent_complete' || e.event_type === 'sub_agent_fix_complete') {
            const success = e.payload.success as boolean;
            const blocker = (e.payload.blocker as string) ?? null;
            return [{ kind: 'sub_agent_block', id: nextId(), taskId: e.payload.task_id as string, agentId: e.payload.agent_id as string | undefined, title: (e.payload.title as string) || `Task ${e.payload.task_id}`, status: success && !blocker ? 'done' : 'blocked', summary: e.payload.summary as string, filesWritten: (e.payload.files_written as string[]) ?? [], blocker, updates: [], ts: e.created_at }];
          } else if (e.event_type === 'sub_agent_fix_started') {
            const taskId = e.payload.task_id as string;
            if (completedTaskIds.has(taskId)) return [];
            return [{ kind: 'sub_agent_block', id: nextId(), taskId, agentId: e.payload.agent_id as string | undefined, title: (e.payload.title as string) || `Task ${taskId}`, status: 'running', summary: '', filesWritten: [], blocker: null, updates: [], ts: e.created_at }];
          } else if (e.event_type === 'sub_agent_cancelled') {
            const taskId = e.payload.task_id as string;
            return [{ kind: 'sub_agent_block', id: nextId(), taskId, agentId: undefined, title: (e.payload.title as string) || `Task ${taskId}`, status: 'cancelled', summary: 'Cancelled by user', filesWritten: [], blocker: null, updates: [], ts: e.created_at }];
          } else {
            return [];
          }
        });

        // Deduplicate sub_agent_blocks: events are chronological so iterating
        // backward and keeping only the first-seen taskId yields the most
        // advanced state for each task (done > running > queued).
        const seenTaskIds = new Set<string>();
        const activityEntries: typeof rawActivityEntries = [];
        for (let i = rawActivityEntries.length - 1; i >= 0; i--) {
          const e = rawActivityEntries[i];
          if (e.kind === 'sub_agent_block') {
            if (seenTaskIds.has(e.taskId)) continue;
            seenTaskIds.add(e.taskId);
          }
          activityEntries.unshift(e);
        }

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

  // WebSocket — real-time activity with auto-reconnect
  useWebSocket(id ? `${WS_BASE}/ws/ideas/${id}` : null, (data: string) => {
    try {
      const event: PipelineEvent = JSON.parse(data);
      if (!event.event_type.startsWith('phase3.')) return;

      // If we receive any phase3 activity while the session shows as failed/done,
      // the orchestrator resumed after a restart — re-fetch to get the live status.
      setSession(current => {
        if (current && current.status === 'FAILED') {
          api.getPhase3(id!).then(setSession).catch(() => {});
        }
        return current;
      });

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
            // Auto-switch to PRD/files tab when prd_only finishes
            if (s.mode === 'prd_only') {
              setMainTab('files');
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

        case 'phase3.orchestrator_token': {
          const chunk = event.payload.content as string;
          if (chunk) {
            setLog(prev => {
              const existingIdx = prev.findLastIndex(e => e.kind === 'orchestrator_streaming');
              if (existingIdx !== -1) {
                const updated = [...prev];
                const existing = updated[existingIdx] as Extract<ActivityEntry, { kind: 'orchestrator_streaming' }>;
                // Keep only the last ~300 chars to avoid unbounded growth
                const combined = (existing.content + chunk).slice(-300);
                updated[existingIdx] = { ...existing, content: combined };
                return updated;
              }
              // Create a new streaming entry, replacing the thinking spinner
              return [
                ...prev.filter(e => e.kind !== 'orchestrator_thinking'),
                { kind: 'orchestrator_streaming' as const, id: nextId(), content: chunk },
              ];
            });
          }
          break;
        }

        case 'phase3.orchestrator_message':
          addEntry({
            kind: 'orchestrator_message', id: nextId(),
            content: event.payload.content as string,
          });
          break;

        case 'phase3.sub_agent_queued':
          addEntry({
            kind: 'sub_agent_block', id: nextId(),
            taskId: event.payload.task_id as string,
            agentId: event.payload.agent_id as string | undefined,
            title: event.payload.title as string,
            status: 'queued',
            summary: '',
            filesWritten: [],
            blocker: null,
            updates: [],
          });
          break;

        case 'phase3.sub_agent_started': {
          const taskId = event.payload.task_id as string;
          upsertSubAgentBlock(
            taskId,
            e => ({ ...e, status: 'running' }),
            () => ({ kind: 'sub_agent_block', id: nextId(), taskId, agentId: event.payload.agent_id as string | undefined, title: (event.payload.title as string) || `Task ${taskId}`, status: 'running', summary: '', filesWritten: [], blocker: null, updates: [] }),
          );
          break;
        }

        case 'phase3.sub_agent_model_fallback': {
          const taskId = event.payload.task_id as string;
          const model = event.payload.model as string;
          updateSubAgentBlock(taskId, e => ({
            ...e,
            updates: [...e.updates, { updateType: 'model_fallback', detail: `retrying with ${model}` }],
          }));
          break;
        }

        case 'phase3.sub_agent_token': {
          const taskId = event.payload.task_id as string;
          const chunk = event.payload.content as string;
          if (chunk) {
            updateSubAgentBlock(taskId, e => ({
              ...e,
              // Keep a rolling window of the last ~200 chars so the subtitle stays fresh
              streamingText: ((e.streamingText ?? '') + chunk).slice(-200),
            }));
          }
          break;
        }

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

        case 'phase3.sub_agent_verify_complete': {
          const taskId = event.payload.task_id as string;
          const verified = event.payload.verified as boolean;
          const issues = (event.payload.issues as string[]) ?? [];
          const detail = verified ? '✓ Verified — clean' : `⚠ ${issues.length} issue(s) found`;
          updateSubAgentBlock(taskId, e => ({
            ...e,
            updates: [...e.updates, { updateType: 'verify', detail, issues: issues.length ? issues : undefined }],
          }));
          break;
        }

        case 'phase3.sub_agent_complete': {
          const taskId = event.payload.task_id as string;
          const summary = event.payload.summary as string;
          const filesWritten = (event.payload.files_written as string[]) ?? [];
          const success = event.payload.success as boolean;
          const blocker = event.payload.blocker as string | null;
          upsertSubAgentBlock(
            taskId,
            e => ({ ...e, status: success && !blocker ? 'done' : 'blocked', summary, filesWritten, blocker, streamingText: undefined }),
            () => ({ kind: 'sub_agent_block', id: nextId(), taskId, agentId: event.payload.agent_id as string | undefined, title: (event.payload.title as string) || `Task ${taskId}`, status: success && !blocker ? 'done' : 'blocked', summary, filesWritten, blocker, updates: [], streamingText: undefined }),
          );
          // Debounce file list refresh on sub-agent completion
          if (fileRefreshTimer.current) clearTimeout(fileRefreshTimer.current);
          fileRefreshTimer.current = setTimeout(() => setFileRefreshKey(k => k + 1), 4000);
          break;
        }

        case 'phase3.sub_agent_cancelled': {
          const taskId = event.payload.task_id as string;
          const title = (event.payload.title as string) || `Task ${taskId}`;
          upsertSubAgentBlock(
            taskId,
            e => ({ ...e, status: 'cancelled', summary: 'Cancelled by user', blocker: null, streamingText: undefined }),
            () => ({ kind: 'sub_agent_block', id: nextId(), taskId, agentId: undefined, title, status: 'cancelled', summary: 'Cancelled by user', filesWritten: [], blocker: null, updates: [], streamingText: undefined }),
          );
          break;
        }

        case 'phase3.plan_warnings':
          addEntry({ kind: 'plan_warnings', id: nextId(), warnings: (event.payload.warnings as string[]) || [] });
          break;

        case 'phase3.syntax_check':
          addEntry({
            kind: 'syntax_check', id: nextId(),
            path: event.payload.path as string,
            passed: event.payload.passed as boolean,
            error: (event.payload.error as string) || '',
            retrying: event.payload.retrying as boolean,
          });
          break;
      }
    } catch { /* ignore */ }
  });

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

  const doStopTask = async (taskId: string) => {
    if (!id) return;
    try {
      await api.cancelPhase3Task(id, taskId);
    } catch {
      // best-effort — task may have already finished
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

  const doReset = async (depth: 'phase3_only' | 'resolution' | 'conversation', deleteOutputDir: boolean) => {
    if (!id) return;
    const labels: Record<string, string> = {
      phase3_only: 'This will delete the Phase 3 session and let you re-run implementation with the same Q&A decisions.',
      resolution: 'This will delete Phase 3 and clear the resolution summary. You will need to re-answer questions and regenerate the summary.',
      conversation: 'This will delete Phase 3 and the entire Phase 2 conversation. The Q&A will start completely from scratch.',
    };
    const extra = deleteOutputDir ? '\n\nGenerated files on disk will also be deleted.' : '';
    if (!window.confirm(`${labels[depth]}${extra}\n\nContinue?`)) return;
    setResetting(true);
    setError(null);
    try {
      await api.resetPhase2(id, depth, deleteOutputDir);
      if (depth === 'phase3_only') {
        // Already on Phase 3 — clear session state in-place so the start screen appears
        // without a remount (navigate to same URL is a no-op in React Router)
        setSession(null);
        setLog([]);
        setError(null);
        setResetting(false);
      } else {
        navigate(`/ideas/${id}/phase2`);
      }
    } catch (e: unknown) {
      setError(`Reset failed: ${(e as Error).message}`);
      setResetting(false);
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
          <div style={{ display: 'flex', gap: 12, marginBottom: 24, justifyContent: 'center', flexWrap: 'wrap' }}>
            {(['classic', 'multi_agent', 'prd_only'] as const).map(m => (
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
                  minWidth: 180,
                  transition: 'all 0.15s',
                }}
              >
                <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 4 }}>
                  {m === 'classic' ? '⚡ Classic' : m === 'multi_agent' ? '🧭 Multi-Agent' : '📄 PRD Only'}
                </div>
                <div style={{ fontSize: 11, lineHeight: 1.5, opacity: 0.85 }}>
                  {m === 'classic'
                    ? 'Direct generation — one file at a time, fast and predictable'
                    : m === 'multi_agent'
                    ? 'Orchestrated — planner delegates tasks to specialized sub-agents'
                    : 'Generate a standalone PRD to implement elsewhere'}
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
            ) : selectedMode === 'multi_agent' ? (
              <>
                <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Multi-agent mode</p>
                <ol style={{ fontSize: 13, color: 'var(--text2)', paddingLeft: 20, lineHeight: 1.8 }}>
                  <li>Generates the PRD first to anchor all decisions</li>
                  <li>An orchestrator reads the PRD and plans tasks autonomously</li>
                  <li>Sub-agents execute each task: writing files, running commands</li>
                  <li>The orchestrator can ask you questions if it needs input</li>
                </ol>
              </>
            ) : (
              <>
                <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>PRD Only mode</p>
                <ol style={{ fontSize: 13, color: 'var(--text2)', paddingLeft: 20, lineHeight: 1.8 }}>
                  <li>Synthesises all Phase 1 + Phase 2 context into a single document</li>
                  <li>Covers architecture, components, data models, API, tech stack (with current versions), implementation phases, and setup guide</li>
                  <li>Searches the web to verify library versions before writing</li>
                  <li>Output is a standalone PRD.md ready to hand to any developer or AI tool</li>
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
            {starting ? 'Starting…' : selectedMode === 'prd_only' ? 'Generate PRD' : 'Begin Implementation'}
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
  const isPrdOnly = session?.mode === 'prd_only';
  const isMultiAgent = session?.mode === 'multi_agent';
  const showChatInput = !isPrdOnly && (isComplete || isFailed || isWaiting ||
    (isRunning && (isMultiAgent || log.some(e => e.kind === 'complete') || hasTimedOut)));
  const showRetryButton = isFailed && !wasCancelled && !hasActivity;

  const fileCount = new Set(log.filter(e => e.kind === 'file').map(e => (e as Extract<ActivityEntry, { kind: 'file' }>).path)).size;
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

      {/* Body: sidebar + main */}
      <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'row' }}>
        <Phase3Sidebar
          idea={idea}
          session={session}
          log={log}
          progressMd={progressQ.data?.content ?? null}
          telemetry={telemetryQ.data ?? null}
        />

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
            {(fileCount > 0 || session?.mode === 'prd_only') && (
              <button
                className={`tab ${mainTab === 'files' ? 'active' : ''}`}
                onClick={() => setMainTab('files')}
                style={{ fontSize: 12 }}
              >
                {session?.mode === 'prd_only' ? 'PRD' : 'Files'}
                {fileCount > 0 && <span style={{ marginLeft: 4, color: isComplete ? 'var(--green)' : 'var(--text2)', fontSize: 11 }}>{fileCount}</span>}
              </button>
            )}
          </div>
          {isComplete && !isRunning && !isPrdOnly && (
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
          {isRunning && (
            <button
              className="btn-ghost"
              style={{ fontSize: 11, padding: '3px 10px', color: 'var(--red)', flexShrink: 0 }}
              disabled={cancelling}
              onClick={doCancel}
            >
              {cancelling ? 'Stopping…' : '■ Stop'}
            </button>
          )}
          {(isComplete || isFailed) && !isRunning && (
            <div style={{ position: 'relative', display: 'inline-block' }}>
              <ResetMenu disabled={resetting} onReset={doReset} />
            </div>
          )}
        </div>

        {/* Activity log tab */}
        {mainTab === 'log' && session.mode === 'multi_agent' ? (
          // ── Multi-agent: two-column layout ─────────────────────────────────
          <div style={{ flex: 1, overflow: 'hidden', display: 'flex' }}>

            {/* Left: orchestrator chat */}
            <div style={{ flex: '0 0 55%', display: 'flex', flexDirection: 'column', overflow: 'hidden', borderRight: '1px solid var(--border)' }}>
              <div style={{ flex: 1, overflowY: 'auto', padding: '12px 20px' }}>
                {(isRunning || isWaiting) && log.filter(e => ['orchestrator_thinking', 'orchestrator_streaming', 'orchestrator_message', 'tool_use', 'thinking', 'user_msg', 'assistant_msg', 'error', 'complete'].includes(e.kind)).length === 0 && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text2)', fontSize: 12 }}>
                    <div style={{ display: 'flex', gap: 3 }}>
                      <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
                    </div>
                    <span>Generating PRD…</span>
                  </div>
                )}
                {(() => {
                  const _visibleKinds = new Set(['orchestrator_thinking', 'orchestrator_streaming', 'orchestrator_message', 'tool_use', 'thinking', 'user_msg', 'assistant_msg', 'error', 'complete']);
                  const _leftEntries = log.filter(e => _visibleKinds.has(e.kind));
                  const _hidden = Math.max(0, _leftEntries.length - activityVisible);
                  const _visible = _leftEntries.slice(-activityVisible);
                  return (
                    <>
                      {_hidden > 0 && (
                        <button
                          onClick={() => setActivityVisible(v => v + 15)}
                          style={{ display: 'block', width: '100%', padding: '6px 0', marginBottom: 8, fontSize: 12, color: 'var(--text2)', background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 6, cursor: 'pointer' }}
                        >
                          ↑ Load {Math.min(15, _hidden)} older messages ({_hidden} hidden)
                        </button>
                      )}
                      {_visible.map((entry) => {
                        switch (entry.kind) {
                          case 'orchestrator_thinking': return <OrchestratorThinkingEntry key={entry.id} />;
                          case 'orchestrator_streaming': return <OrchestratorStreamingEntry key={entry.id} content={entry.content} />;
                          case 'orchestrator_message': return <OrchestratorMessageEntry key={entry.id} content={entry.content} />;
                          case 'tool_use': return <ToolUseEntry key={entry.id} tool={entry.tool} detail={entry.detail} />;
                          case 'thinking': return <ThinkingEntry key={entry.id} />;
                          case 'user_msg': return <UserMsgEntry key={entry.id} content={entry.content} />;
                          case 'assistant_msg': return <AssistantMsgEntry key={entry.id} content={entry.content} />;
                          case 'error': return <ErrorEntry key={entry.id} message={entry.message} />;
                          case 'complete': return <CompleteEntry key={entry.id} summary={entry.summary} outputDir={entry.outputDir} onBrowse={() => setMainTab('files')} isPrdOnly={session?.mode === 'prd_only'} />;
                          default: return null;
                        }
                      })}
                    </>
                  );
                })()}
                <div ref={logEndRef} />
              </div>

              {/* Chat input */}
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
                    onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); } }}
                    placeholder={isWaiting ? 'Orchestrator is waiting for your reply…' : isRunning ? 'Send a message — picked up before the next task batch…' : 'Request a change or addition…'}
                    rows={1}
                    style={{
                      flex: 1,
                      background: 'var(--bg)',
                      border: `1px solid ${isWaiting ? 'var(--accent)' : 'var(--border)'}`,
                      borderRadius: 8,
                      color: 'var(--text)',
                      padding: '8px 12px',
                      fontSize: 13,
                      resize: 'none',
                      outline: 'none',
                      lineHeight: 1.5,
                      maxHeight: 120,
                      overflowY: 'auto',
                    }}
                  />
                  <button className="btn-primary" style={{ fontSize: 12, padding: '8px 14px', flexShrink: 0 }} disabled={sending || !chatInput.trim()} onClick={doSend}>
                    {sending ? '…' : isWaiting ? 'Reply' : 'Send'}
                  </button>
                  {isRunning && (
                    <button className="btn-ghost" style={{ fontSize: 12, padding: '8px 14px', flexShrink: 0, color: 'var(--red)' }} disabled={cancelling} onClick={doCancel}>
                      {cancelling ? 'Stopping…' : 'Stop'}
                    </button>
                  )}
                </div>
              )}

              {showRetryButton && (
                <div style={{ flexShrink: 0, borderTop: '1px solid var(--border)', padding: '10px 16px', background: 'var(--bg2)', display: 'flex', alignItems: 'center', gap: 12 }}>
                  <button className="btn-primary" style={{ fontSize: 12 }} disabled={starting} onClick={doStart}>
                    {starting ? 'Starting…' : 'Try again →'}
                  </button>
                  <button className="btn-ghost" style={{ fontSize: 12, color: 'var(--text2)' }} onClick={() => navigate(`/ideas/${id}`, { state: { tab: 'audit', skipRedirect: true } })}>
                    View audit trail →
                  </button>
                  {session.summary && <span style={{ fontSize: 12, color: 'var(--red)', marginLeft: 4 }}>{session.summary}</span>}
                </div>
              )}
            </div>

            {/* Right: tasks panel */}
            {(() => {
              const allTasks = log.filter((e): e is SubAgentBlockEntry => e.kind === 'sub_agent_block');
              const queuedTasks = allTasks.filter(t => t.status === 'queued');
              const runningTasks = allTasks.filter(t => t.status === 'running');
              const completedTasks = allTasks.filter(t => t.status === 'done' || t.status === 'blocked');
              const recentCompleted = completedTasks.slice(-3);
              const olderCompleted = completedTasks.slice(0, -3);
              const succeededTotal = completedTasks.filter(t => t.status === 'done').length;
              return (
                <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px', background: 'var(--bg)' }}>
                  <p style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>
                    Tasks {allTasks.length > 0 && `· ${allTasks.length}`}
                  </p>
                  {allTasks.length === 0 ? (
                    <p style={{ color: 'var(--text2)', fontSize: 12, lineHeight: 1.6 }}>
                      Tasks will appear here as the orchestrator delegates work.
                    </p>
                  ) : (
                    <>
                      {olderCompleted.length > 0 && (
                        <CompletedGroup
                          tasks={olderCompleted}
                          allSucceeded={succeededTotal}
                          allTotal={completedTasks.length}
                          onStop={doStopTask}
                        />
                      )}
                      {recentCompleted.map(t => (
                        <TaskBlock
                          key={t.id}
                          taskId={t.taskId}
                          agentId={t.agentId}
                          title={t.title}
                          status={t.status}
                          summary={t.summary}
                          filesWritten={t.filesWritten}
                          blocker={t.blocker}
                          updates={t.updates}
                        />
                      ))}
                      {runningTasks.map(t => (
                        <TaskBlock
                          key={t.id}
                          taskId={t.taskId}
                          agentId={t.agentId}
                          title={t.title}
                          status={t.status}
                          summary={t.summary}
                          filesWritten={t.filesWritten}
                          blocker={t.blocker}
                          updates={t.updates}
                          streamingText={t.streamingText}
                          onStop={() => doStopTask(t.taskId)}
                        />
                      ))}
                      {queuedTasks.length > 0 && (
                        <>
                          <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.06em', margin: '10px 0 4px' }}>
                            Queued · {queuedTasks.length}
                          </p>
                          {queuedTasks.map(t => (
                            <TaskBlock
                              key={t.id}
                              taskId={t.taskId}
                              agentId={t.agentId}
                              title={t.title}
                              status={t.status}
                              summary={t.summary}
                              filesWritten={t.filesWritten}
                              blocker={t.blocker}
                              updates={t.updates}
                            />
                          ))}
                        </>
                      )}
                    </>
                  )}
                </div>
              );
            })()}
          </div>

        ) : mainTab === 'log' ? (
          // ── Classic: single-column log ──────────────────────────────────────
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
                  case 'complete': return <CompleteEntry key={entry.id} summary={entry.summary} outputDir={entry.outputDir} onBrowse={() => setMainTab('files')} isPrdOnly={session?.mode === 'prd_only'} />;
                  case 'user_msg': return <UserMsgEntry key={entry.id} content={entry.content} />;
                  case 'assistant_msg': return <AssistantMsgEntry key={entry.id} content={entry.content} />;
                  case 'plan_warnings': return <PlanWarningsEntry key={entry.id} warnings={entry.warnings} />;
                  case 'syntax_check': return <SyntaxCheckEntry key={entry.id} path={entry.path} passed={entry.passed} error={entry.error} retrying={entry.retrying} />;
                  default: return null;
                }
              })}

              <div ref={logEndRef} />
            </div>

            {/* Chat input */}
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
                  <button className="btn-ghost" style={{ fontSize: 12, padding: '8px 14px', flexShrink: 0, color: 'var(--red)' }} disabled={cancelling} onClick={doCancel}>
                    {cancelling ? 'Stopping…' : 'Stop'}
                  </button>
                ) : (
                  <button className="btn-primary" style={{ fontSize: 12, padding: '8px 14px', flexShrink: 0 }} disabled={sending || !chatInput.trim()} onClick={doSend}>
                    {sending ? '…' : 'Send'}
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
        ) : null}

        {/* Files tab */}
        {mainTab === 'files' && (fileCount > 0 || session?.mode === 'prd_only') && (
          <div style={{ flex: 1, overflow: 'hidden', padding: '12px 20px' }}>
            <FileBrowser ideaId={id!} refreshKey={fileRefreshKey} />
          </div>
        )}
        </div>{/* end main column */}
      </div>{/* end body row */}
    </div>
  );
}
