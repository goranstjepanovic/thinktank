import type { PipelineEvent } from '../types';
import { useIdeaStore } from '../store/ideaStore';
import { WS_BASE } from '../api/client';
import { useWebSocket } from './useWebSocket';

export function usePipelineEvents(ideaId: string | undefined) {
  const applyEvent = useIdeaStore((s) => s.applyEvent);

  useWebSocket(ideaId ? `${WS_BASE}/ws/ideas/${ideaId}` : null, (data: string) => {
    try {
      const event: PipelineEvent = JSON.parse(data);
      applyEvent(event);
    } catch {
      // ignore malformed frames
    }
  });
}
