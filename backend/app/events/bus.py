import asyncio
from collections import defaultdict

from app.events.schemas import PipelineEvent


class EventBus:
    """
    In-process async event bus. One list of subscriber queues per idea_id.
    Pipeline tasks publish; WebSocket handlers subscribe/unsubscribe.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, idea_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[idea_id].append(q)
        return q

    def unsubscribe(self, idea_id: str, queue: asyncio.Queue) -> None:
        try:
            self._subscribers[idea_id].remove(queue)
        except ValueError:
            pass

    async def publish(self, event: PipelineEvent) -> None:
        for q in self._subscribers.get(event.idea_id, []):
            await q.put(event)


# Singleton — imported by pipeline and API layers
event_bus = EventBus()
