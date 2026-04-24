import { useEffect, useRef } from 'react';

/**
 * Generic auto-reconnecting WebSocket hook.
 *
 * - Connects when `url` is non-null, disconnects/stops on unmount or when url → null.
 * - Reconnects with exponential backoff (1 s → 2 s → … capped at 15 s) on any unclean close.
 * - `onMessage` is stored in a ref so it never needs to be a dependency — the hook
 *   always calls the latest version without tearing down the socket.
 * - Silently ignores `{"type":"keepalive"}` frames sent by the backend heartbeat.
 */
export function useWebSocket(
  url: string | null | undefined,
  onMessage: (data: string) => void,
) {
  const onMessageRef = useRef(onMessage);
  useEffect(() => { onMessageRef.current = onMessage; });

  useEffect(() => {
    if (!url) return;

    let unmounted = false;
    let retryDelay = 1000;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let ws: WebSocket | null = null;

    function connect() {
      if (unmounted) return;
      ws = new WebSocket(url!);

      ws.onopen = () => { retryDelay = 1000; };

      ws.onerror = () => ws?.close();

      ws.onclose = () => {
        ws = null;
        if (unmounted) return;
        retryTimer = setTimeout(() => {
          retryDelay = Math.min(retryDelay * 2, 15_000);
          connect();
        }, retryDelay);
      };

      ws.onmessage = (ev) => {
        try {
          // Ignore server heartbeat frames before passing to the consumer
          const raw = JSON.parse(ev.data) as { type?: string };
          if (raw.type === 'keepalive') return;
        } catch { /* not JSON — fall through */ }
        onMessageRef.current(ev.data);
      };
    }

    connect();

    return () => {
      unmounted = true;
      if (retryTimer) clearTimeout(retryTimer);
      ws?.close(1000);
      ws = null;
    };
  }, [url]);
}
