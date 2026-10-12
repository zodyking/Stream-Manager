import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from onvif_proxy.web import _jpeg, published_frame_url, setup_routes


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
        self.assertIn('src="static/app.js?v=117"', text)

    async def test_slashed_static_still_loads(self):
        response = await self.client.get("///static/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn("rooted", await response.text())


class PublishedSnapshotTests(unittest.TestCase):
    def test_snapshot_url_is_a_cached_jpeg_of_the_published_stream(self):
        self.assertEqual(
            published_frame_url(19841, "cam 1"),
            "http://127.0.0.1:19841/api/frame.jpeg?src=cam%201&cache=5s",
        )
        self.assertTrue(_jpeg(b"\xff\xd8\xff"))
        self.assertFalse(_jpeg(b""))
        self.assertFalse(_jpeg(b"GIF89a"))
