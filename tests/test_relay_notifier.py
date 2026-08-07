from __future__ import annotations

import asyncio
import threading
import unittest

from server import RelayUpdateNotifier


class RelayUpdateNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_from_another_thread_wakes_subscriber(self) -> None:
        notifier = RelayUpdateNotifier()
        subscription_id, event = notifier.subscribe()

        thread = threading.Thread(target=notifier.notify)
        thread.start()
        thread.join()

        await asyncio.wait_for(event.wait(), timeout=1)
        notifier.unsubscribe(subscription_id)


if __name__ == "__main__":
    unittest.main()
