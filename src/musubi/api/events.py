"""In-process pub-sub broker for real-time thought delivery over SSE."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from musubi.types.thought import Thought

logger = logging.getLogger(__name__)

# Connection cap: 100 per API process
MAX_SUBSCRIBERS = 100


@dataclass(eq=False)
class Subscription:
    namespace: str
    includes: set[str]
    queue: asyncio.Queue[Thought]


class ThoughtBroker:
    """In-process pub-sub broker for thoughts.

    Fanout semantics: BROADCAST. Every subscriber matching the namespace and
    include filters receives every event. It is NOT competing-consumer.
    """

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    def subscribe(self, namespace: str, includes: set[str]) -> Subscription:
        """Create a new subscription."""
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            raise ConnectionError("Connection cap exceeded")

        sub = Subscription(namespace=namespace, includes=includes, queue=asyncio.Queue())
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Remove a subscription."""
        self._subscribers.discard(sub)

    def publish(self, thought: Thought) -> None:
        """Fanout a thought to all matching subscribers."""
        if not self._subscribers:
            return

        to_presence = thought.to_presence
        namespace = thought.namespace

        for sub in list(self._subscribers):
            if sub.namespace != namespace:
                continue

            # Subscriber's `includes` is a set of `to_presence` values the
            # client wants to receive. Default at the endpoint is
            # {token_presence, "all"} — so broadcasts (to_presence="all")
            # reach every subscriber who kept the default. A client that
            # explicitly narrows `include` (e.g. `include=openclaw`) opts out
            # of broadcasts. "all" is NOT a subscriber-side wildcard; it's a
            # to_presence literal.
            if to_presence in sub.includes:
                try:
                    if sub.queue.qsize() > 1000:
                        # Drop event (slow consumer)
                        logger.warning(
                            "Dropped thought %s for slow consumer in %s",
                            thought.object_id,
                            namespace,
                        )
                        continue

                    sub.queue.put_nowait(thought)
                except Exception as e:
                    logger.error("Failed to publish thought to subscriber: %s", e)


# Global singleton for the API process
broker = ThoughtBroker()
