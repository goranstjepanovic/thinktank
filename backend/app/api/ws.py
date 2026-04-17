import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events.bus import event_bus

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/ideas/{idea_id}")
async def idea_websocket(idea_id: str, websocket: WebSocket):
    await websocket.accept()
    queue = event_bus.subscribe(idea_id)
    try:
        while True:
            event = await queue.get()
            await websocket.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(idea_id, queue)
