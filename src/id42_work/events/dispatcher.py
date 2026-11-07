"""Synchronous delivery of persisted events to subscribers (specification 11)."""

import logging
import threading
from collections.abc import Callable

from id42_work.events.models import WorkEvent

Subscriber = Callable[[WorkEvent], None]

_logger = logging.getLogger("id42_work.events")


class EventDispatcher:
    """Calls subscribers after an event is persisted.

    A subscriber exception is logged and never affects the execution or other
    subscribers.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(subscriber)

    def publish(self, event: WorkEvent) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                _logger.exception(
                    "event subscriber failed",
                    extra={
                        "event.type": event.event_type.value,
                        "execution.id": str(event.execution_id),
                    },
                )
