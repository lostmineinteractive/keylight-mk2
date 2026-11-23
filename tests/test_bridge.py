"""End-to-end tests of the daemon against a fake light.

Run with the bridge's virtualenv (it has websockets and aiohttp):

    "$HOME/Library/Application Support/keylight-mk2/venv/bin/python" -m unittest discover tests
"""

from __future__ import annotations

import asyncio
import errno
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from keylight_mk2 import configure  # noqa: E402
from keylight_mk2.api import Api  # noqa: E402
from keylight_mk2.convert import kelvin_to_mired, mired_to_kelvin  # noqa: E402
from keylight_mk2.light import Light, LightRangeError, LightRPCError  # noqa: E402
from tests.fake_light import FakeLight, make_pki  # noqa: E402


class ConvertTest(unittest.TestCase):
    def test_kelvin_to_mired(self):
        self.assertEqual(kelvin_to_mired(6500), 154)
        self.assertEqual(kelvin_to_mired(4500), 222)
        self.assertEqual(kelvin_to_mired(3000), 333)

    def test_clamping(self):
        self.assertEqual(kelvin_to_mired(100000), 143)
        self.assertEqual(kelvin_to_mired(7000), 143)
        self.assertEqual(kelvin_to_mired(2900), 344)
        self.assertEqual(kelvin_to_mired(1), 344)
        self.assertEqual(kelvin_to_mired(-5), 344)

    def test_mired_to_kelvin_rounds_to_50(self):
        self.assertEqual(mired_to_kelvin(143), 7000)
        self.assertEqual(mired_to_kelvin(222), 4500)
        self.assertEqual(mired_to_kelvin(200), 5000)
        self.assertEqual(mired_to_kelvin(344), 2900)


class LocalNetworkBlockedTest(unittest.TestCase):
    """macOS Local Network privacy shows up as EHOSTUNREACH on the mDNS send."""

    def _blocked(self, error):
        class FakeSocket:
            def __init__(self, *args):
                pass

            def sendto(self, data, address):
                self.address = address
                if error is not None:
                    raise OSError(error, "test")

            def close(self):
                pass

        with unittest.mock.patch.object(configure.socket, "socket", FakeSocket):
            return configure.local_network_blocked()

    def test_send_succeeds(self):
        self.assertFalse(self._blocked(None))

    def test_no_route_to_host_means_blocked(self):
        self.assertTrue(self._blocked(errno.EHOSTUNREACH))

    def test_other_errors_are_not_a_block(self):
        self.assertFalse(self._blocked(errno.ENETUNREACH))


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.certs = make_pki(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    async def asyncSetUp(self):
        self.fake = FakeLight(self.root)
        await self.fake.start()
        self.light = Light("key", "127.0.0.1", certs=self.certs)
        self.light.port = self.fake.port
        self.light.rpc_timeout = 0.5
        self.light.connect_wait = 2.0
        self.light.backoff_min = 0.05
        self.light.backoff_max = 0.2
        self.light.start()
        await self.light.ensure_connected(timeout=5)
        self.client = await self._client(Api([self.light]))

    async def asyncTearDown(self):
        await self.client.close()
        await self.light.stop()
        await self.fake.stop()

    async def _client(self, api: Api) -> TestClient:
        client = TestClient(TestServer(api.app()))
        await client.start_server()
        return client

    async def _post(self, path: str, body: str = "", status: int = 200) -> dict:
        # Sent the way `curl -d` does: JSON labelled as a form post.
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = await self.client.post(path, data=body, headers=headers)
        reply = await response.json()
        self.assertEqual(response.status, status, reply)
        return reply

    async def _status(self) -> dict:
        return await (await self.client.get("/status")).json()

    async def _until(self, predicate, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            self.assertLess(asyncio.get_running_loop().time(), deadline, "condition never met")
            await asyncio.sleep(0.02)

    async def test_status(self):
        reply = await self._status()
        self.assertTrue(reply["ok"])
        self.assertEqual((reply["on"], reply["brightness"], reply["kelvin"]), (0, 30, 5000))
        light = reply["lights"][0]
        self.assertEqual(light["name"], "key")
        self.assertEqual(light["serial"], "FAKE00000001")
        self.assertTrue(light["reachable"])
        self.assertIsNotNone(light["heartbeat_age"])

    async def test_set_echoes_applied_state(self):
        reply = await self._post("/set", '{"on":1,"brightness":25,"kelvin":5000}')
        self.assertEqual(
            (reply["on"], reply["brightness"], reply["kelvin"], reply["temperature"]),
            (1, 25, 5000, 200),
        )
        self.assertEqual(self.fake.state, {"on": 1, "brightness": 25, "temperature": 200})

    async def test_out_of_range_is_clamped(self):
        reply = await self._post("/set", '{"kelvin":100000,"brightness":500}')
        self.assertEqual((reply["brightness"], reply["kelvin"], reply["temperature"]), (100, 7000, 143))
        reply = await self._post("/set", '{"temperature":9999,"brightness":-20}')
        self.assertEqual((reply["brightness"], reply["temperature"]), (0, 344))
        self.assertEqual(self.fake.rejected, 0, "the device saw an out-of-range value")

    async def test_on_restores_previous_settings(self):
        await self._post("/set", '{"on":1,"brightness":70,"temperature":300}')
        self.assertEqual((await self._post("/off"))["on"], 0)
        reply = await self._post("/on")
        self.assertEqual((reply["on"], reply["brightness"], reply["temperature"]), (1, 70, 300))

    async def test_toggle(self):
        self.assertEqual((await self._post("/toggle"))["on"], 1)
        self.assertEqual((await self._post("/toggle"))["on"], 0)

    async def test_request_ids_are_strings(self):
        await self._post("/on")
        self.assertTrue(self.fake.requests)
        self.assertTrue(all(isinstance(request["id"], str) for request in self.fake.requests))

    async def test_external_change_is_tracked(self):
        await self.fake.heartbeat()
        await self.fake.push_state(on=1, brightness=5)
        await self._until(lambda: self.light.lights[0]["brightness"] == 5)
        reply = await self._status()
        self.assertEqual((reply["on"], reply["brightness"]), (1, 5))

    async def test_per_light_routes(self):
        reply = await self._post("/light/key/set", '{"brightness":40}')
        self.assertEqual((reply["name"], reply["brightness"]), ("key", 40))
        self.assertEqual((await self._post("/light/FAKE00000001/on"))["on"], 1)
        self.assertEqual((await self._post("/set", '{"light":"key","on":0}'))["on"], 0)
        self.assertEqual((await self.client.get("/light/key/status")).status, 200)
        self.assertEqual((await self.client.get("/light/nope/status")).status, 404)

    async def test_bad_input(self):
        for body in ("", "{}", '{"brightness":"bright"}', '{"kelvin":4000,"temperature":250}',
                     '{"on":2}', '{"brightnes":40}', "[1]", '{"on":1'):  # fmt: skip
            reply = await self._post("/set", body, status=400)
            self.assertFalse(reply["ok"])
            self.assertIn("error", reply)
        self.assertEqual((await self.client.get("/on")).status, 405)
        self.assertEqual((await self.client.get("/nope")).status, 404)

    async def test_query_parameters(self):
        reply = await self._post("/set?brightness=55&on=true")
        self.assertEqual((reply["on"], reply["brightness"]), (1, 55))

    async def test_device_range_error_is_a_400(self):
        with self.assertRaises(LightRangeError):
            await self.light.call(
                "put.elgato.lights",
                {"numberOfLights": 1, "lights": [{"on": 1, "brightness": 50, "temperature": 9}]},
            )

    async def test_unknown_method(self):
        with self.assertRaises(LightRPCError):
            await self.light.call("get.elgato.nope")

    async def test_web_pages_are_rejected(self):
        response = await self.client.post("/on", headers={"Origin": "https://example.com"})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.fake.state["on"], 0)

    async def test_shared_secret(self):
        client = await self._client(Api([self.light], secret="s3cret"))
        try:
            self.assertEqual((await client.get("/status")).status, 401)
            self.assertEqual((await client.post("/on", headers={"X-Bridge-Secret": "nope"})).status, 401)
            self.assertEqual((await client.post("/on", headers={"X-Bridge-Secret": "s3cret"})).status, 200)
            self.assertEqual((await client.get("/status", headers={"Authorization": "Bearer s3cret"})).status, 200)
        finally:
            await client.close()

    async def test_unreachable_is_503_then_reconnects(self):
        # "Pull the power": the light vanishes mid-session.
        await self.fake.stop()
        await self._until(lambda: not self.light.reachable)
        self.light.connect_wait = 0.3
        reply = await self._post("/on", status=503)
        self.assertFalse(reply["ok"])
        self.assertIn("unreachable", reply["error"])
        self.assertFalse((await self._status())["reachable"])
        # "Restore the power": the daemon reconnects with no outside help.
        await self.fake.start()
        await self._until(lambda: self.light.reachable)
        self.assertEqual((await self._post("/on"))["on"], 1)

    async def test_silently_dead_socket_recovers_within_one_request(self):
        self.fake.go_mute()
        self.assertEqual((await self._post("/on"))["on"], 1)
        self.assertEqual(self.fake.state["on"], 1)

    async def test_missed_heartbeat_triggers_reconnect(self):
        self.light.heartbeat_timeout = 0.2
        self.light.watchdog_interval = 0.1
        # Restart the session so the supervisor picks up the short intervals.
        await self.fake.stop()
        await self.fake.start()
        await self._until(lambda: self.light.reachable)
        before = len(self.fake.requests)
        self.fake.go_mute()
        await self._until(lambda: not self.light.reachable)
        await self._until(lambda: self.light.reachable)
        self.assertGreater(len(self.fake.requests), before)


if __name__ == "__main__":
    unittest.main()
