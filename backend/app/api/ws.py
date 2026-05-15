import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events.bus import event_bus

router = APIRouter(tags=["websocket"])

_KEEPALIVE_INTERVAL = 25  # seconds — below typical proxy/NAT idle timeouts


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
    except (WebSocketDisconnect, Exception):
        # WebSocketDisconnect: clean client close.
        # Exception: abrupt close (ConnectionClosedError 1012 service restart,
        # browser refresh, proxy timeout, etc.) — log nothing, just clean up.
        pass
    finally:
        event_bus.unsubscribe(idea_id, queue)
