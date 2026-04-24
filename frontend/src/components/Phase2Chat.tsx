import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { useNavigate, useParams } from 'react-router-dom';
import { api, BASE, WS_BASE } from '../api/client';
import type { IdeaDetail, Phase2Message, Phase2Session, PipelineEvent } from '../types';
import { PhaseNav } from './PhaseNav';
import { useWebSocket } from '../hooks/useWebSocket';

// ---------------------------------------------------------------------------
// Message bubble
// ---------------------------------------------------------------------------

function MessageBubble({ msg, streaming }: { msg: Phase2Message; streaming?: boolean }) {
  const isUser = msg.role === 'user';
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start', marginBottom: 16 }}>
      {!isUser && (
        <div style={{
          width: 28, height: 28, borderRadius: '50%', background: '#1e2d4a',
          color: 'var(--blue)', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 11, fontWeight: 700, flexShrink: 0, marginRight: 10, marginTop: 2,
        }}>
          AI
        </div>
      )}
      <div style={{
        maxWidth: '80%',
        background: isUser ? '#1e2d4a' : 'var(--bg2)',
        border: `1px solid ${isUser ? '#2d4a7a' : 'var(--border)'}`,
        borderRadius: isUser ? '12px 12px 2px 12px' : '2px 12px 12px 12px',
        padding: '10px 14px',
        fontSize: 13,
        lineHeight: 1.6,
      }}>
        {isUser ? (
          <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{msg.content}</p>
        ) : (
          <div className="markdown" style={{ fontSize: 13 }}>
            <ReactMarkdown>{msg.content}</ReactMarkdown>
            {streaming && <span className="stream-cursor" />}
          </div>
        )}
        {!streaming && (
          <p style={{ margin: '6px 0 0', fontSize: 10, color: 'var(--text2)', textAlign: 'right' }}>
            {new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </p>
        )}
      </div>
      {isUser && (
        <div style={{
          width: 28, height: 28, borderRadius: '50%', background: '#252535',
          color: 'var(--text2)', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 11, fontWeight: 700, flexShrink: 0, marginLeft: 10, marginTop: 2,
        }}>
          You
        </div>
      )}
    </div>
  );
}

function TypingIndicator() {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 16 }}>
      <div style={{
        width: 28, height: 28, borderRadius: '50%', background: '#1e2d4a',
        color: 'var(--blue)', display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 11, fontWeight: 700, flexShrink: 0, marginRight: 10, marginTop: 2,
      }}>
        AI
      </div>
      <div style={{
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: '2px 12px 12px 12px', padding: '12px 16px',
        display: 'flex', alignItems: 'center', gap: 3,
      }}>
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </div>
    </div>
  );
}

function ResolutionSummaryPanel({ summary }: { summary: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ borderBottom: '1px solid var(--border)', background: '#0e1a0e' }}>
      <button
        style={{
          width: '100%', padding: '8px 24px', display: 'flex', justifyContent: 'space-between',
          alignItems: 'center', background: 'none', border: 'none', cursor: 'pointer',
          color: 'var(--green)', fontSize: 12, fontWeight: 600,
        }}
        onClick={() => setOpen((v) => !v)}
      >
        <span>✓ Resolution Summary — decisions captured from Q&A</span>
        <span>{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div style={{ padding: '0 24px 16px', maxHeight: 360, overflowY: 'auto' }}>
          <div className="markdown" style={{ fontSize: 12 }}>
            <ReactMarkdown>{summary}</ReactMarkdown>
          </div>
        </div>
      )}
    </div>
  );
}

function SummaryGeneratingBanner() {
  return (
    <div style={{
      borderBottom: '1px solid var(--border)', background: '#0e1a0e',
      padding: '8px 24px', display: 'flex', alignItems: 'center', gap: 10,
    }}>
      <span className="typing-dot" />
      <span className="typing-dot" />
      <span className="typing-dot" />
      <span style={{ fontSize: 12, color: 'var(--green)', fontWeight: 600 }}>
        Generating resolution summary…
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function Phase2Chat() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [idea, setIdea] = useState<IdeaDetail | null>(null);
  const [session, setSession] = useState<Phase2Session | null>(null);
  const [messages, setMessages] = useState<Phase2Message[]>([]);
  // The assistant message being built token-by-token
  const [streamingContent, setStreamingContent] = useState<string | null>(null);
  const [thinking, setThinking] = useState(false); // true only during opening message generation
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Ref to avoid stale closure in WS handler when checking session status
  const sessionRef = useRef<Phase2Session | null>(null);
  useEffect(() => { sessionRef.current = session; }, [session]);

  // Load idea + existing session on mount
  useEffect(() => {
    if (!id) return;
    api.getIdea(id).then(setIdea).catch(() => {});
    api.getPhase2(id)
      .then((s) => { setSession(s); setMessages(s.messages); })
      .catch(() => {});
  }, [id]);

  // WebSocket — handles opening message (non-streaming) and other clients' messages
  useWebSocket(id ? `${WS_BASE}/ws/ideas/${id}` : null, (data: string) => {
    try {
      const event: PipelineEvent = JSON.parse(data);
      if (!event.event_type.startsWith('phase2.')) return;

      if (event.event_type === 'phase2.thinking') {
        // Only show the message-stream typing indicator for opening message generation.
        // Resolution summary generation (post-RESOLVING) is indicated by SummaryGeneratingBanner,
        // derived from session.status + resolution_summary being null.
        const currentSession = sessionRef.current;
        if (!currentSession || currentSession.status === 'RESOLVING') {
          setThinking(true);
        }
      } else if (event.event_type === 'phase2.message') {
        setThinking(false);
        // Only append messages we didn't already receive via our own stream
        const incoming: Phase2Message = {
          id: event.payload.message_id as string,
          session_id: event.payload.session_id as string,
          role: event.payload.role as 'user' | 'assistant',
          content: event.payload.content as string,
          created_at: event.timestamp,
        };
        setMessages((prev) => prev.some((m) => m.id === incoming.id) ? prev : [...prev, incoming]);
      } else if (event.event_type === 'phase2.error') {
        setThinking(false);
        setError(`Generation failed: ${event.payload.error}`);
      } else if (event.event_type === 'phase2.status_changed') {
        // Re-fetch session to get resolution_summary if it was just generated
        api.getPhase2(id!).then((s) => setSession(s)).catch(() => {});
      } else if (event.event_type === 'phase2.started') {
        api.getPhase2(id!).then((s) => { setSession(s); setMessages(s.messages); }).catch(() => {});
      }
    } catch { /* ignore */ }
  });

  // Scroll to bottom when content changes
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent, thinking]);

  const doStart = async () => {
    if (!id) return;
    setStarting(true);
    setError(null);
    try {
      const s = await api.startPhase2(id);
      setSession(s);
      setMessages(s.messages);
    } catch (e: unknown) {
      setError(`Failed to start Phase 2: ${(e as Error).message}`);
    } finally {
      setStarting(false);
    }
  };

  const doSend = async () => {
    if (!id || !input.trim() || isStreaming || thinking) return;
    const content = input.trim();
    setInput('');
    setError(null);

    // Add user message optimistically
    const tempUserMsg: Phase2Message = {
      id: `temp-${Date.now()}`,
      session_id: session?.id ?? '',
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    setIsStreaming(true);
    setStreamingContent('');

    let accumulated = '';

    try {
      const resp = await fetch(`${BASE}/ideas/${id}/phase2/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });

      if (!resp.ok) {
        throw new Error(`${resp.status} ${resp.statusText}`);
      }

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE events are separated by \n\n
        const parts = buffer.split('\n\n');
        buffer = parts.pop() ?? '';

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));

            if (data.type === 'chunk') {
              accumulated += data.content;
              setStreamingContent(accumulated);
            } else if (data.type === 'done') {
              // Commit the streamed message as a real message
              const assistantMsg: Phase2Message = {
                id: data.message_id,
                session_id: session?.id ?? '',
                role: 'assistant',
                content: accumulated,
                created_at: new Date().toISOString(),
              };
              setMessages((prev) => [...prev, assistantMsg]);
              setStreamingContent(null);
              setIsStreaming(false);
            } else if (data.type === 'error') {
              throw new Error(data.error);
            }
          } catch (parseErr) {
            // ignore malformed SSE lines
          }
        }
      }
    } catch (e: unknown) {
      setError(`Message failed: ${(e as Error).message}`);
      // Remove the optimistic user message on failure
      setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
      setStreamingContent(null);
      setIsStreaming(false);
    } finally {
      inputRef.current?.focus();
    }
  };

  const doMarkReady = async () => {
    if (!id) return;
    try {
      const updated = await api.markPhase2Ready(id);
      setSession(updated);
    } catch (e: unknown) {
      setError(`Failed: ${(e as Error).message}`);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  };

  if (!idea) {
    return <div className="page"><p style={{ color: 'var(--text2)' }}>Loading…</p></div>;
  }

  const selectedBranch = idea.branches.find((b) => b.id === idea.selected_branch_id);
  const isGenerating = isStreaming || thinking;

  // No session — start screen
  if (!session) {
    return (
      <div className="page">
        <button className="btn-ghost" style={{ fontSize: 12, marginBottom: 16 }}
          onClick={() => navigate(`/ideas/${id}`, { state: { skipRedirect: true } })}>
          ← Back to analysis
        </button>
        <div style={{ maxWidth: 640, margin: '60px auto', textAlign: 'center' }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 12 }}>Phase 2 — Implementation</h1>
          <p style={{ fontSize: 14, color: 'var(--text2)', marginBottom: 8 }}>
            <strong style={{ color: 'var(--text)' }}>{idea.name}</strong>
            {selectedBranch && <> · Branch {selectedBranch.branch_index}</>}
          </p>
          {selectedBranch?.approach_summary && (
            <p style={{ fontSize: 13, color: 'var(--text2)', marginBottom: 28 }}>{selectedBranch.approach_summary}</p>
          )}
          <div className="card" style={{ textAlign: 'left', marginBottom: 28 }}>
            <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>What happens in Phase 2</p>
            <ol style={{ fontSize: 13, color: 'var(--text2)', paddingLeft: 20, lineHeight: 1.8 }}>
              <li>The assistant presents the Open Questions from the Phase 1 analysis</li>
              <li>You resolve them and add context, preferences, and constraints</li>
              <li>Once questions are resolved, click <strong>Proceed to implementation</strong></li>
              <li>Implementation artifacts generated step by step — code, scaffolding, tests, config</li>
              <li>Review each artifact; redirect as needed</li>
            </ol>
          </div>
          {error && <p style={{ color: 'var(--red)', fontSize: 13, marginBottom: 16 }}>{error}</p>}
          <button className="btn-primary" style={{ fontSize: 14, padding: '10px 28px' }} disabled={starting} onClick={doStart}>
            {starting ? 'Starting…' : 'Begin Phase 2'}
          </button>
        </div>
      </div>
    );
  }

  // Build the displayed message list: committed messages + in-progress streaming message
  const displayMessages = messages;
  const streamingMsg: Phase2Message | null = streamingContent !== null
    ? { id: 'streaming', session_id: session.id, role: 'assistant', content: streamingContent, created_at: new Date().toISOString() }
    : null;

  return (
    <div className="page" style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 56px)', padding: 0 }}>
      {/* Header */}
      <div style={{
        padding: '10px 20px', borderBottom: '1px solid var(--border)',
        flexShrink: 0, display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        background: 'var(--bg2)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <PhaseNav ideaId={id!} currentPhase={2} />
          {selectedBranch && (
            <span style={{ fontSize: 12, color: 'var(--text2)' }}>
              · Branch {selectedBranch.branch_index}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {session.status === 'RESOLVING' && (
            <button className="btn-primary" style={{ fontSize: 12 }} onClick={doMarkReady}>
              Proceed to implementation →
            </button>
          )}
          {['READY', 'IMPLEMENTING', 'COMPLETE'].includes(session.status) && (
            <button className="btn-primary"
              style={{ fontSize: 12, background: '#1a3a1a', borderColor: 'var(--green)', color: 'var(--green)' }}
              disabled={!session.resolution_summary}
              title={!session.resolution_summary ? 'Waiting for resolution summary to finish generating…' : undefined}
              onClick={() => navigate(`/ideas/${id}/phase3`)}>
              {session.resolution_summary ? 'Phase 3: Build →' : 'Generating summary…'}
            </button>
          )}
          <span className={`badge badge-${session.status.toLowerCase()}`} style={{ fontSize: 11 }}>
            {session.status === 'RESOLVING' ? 'Resolving' : session.status}
          </span>
        </div>
      </div>

      {/* Resolution summary panel — loading banner while generating, collapsible panel once ready */}
      {session.status !== 'RESOLVING' && (
        session.resolution_summary
          ? <ResolutionSummaryPanel summary={session.resolution_summary} />
          : <SummaryGeneratingBanner />
      )}

      {/* Messages */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>
        {displayMessages.length === 0 && !thinking && !streamingMsg && (
          <p style={{ color: 'var(--text2)', textAlign: 'center', marginTop: 40 }}>
            Starting Phase 2 session…
          </p>
        )}
        {displayMessages.map((m) => <MessageBubble key={m.id} msg={m} />)}
        {streamingMsg && <MessageBubble msg={streamingMsg} streaming />}
        {thinking && !streamingMsg && <TypingIndicator />}
        {error && (
          <div className="card" style={{ borderColor: 'var(--red)', background: '#1e0e0e', margin: '8px 0' }}>
            <p style={{ fontSize: 13, color: 'var(--red)' }}>{error}</p>
            <button className="btn-ghost" style={{ fontSize: 12, marginTop: 8 }} onClick={() => setError(null)}>
              Dismiss
            </button>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div style={{ borderTop: '1px solid var(--border)', padding: '12px 24px', flexShrink: 0 }}>
        {session.status === 'READY' && session.resolution_summary && (
          <p style={{ fontSize: 12, color: 'var(--green)', marginBottom: 8 }}>
            ✓ Questions resolved — continue the conversation to guide implementation.
          </p>
        )}
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
          <textarea
            ref={inputRef}
            style={{
              flex: 1, background: 'var(--bg2)', border: '1px solid var(--border)',
              borderRadius: 8, color: 'var(--text)', padding: '10px 12px',
              fontSize: 13, resize: 'none', lineHeight: 1.5, maxHeight: 160, minHeight: 44,
            }}
            placeholder={isGenerating ? 'Waiting for response…' : 'Type a message… (Enter to send, Shift+Enter for new line)'}
            value={input}
            disabled={isGenerating}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
          />
          <button
            className="btn-primary"
            style={{ flexShrink: 0, padding: '10px 20px' }}
            disabled={!input.trim() || isGenerating}
            onClick={doSend}
          >
            {isStreaming ? '…' : 'Send'}
          </button>
        </div>
        <p style={{ fontSize: 11, color: 'var(--text2)', marginTop: 6 }}>
          Enter ↵ to send · Shift+Enter for new line
        </p>
      </div>
    </div>
  );
}
