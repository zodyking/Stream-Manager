import os
import unittest
from pathlib import Path

os.environ["HASP_CANVAS_WRITERS"] = "0"

from onvif_proxy.compositor import (
    canvas_video_args,
    choose_writer,
    compositor_input,
    escape_drawtext,
    slate_command,
)
from onvif_proxy.ha_client import Playback
from onvif_proxy.models import OutputSettings, camera_from_dict, camera_to_dict
from onvif_proxy.stream_manager import StreamManager
from onvif_proxy.engine import RecordingEngine


class FakeHa:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}

    async def playback_for_entity(self, entity_id: str) -> Playback:
        return Playback(None, None)


def sample(**overrides) -> dict:
    raw = {
        "id": "desk",
        "name": "Desk",
        "listen_ip": "127.0.0.1",
        "fallback_source_id": "live",
        "cycle_enabled": True,
        "sources": [{"id": "live", "type": "rtsp", "name": "Printer", "url": "rtsp://printer.example/live"}],
        "layers": [
            {"id": "tile", "type": "source", "source_id": "live", "x": 0.05, "y": 0.08, "w": 0.6, "h": 0.7, "z": 1},
            {"id": "label", "type": "text", "text": "Room: 1", "x": 0.08, "y": 0.8, "w": 0.4, "h": 0.12, "z": 2},
            {"id": "shade", "type": "box", "x": 0, "y": 0, "w": 1, "h": 0.12, "z": 0, "fill": "#102C46", "opacity": 0.5},
        ],
    }
    raw.update(overrides)
    return raw


class CanvasModelTests(unittest.TestCase):
    def test_layers_round_trip(self):
        camera = camera_from_dict(sample())
        again = camera_from_dict(camera_to_dict(camera))
        self.assertEqual([layer.id for layer in again.layers], ["tile", "label", "shade"])
        self.assertEqual(again.layers[1].text, "Room: 1")
        self.assertEqual(again.layers[0].w, 0.6)
        self.assertEqual(again.layers[2].opacity, 0.5)

    def test_four_source_limit(self):
        layers = [
            {"id": f"s{index}", "type": "source", "source_id": "live", "x": 0, "y": 0, "w": 0.2, "h": 0.2, "z": index}
            for index in range(5)
        ]
        with self.assertRaises(ValueError):
            camera_from_dict(sample(layers=layers, cycle_enabled=False))


class CanvasCommandTests(unittest.TestCase):
    def test_slate_and_encoder_stay_protect_safe(self):
        command = " ".join(slate_command(640, 360, 15, 41000))
        self.assertIn("No source", command)
        self.assertIn("0x071320", command)
        video = canvas_video_args(OutputSettings())
        self.assertIn("sliced-threads=0:slices=1", video)
        self.assertIn("-bf 0", video)

    def test_published_graph_ignores_the_source_address(self):
        camera = camera_from_dict(sample())
        first = compositor_input(camera)
        second = compositor_input(camera)
        self.assertEqual(first, second)
        self.assertNotIn("printer.example", first)
        self.assertNotIn("No source", first)
        self.assertIn("[vout]", first)
        self.assertIn("overlay=", first)
        self.assertIn("Room\\: 1", first)
        camera.layers[0].x = 0.2
        self.assertNotEqual(first, compositor_input(camera))

    def test_drawtext_escapes_colons(self):
        self.assertIn("\\:", escape_drawtext("a:b"))

    def test_slot_picks_slate_when_live_is_missing(self):
        self.assertEqual(choose_writer(True, ["live"], ["slate"])[0], "live")
        self.assertEqual(choose_writer(False, ["live"], ["slate"])[0], "slate")
        self.assertEqual(choose_writer(True, None, ["slate"])[0], "slate")


class CanvasPublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_canvas_stays_published_when_switching_would_change_a_single_source(self):
        camera = camera_from_dict(sample())
        engine = RecordingEngine()
        streams = StreamManager(engine, ui_port=8099, onvif_port=8080, rtsp_port=8554, data_dir=Path("."))
        await streams.sync([camera], {}, FakeHa())
        published = engine.streams["desk"]
        self.assertTrue(published.startswith("ffmpeg:canvas#input="))
        self.assertNotIn("printer.example", published)
        self.assertNotIn("No source", published)
        self.assertNotRegex(published, r"\s")
        self.assertNotIn("width=", published)
        self.assertNotIn("#audio=", published)
        self.assertIn("sliced-threads=0:slices=1", "\n".join(engine.templates.values()))
        self.assertEqual(streams.runtime["desk"]["reason"], "canvas")
        self.assertFalse(streams.runtime["desk"]["has_audio"])

        await streams.sync([camera], {}, FakeHa())
        self.assertEqual(engine.streams["desk"], published)

        camera.layers = []
        await streams.sync([camera], {}, FakeHa())
        self.assertIn("printer.example", engine.streams["desk"])
        self.assertNotEqual(engine.streams["desk"], published)
