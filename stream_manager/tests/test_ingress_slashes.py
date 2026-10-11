import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from onvif_proxy.web import setup_routes


class IngressSlashTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        setup_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_triple_slash_serves_the_panel(self):
        response = await self.client.get("///")
        text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("Stream Manager", text)
        self.assertIn('src="static/app.js?v=113"', text)

    async def test_slashed_static_still_loads(self):
        response = await self.client.get("///static/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn("rooted", await response.text())
