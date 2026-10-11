import tempfile
import unittest
from pathlib import Path

from onvif_proxy.aliases import AddressBook, place_camera
from onvif_proxy.models import Camera, endpoints_for


class AliasTests(unittest.TestCase):
    def test_allocate_skips_a_live_address(self):
        with tempfile.TemporaryDirectory() as folder:
            book = AddressBook(Path(folder) / "aliases.json", run=lambda args: _ok())
            taken = set()

            def probe(ip: str) -> bool:
                return ip == "192.168.0.250"

            address = book.allocate("wlp0s20f0u1", "192.168.0.26", 24, "cam", probe=probe, present=taken)
            self.assertEqual(address, "192.168.0.249")
            self.assertEqual(book._entries["192.168.0.249"]["camera_id"], "cam")

    def test_virtual_camera_uses_a_plain_onvif_service(self):
        camera = Camera(id="676d2b88f4d6", name="Printer", listen_ip="192.168.0.249", virtual_ip=True, service_port=80)
        urls = endpoints_for(camera, 8080, 8554)
        self.assertEqual(urls.device, "http://192.168.0.249/onvif/device_service")
        self.assertEqual(urls.media, "http://192.168.0.249/onvif/media_service")
        self.assertEqual(urls.stream, "rtsp://192.168.0.249:8554/676d2b88f4d6")

    def test_same_interface_keeps_the_reserved_address(self):
        with tempfile.TemporaryDirectory() as folder:
            book = AddressBook(Path(folder) / "aliases.json", run=lambda args: _ok())
            previous = Camera(
                id="cam",
                name="Printer",
                interface="wlp0s20f0u1",
                listen_ip="192.168.0.240",
                virtual_ip=True,
                service_port=80,
            )
            camera = Camera(id="cam", name="Printer", interface="wlp0s20f0u1", listen_ip="0.0.0.0")
            place_camera(
                camera,
                previous,
                [{"name": "wlp0s20f0u1", "ip": "192.168.0.26", "prefix": "24"}],
                book,
                probe=lambda ip: False,
            )
            self.assertEqual(camera.listen_ip, "192.168.0.240")
            self.assertTrue(camera.virtual_ip)

    def test_loopback_does_not_reserve_an_address(self):
        with tempfile.TemporaryDirectory() as folder:
            book = AddressBook(Path(folder) / "aliases.json", run=lambda args: _ok())
            camera = Camera(id="cam", name="Printer", interface="lo", listen_ip="127.0.0.1")
            place_camera(camera, None, [{"name": "lo", "ip": "127.0.0.1", "prefix": "8"}], book)
            self.assertFalse(camera.virtual_ip)
            self.assertEqual(camera.listen_ip, "127.0.0.1")
            self.assertEqual(book._entries, {})


def _ok():
    class Result:
        returncode = 0
        stderr = ""

    return Result()


if __name__ == "__main__":
    unittest.main()
