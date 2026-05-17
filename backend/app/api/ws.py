import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events.bus import event_bus

try:
    from websockets.exceptions import ConnectionClosed as _WsConnectionClosed
except ImportError:
    _WsConnectionClosed = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])

_KEEPALIVE_INTERVAL = 25  # seconds — below typical proxy/NAT idle timeouts

# Close codes that indicate a normal client-initiated disconnect (not a server error)
_NORMAL_CLOSE_CODES = {1000, 1001}


@router.websocket("/ws/ideas/{idea_id}")
async def idea_websocket(idea_id: str, websocket: WebSocket):
    await websocket.accept()
    queue = event_bus.subscribe(idea_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_INTERVAL)
                await websocket.send_text(event.model_dump_json())
            except asyncio.TimeoutError:
                # No events for a while — send a lightweight ping so the connection
                # stays alive through proxies and NAT devices.
                await websocket.send_text('{"type":"keepalive"}')
    except WebSocketDisconnect:
        pass  # clean client close
    except Exception as exc:
        # websockets library raises ConnectionClosed* when the client navigates away
        # (code 1001 "going away") or closes normally (1000).  Treat these as clean
        # disconnects — no warning needed.
        if _WsConnectionClosed and isinstance(exc, _WsConnectionClosed):
            rcvd = getattr(exc, "rcvd", None)
            code = getattr(rcvd, "code", None)
            if code in _NORMAL_CLOSE_CODES:
                return
        logger.warning("ws: unexpected error for idea %s: %s", idea_id, exc, exc_info=True)
    finally:
        event_bus.unsubscribe(idea_id, queue)
