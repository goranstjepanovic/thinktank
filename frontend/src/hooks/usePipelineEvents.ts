import { useEffect, useRef } from 'react';
import type { PipelineEvent } from '../types';
import { useIdeaStore } from '../store/ideaStore';

const WS_BASE = 'ws://localhost:8000';

export function usePipelineEvents(ideaId: string | undefined) {
  const applyEvent = useIdeaStore((s) => s.applyEvent);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!ideaId) return;

    const ws = new WebSocket(`${WS_BASE}/ws/ideas/${ideaId}`);
    wsRef.current = ws;

    ws.onmessage = (msg) => {
      try {
        const event: PipelineEvent = JSON.parse(msg.data);
        applyEvent(event);
      } catch {
        // ignore malformed frames
      }
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [ideaId, applyEvent]);
}
