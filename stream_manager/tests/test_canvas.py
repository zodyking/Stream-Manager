import os
import unittest
from pathlib import Path
from unittest import mock

os.environ["HASP_CANVAS_WRITERS"] = "0"

from onvif_proxy.compositor import (
    canvas_video_args,
    choose_writer,
    compositor_input,
    escape_drawtext,
    resolve_text,
    slate_command,
)
from onvif_proxy.ha_client import Playback
from onvif_proxy.models import OutputSettings, camera_from_dict, camera_to_dict
from onvif_proxy.rules import Decision
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
        self.assertTrue(all(layer.layout_source == "live" for layer in again.layers))

    def test_four_source_limit(self):
        layers = [
            {"id": f"s{index}", "type": "source", "source_id": "live", "x": 0, "y": 0, "w": 0.2, "h": 0.2, "z": index}
            for index in range(5)
        ]
        with self.assertRaises(ValueError):
            camera_from_dict(sample(layers=layers, cycle_enabled=False))

    def test_each_switch_can_hold_four_sources(self):
        layers = []
        for source_id, start in (("live", 0), ("still", 4)):
            layers.extend(
                {
                    "id": f"s{start + index}",
                    "type": "source",
                    "source_id": "live",
                    "layout_source": source_id,
                    "x": 0,
                    "y": 0,
                    "w": 0.2,
                    "h": 0.2,
                    "z": index,
                }
                for index in range(4)
            )
        camera = camera_from_dict(
            sample(
                cycle_enabled=False,
                sources=[
                    {"id": "live", "type": "rtsp", "name": "Printer", "url": "rtsp://printer.example/live"},
                    {"id": "still", "type": "rtsp", "name": "Idle", "url": "rtsp://printer.example/idle"},
                ],
                layers=layers,
            )
        )
        self.assertEqual(len(camera.layers), 8)


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

    def test_text_uses_the_source_name_for_the_active_layout(self):
        camera = camera_from_dict(
            sample(
                sources=[
                    {"id": "live", "type": "rtsp", "name": "Printer", "url": "rtsp://printer.example/live"},
                    {"id": "still", "type": "rtsp", "name": "Idle", "url": "rtsp://printer.example/idle"},
                ],
                layers=[
                    {"id": "label", "type": "text", "layout_source": "live", "text": "{name}", "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.2, "z": 1},
                    {"id": "other", "type": "text", "layout_source": "still", "text": "Bay closed", "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.2, "z": 1},
                ],
            )
        )
        live = compositor_input(camera, "live")
        still = compositor_input(camera, "still")
        self.assertIn("Printer", live)
        self.assertNotIn("{name}", live)
        self.assertNotIn("Bay closed", live)
        self.assertIn("Bay closed", still)
        self.assertNotIn("Printer", still)
        self.assertEqual(resolve_text("{camera}", camera, "live"), "Desk")

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
        self.assertEqual(streams.runtime["desk"]["reason"], "fallback")
        self.assertEqual(streams.runtime["desk"]["source_id"], "live")
        self.assertTrue(streams.runtime["desk"]["designed"])
        self.assertFalse(streams.runtime["desk"]["has_audio"])

        await streams.sync([camera], {}, FakeHa())
        self.assertEqual(engine.streams["desk"], published)

        camera.layers = []
        await streams.sync([camera], {}, FakeHa())
        self.assertIn("printer.example", engine.streams["desk"])
        self.assertNotEqual(engine.streams["desk"], published)

    async def test_switching_publishes_the_layout_for_that_source(self):
        camera = camera_from_dict(
            sample(
                cycle_seconds=30,
                sources=[
                    {"id": "live", "type": "rtsp", "name": "Printer", "url": "rtsp://printer.example/live"},
                    {"id": "still", "type": "rtsp", "name": "Idle", "url": "rtsp://printer.example/idle"},
                ],
                layers=[
                    {"id": "live-label", "type": "text", "layout_source": "live", "text": "{name}", "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.2, "z": 1},
                    {"id": "still-label", "type": "text", "layout_source": "still", "text": "Bay closed", "x": 0.2, "y": 0.2, "w": 0.4, "h": 0.2, "z": 1},
                ],
            )
        )
        engine = RecordingEngine()
        streams = StreamManager(engine, ui_port=8099, onvif_port=8080, rtsp_port=8554, data_dir=Path("."))
        with mock.patch("onvif_proxy.stream_manager.cycle_decision", return_value=Decision("live", None, "cycle")):
            await streams.sync([camera], {}, FakeHa())
        live = engine.streams["desk"]
        self.assertTrue(live.startswith("ffmpeg:canvas#input="))
        self.assertIn("Printer", "\n".join(engine.templates.values()))
        self.assertNotIn("Bay closed", "\n".join(engine.templates.values()))
        self.assertEqual(streams.runtime["desk"]["reason"], "cycle")
        self.assertEqual(streams.runtime["desk"]["source_id"], "live")

        with mock.patch("onvif_proxy.stream_manager.cycle_decision", return_value=Decision("still", None, "cycle")):
            await streams.sync([camera], {}, FakeHa())
        still = engine.streams["desk"]
        self.assertNotEqual(still, live)
        self.assertIn("Bay closed", "\n".join(engine.templates.values()))
        self.assertEqual(streams.runtime["desk"]["source_id"], "still")

        plain = camera_from_dict(
            sample(
                id="desk",
                rules_enabled=True,
                cycle_enabled=False,
                fallback_source_id="live",
                sources=[
                    {"id": "live", "type": "rtsp", "name": "Printer", "url": "rtsp://printer.example/live"},
                    {"id": "plain", "type": "rtsp", "name": "Plain", "url": "rtsp://printer.example/plain"},
                ],
                rules=[{"id": "r1", "entity_id": "switch.mode", "operator": "eq", "value": "plain", "source_id": "plain"}],
                layers=[
                    {"id": "live-label", "type": "text", "layout_source": "live", "text": "Designed", "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.2, "z": 1},
                ],
            )
        )
        await streams.sync([plain], {"switch.mode": "plain"}, FakeHa())
        self.assertIn("printer.example/plain", engine.streams["desk"])
        self.assertEqual(streams.runtime["desk"]["reason"], "rule")
        self.assertFalse(streams.runtime["desk"].get("designed"))
