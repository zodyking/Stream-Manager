import tempfile
import unittest
from pathlib import Path

from onvif_proxy.engine import RecordingEngine
from onvif_proxy.ha_client import Playback
from onvif_proxy.discovery import DiscoveryService, scope_list
from onvif_proxy.models import Source, camera_from_dict
from onvif_proxy.stream_manager import StreamManager, ffmpeg_still, ffmpeg_templates, producer_for, with_source_bind


class FakeHa:
    def __init__(self, playback: Playback) -> None:
        self.playback = playback
        self.states: dict[str, str] = {}

    async def playback_for_entity(self, entity_id: str) -> Playback:
        return self.playback


def manager(tmp: Path) -> tuple[StreamManager, RecordingEngine]:
    engine = RecordingEngine()
    return StreamManager(engine, ui_port=8099, onvif_port=8080, rtsp_port=8554, data_dir=tmp), engine


class StreamManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_rule_switch_keeps_rtsp_url_and_stream_name(self):
        cam = camera_from_dict(
            {
                "id": "printer",
                "name": "Printer",
                "listen_ip": "192.0.2.20",
                "rules_enabled": True,
                "fallback_source_id": "still",
                "sources": [
                    {"id": "live", "type": "rtsp", "name": "Live", "url": "rtsp://camera.example/live"},
                    {"id": "still", "type": "image", "name": "Off", "image_url": "https://example.com/off.jpg"},
                ],
                "rules": [
                    {"id": "r1", "entity_id": "switch.printer", "operator": "eq", "value": "on", "source_id": "live"}
                ],
            }
        )
        streams, engine = manager(Path("."))
        ha = FakeHa(Playback(None, None))
        await streams.sync([cam], {}, ha)
        self.assertEqual(streams.runtime["printer"]["source_id"], "still")
        self.assertEqual(streams.runtime["printer"]["source_kind"], "image")
        self.assertEqual(streams.runtime["printer"]["rtsp_url"], "rtsp://192.0.2.20:8554/printer")
        self.assertIn("off.jpg", engine.streams["printer"])

        await streams.sync([cam], {"switch.printer": "on"}, ha)
        self.assertEqual(streams.runtime["printer"]["source_id"], "live")
        self.assertEqual(streams.runtime["printer"]["source_kind"], "stream")
        self.assertEqual(streams.runtime["printer"]["rtsp_url"], "rtsp://192.0.2.20:8554/printer")
        published = engine.streams["printer"]
        self.assertTrue(published.startswith("ffmpeg:rtsp://camera.example/live#video="))
        self.assertNotRegex(published, r"\s")
        self.assertIn("width=1920", published)
        self.assertIn("height=1080", published)
        self.assertIn("-profile:v main", "\n".join(engine.templates.values()))
        self.assertIn("bitrate=2048k", published)
        self.assertIn("printer", engine.streams)

        await streams.sync([cam], {"switch.printer": "off"}, ha)
        self.assertEqual(streams.runtime["printer"]["reason"], "fallback")
        self.assertEqual(streams.runtime["printer"]["rtsp_url"], "rtsp://192.0.2.20:8554/printer")

    async def test_ha_source_without_connection_records_error(self):
        cam = camera_from_dict(
            {
                "id": "door",
                "name": "Door",
                "listen_ip": "127.0.0.1",
                "fallback_source_id": "cam",
                "sources": [{"id": "cam", "type": "ha_camera", "entity_id": "camera.door", "name": "Door"}],
            }
        )
        streams, engine = manager(Path("."))
        ha = FakeHa(Playback(None, "Home Assistant is not connected"))
        await streams.sync([cam], {}, ha)
        self.assertEqual(streams.runtime["door"]["source_id"], "cam")
        self.assertIn("not connected", streams.runtime["door"]["error"])
        self.assertNotIn("door", engine.streams)

    async def test_custom_output_is_applied_to_the_producer(self):
        cam = camera_from_dict(
            {
                "id": "door",
                "name": "Door",
                "listen_ip": "127.0.0.1",
                "fallback_source_id": "cam",
                "sources": [{"id": "cam", "type": "rtsp", "name": "Door", "url": "rtsp://camera.example/door"}],
                "output": {
                    "width": 640,
                    "height": 360,
                    "framerate": 8,
                    "bitrate_kbps": 512,
                    "h264_profile": "Baseline",
                    "gov_length": 12,
                },
            }
        )
        streams, engine = manager(Path("."))
        await streams.sync([cam], {}, FakeHa(Playback(None, None)))
        published = engine.streams["door"]
        encoded = "\n".join(engine.templates.values())
        self.assertNotRegex(published, r"\s")
        self.assertIn("width=640", published)
        self.assertIn("height=360", published)
        self.assertIn("-r 8", encoded)
        self.assertIn("-g 12", encoded)
        self.assertIn("-profile:v baseline", encoded)
        self.assertIn("sliced-threads=0:slices=1", encoded)
        self.assertIn("bitrate=512k", published)
        self.assertNotIn("#video=h264", published)

    async def test_listening_network_binds_the_rtsp_pull(self):
        cam = camera_from_dict(
            {
                "id": "gate",
                "name": "Gate",
                "listen_ip": "192.0.2.50",
                "interface": "guest",
                "source_ip": "192.0.2.10",
                "source_interface": "cameras",
                "fallback_source_id": "cam",
                "sources": [{"id": "cam", "type": "rtsp", "name": "Gate", "url": "rtsp://camera.example/gate"}],
            }
        )
        streams, engine = manager(Path("."))
        await streams.sync([cam], {}, FakeHa(Playback(None, None)))
        published = engine.streams["gate"]
        self.assertIn("-localaddr 192.0.2.10 -i {input}", "\n".join(engine.templates.values()))
        self.assertNotRegex(published, r"\s")
        self.assertTrue(published.startswith("ffmpeg:rtsp://camera.example/gate#video="))
        self.assertEqual(streams.runtime["gate"]["rtsp_url"], "rtsp://192.0.2.50:8554/gate")

    def test_host_route_does_not_bind_a_local_address(self):
        src = with_source_bind("ffmpeg:rtsp://camera.example/live#video=-c:v libx264", "")
        self.assertNotIn("localaddr", src)

    async def test_cycle_runs_until_a_rule_matches(self):
        cam = camera_from_dict(
            {
                "id": "spin",
                "name": "Spin",
                "listen_ip": "127.0.0.1",
                "cycle_enabled": True,
                "cycle_seconds": 5,
                "rules_enabled": True,
                "fallback_source_id": "a",
                "sources": [
                    {"id": "a", "type": "rtsp", "name": "A", "url": "rtsp://camera.example/a"},
                    {"id": "b", "type": "rtsp", "name": "B", "url": "rtsp://camera.example/b"},
                ],
                "rules": [{"id": "r1", "entity_id": "switch.printer", "operator": "eq", "value": "on", "source_id": "b"}],
            }
        )
        streams, _engine = manager(Path("."))
        await streams.sync([cam], {}, FakeHa(Playback(None, None)))
        self.assertEqual(streams.runtime["spin"]["reason"], "cycle")
        self.assertIn(streams.runtime["spin"]["source_id"], {"a", "b"})
        await streams.sync([cam], {"switch.printer": "on"}, FakeHa(Playback(None, None)))
        self.assertEqual(streams.runtime["spin"]["reason"], "rule")
        self.assertEqual(streams.runtime["spin"]["source_id"], "b")

    def test_uploaded_video_loops_in_real_time(self):
        with tempfile.TemporaryDirectory() as folder:
            images = Path(folder) / "images"
            images.mkdir()
            (images / "loop.mp4").write_bytes(b"video")
            src, error = producer_for(
                Source(id="clip", type="media", name="Clip", image_path="images/loop.mp4"),
                data_dir=Path(folder),
                ui_port=1,
                ha_mode=None,
                ha_error=None,
            )
        self.assertIsNone(error)
        self.assertTrue(src.startswith("ffmpeg:"))
        self.assertTrue(any("stream_loop -1 -re" in body for body in ffmpeg_templates().values()))

    def test_still_image_template(self):
        src = ffmpeg_still("C:/data/images/off.jpg")
        self.assertNotRegex(src, r"\s")
        self.assertIn("h264", src)
        self.assertTrue(src.startswith("ffmpeg:"))
        self.assertTrue(any("{input}" in body for body in engine_templates().values()))


def engine_templates() -> dict[str, str]:
    from onvif_proxy.stream_manager import ffmpeg_templates

    return ffmpeg_templates()


class FakeSock:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, packet, addr):
        self.sent.append((packet, addr))


def _ad(ip: str, name: str) -> dict:
    return {
        "ip": ip,
        "endpoint_uuid": name,
        "scopes": scope_list(name),
        "xaddr": f"http://{ip}/onvif/device_service",
    }


class DiscoveryTests(unittest.TestCase):
    def test_each_camera_answers_from_its_own_address(self):
        service = DiscoveryService()
        gate, door = FakeSock(), FakeSock()
        service._targets = [_ad("192.0.2.40", "Gate"), _ad("192.0.2.41", "Door")]
        service._senders = {"192.0.2.40": gate, "192.0.2.41": door}
        probe = b"<s:Header><a:MessageID>uuid:p1</a:MessageID></s:Header><d:Probe><d:Types>tds:Device</d:Types></d:Probe>"
        service._reply(probe, ("192.0.2.2", 3702))
        self.assertEqual(len(gate.sent), 1)
        self.assertIn(b"http://192.0.2.40/onvif/device_service", gate.sent[0][0])
        self.assertIn(b"http://192.0.2.41/onvif/device_service", door.sent[0][0])
        self.assertEqual(gate.sent[0][1], ("192.0.2.2", 3702))

    def test_duplicate_probe_on_two_interfaces_is_answered_once(self):
        service = DiscoveryService()
        sock = FakeSock()
        service._targets = [_ad("192.0.2.40", "Gate")]
        service._senders = {"192.0.2.40": sock}
        probe = b"<a:MessageID>uuid:p2</a:MessageID><d:Probe></d:Probe>"
        service._reply(probe, ("192.0.2.2", 3702))
        service._reply(probe, ("192.0.2.2", 3702))
        self.assertEqual(len(sock.sent), 1)

    def test_hello_goes_to_the_multicast_group(self):
        service = DiscoveryService()
        sock = FakeSock()
        service._targets = [_ad("192.0.2.40", "Gate")]
        service._senders = {"192.0.2.40": sock}
        service._announce("Hello")
        packet, addr = sock.sent[0]
        self.assertEqual(addr, ("239.255.255.250", 3702))
        self.assertIn(b"discovery/Hello", packet)
        self.assertIn(b"dn:NetworkVideoTransmitter", packet)


if __name__ == "__main__":
    unittest.main()
