import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import server


class VideoPresenceTests(unittest.TestCase):
    def test_explicit_leases_cannot_be_reclaimed_by_heartbeats(self):
        presence = server.VideoPresence()
        clock = [100.]
        with patch('server.time.monotonic', lambda: clock[0]):
            a = UUID(presence.start(uuid4())['lease_id'])
            self.assertTrue(presence.update(a, 1, True))
            self.assertFalse(presence.update(a, 1, False))
            b = UUID(presence.start(uuid4())['lease_id'])
            for elapsed in (1, 61, 600):
                clock[0] += elapsed
                self.assertFalse(presence.update(a, elapsed+2, True))
            self.assertTrue(presence.update(b, 1, True))
            clock[0] += 11
            self.assertEqual(presence.snapshot()['age_ms'], 11000)
            self.assertFalse(presence.update(a, 1000, False))
            self.assertTrue(presence.update(b, 2, False))
            self.assertFalse(presence.snapshot()['active'])
            self.assertFalse(presence.update(b, 3, True))
            reboot = server.VideoPresence()
            self.assertNotEqual(reboot.boot_id, presence.boot_id)
            self.assertFalse(reboot.update(b, 4, True))

    def test_http_origin_loopback_and_strict_payload(self):
        request = SimpleNamespace(headers={'origin': 'https://cloudxr.example:48322'},
                                  client=SimpleNamespace(host='192.168.8.222'))
        with patch.object(server, 'video_presence', server.VideoPresence()):
            lease = asyncio.run(server.start_video_presence(
                server.VideoPresenceStart(session_id=uuid4()), request))
            update = server.VideoPresenceRequest(lease_id=lease['lease_id'], seq=1, active=True)
            self.assertTrue(asyncio.run(server.set_video_presence(update, request))['accepted'])
            with self.assertRaises(HTTPException):
                asyncio.run(server.get_video_presence(request))
            local = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'))
            self.assertTrue(asyncio.run(server.get_video_presence(local))['active'])
            for origin in ('https://wrong.example:48323', 'https://host:x', ''):
                request.headers['origin'] = origin
                with self.assertRaises(HTTPException):
                    asyncio.run(server.set_video_presence(update, request))
        for extra in ({'seq': 0}, {'active': 'true'}, {'lease_id': 'bad'}, {'extra': 1}):
            with self.assertRaises(ValidationError):
                server.VideoPresenceRequest.model_validate(dict(lease_id=str(uuid4()), seq=1, active=True) | extra)
