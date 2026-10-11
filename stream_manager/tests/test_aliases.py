import tempfile
import unittest
from pathlib import Path

from onvif_proxy.aliases import AddressBook, link_name, place_camera, virtual_mac
from onvif_proxy.models import Camera, endpoints_for

WIRED = [{"name": "eth0", "ip": "192.168.0.26", "prefix": "24"}]
WIFI = [{"name": "wlp0s20f0u1", "ip": "192.168.0.26", "prefix": "24", "wireless": True}]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        return _ok()

    def joined(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


def book(folder: str, run: Recorder, sysctl=lambda path, value: True) -> AddressBook:
    return AddressBook(Path(folder) / "aliases.json", run=run, sysctl=sysctl, gateway=lambda iface: "192.168.0.1")


def wifi(name: str) -> bool:
    return name.startswith("wl")


class AliasTests(unittest.TestCase):
    def test_allocate_skips_a_live_address(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Recorder()
            address = book(folder, run).allocate(
                "eth0", "192.168.0.26", 24, "cam", probe=lambda ip: ip == "192.168.0.250", present=set()
            )
            self.assertEqual(address, "192.168.0.249")

    def test_wired_camera_gets_its_own_mac_and_route(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Recorder()
            camera = Camera(id="676d2b88f4d6", name="Printer", interface="eth0", listen_ip="0.0.0.0")
            place_camera(camera, None, WIRED, book(folder, run), probe=lambda ip: False, wireless=wifi)
            self.assertEqual(camera.link, "smv676d2b88f4d6")
            self.assertEqual(camera.mac, virtual_mac("676d2b88f4d6"))
            self.assertTrue(camera.mac.startswith("02:"))
            calls = run.joined()
            self.assertIn(
                f"ip link add smv676d2b88f4d6 link eth0 address {camera.mac} type macvlan mode bridge", calls
            )
            self.assertIn("ip addr add 192.168.0.250/24 dev smv676d2b88f4d6 noprefixroute", calls)
            self.assertTrue(any(call.startswith("ip rule add from 192.168.0.250 lookup") for call in calls))
            self.assertTrue(any("default via 192.168.0.1 dev smv676d2b88f4d6" in call for call in calls))

    def test_each_camera_has_a_different_mac(self):
        self.assertNotEqual(virtual_mac("aaaaaaaaaaaa"), virtual_mac("bbbbbbbbbbbb"))
        self.assertLessEqual(len(link_name("aaaaaaaaaaaa")), 15)

    def test_wifi_falls_back_to_a_shared_mac(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Recorder()
            camera = Camera(id="cam", name="Printer", interface="wlp0s20f0u1", listen_ip="0.0.0.0")
            place_camera(
                camera, None, WIFI, book(folder, run), probe=lambda ip: False, wireless=wifi, hardware=lambda name: "aa:bb:cc:dd:ee:ff"
            )
            self.assertEqual(camera.link, "")
            self.assertEqual(camera.mac, "aa:bb:cc:dd:ee:ff")
            self.assertIn("ip addr add 192.168.0.250/24 dev wlp0s20f0u1", run.joined())

    def test_virtual_camera_uses_a_plain_onvif_service(self):
        camera = Camera(id="676d2b88f4d6", name="Printer", listen_ip="192.168.0.249", virtual_ip=True, service_port=80)
        urls = endpoints_for(camera, 8080, 8554)
        self.assertEqual(urls.device, "http://192.168.0.249/onvif/device_service")
        self.assertEqual(urls.media, "http://192.168.0.249/onvif/media_service")
        self.assertEqual(urls.stream, "rtsp://192.168.0.249:8554/676d2b88f4d6")

    def test_legacy_shared_address_moves_to_its_own_mac_and_is_announced(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Recorder()
            announced = []
            addresses = AddressBook(
                Path(folder) / "aliases.json",
                run=run,
                sysctl=lambda path, value: True,
                gateway=lambda iface: "192.168.0.1",
                announce=lambda link, mac, ip: announced.append((link, mac, ip)),
            )
            addresses._entries["192.168.0.249"] = {"interface": "eth0", "prefix": 24, "camera_id": "cam", "link": "", "mac": ""}
            legacy = Camera(id="cam", name="Printer", interface="eth0", listen_ip="192.168.0.249", virtual_ip=True, service_port=80)
            camera = Camera(**{**legacy.__dict__})
            place_camera(camera, legacy, WIRED, addresses, probe=lambda ip: True, wireless=wifi)
            self.assertEqual(camera.listen_ip, "192.168.0.249")
            self.assertEqual(camera.link, "smvcam")
            self.assertIn("ip addr del 192.168.0.249/24 dev eth0", run.joined())
            self.assertEqual(announced, [("smvcam", virtual_mac("cam"), "192.168.0.249")])

    def test_same_interface_keeps_the_reserved_address(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = Camera(
                id="cam", name="Printer", interface="eth0", listen_ip="192.168.0.240", virtual_ip=True, service_port=80
            )
            camera = Camera(id="cam", name="Printer", interface="eth0", listen_ip="0.0.0.0")
            place_camera(camera, previous, WIRED, book(folder, Recorder()), probe=lambda ip: True, wireless=wifi)
            self.assertEqual(camera.listen_ip, "192.168.0.240")
            self.assertTrue(camera.virtual_ip)

    def test_requested_address_is_used_and_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            addresses = book(folder, Recorder())
            camera = Camera(id="cam", name="Printer", interface="eth0", listen_ip="0.0.0.0", requested_ip="192.168.0.60")
            place_camera(camera, None, WIRED, addresses, probe=lambda ip: False, wireless=wifi)
            self.assertEqual(camera.listen_ip, "192.168.0.60")

            other = Camera(id="other", name="Door", interface="eth0", listen_ip="0.0.0.0", requested_ip="192.168.0.60")
            with self.assertRaisesRegex(ValueError, "another camera"):
                place_camera(other, None, WIRED, addresses, probe=lambda ip: False, wireless=wifi)
            outside = Camera(id="x", name="Yard", interface="eth0", listen_ip="0.0.0.0", requested_ip="10.0.0.5")
            with self.assertRaisesRegex(ValueError, "not a usable address"):
                place_camera(outside, None, WIRED, addresses, probe=lambda ip: False, wireless=wifi)
            busy = Camera(id="y", name="Gate", interface="eth0", listen_ip="0.0.0.0", requested_ip="192.168.0.61")
            with self.assertRaisesRegex(ValueError, "answers on the network"):
                place_camera(busy, None, WIRED, addresses, probe=lambda ip: True, wireless=wifi)

    def test_arp_isolation_falls_back_to_nftables(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Recorder()
            addresses = book(folder, run, sysctl=lambda path, value: False)
            addresses.ensure("eth0", "192.168.0.250", 24, "cam", link="smvcam", mac="02:00:00:00:00:01")
            self.assertEqual(addresses.arp_mode, "nftables")
            self.assertTrue(any(call[:2] == ["nft", "-f"] for call in run.calls))

    def test_moving_from_shared_to_own_mac_drops_the_old_address(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Recorder()
            addresses = book(folder, run)
            addresses.ensure("eth0", "192.168.0.250", 24, "cam")
            addresses.ensure("eth0", "192.168.0.250", 24, "cam", link="smvcam", mac="02:00:00:00:00:01")
            self.assertIn("ip addr del 192.168.0.250/24 dev eth0", run.joined())

    def test_restore_tolerates_an_address_that_is_already_assigned(self):
        class Assigned(Recorder):
            def __call__(self, args):
                self.calls.append(list(args))
                result = _ok()
                if args[1:3] == ["addr", "add"]:
                    result.returncode = 2
                    result.stderr = "Error: ipv4: Address already assigned."
                return result

        with tempfile.TemporaryDirectory() as folder:
            run = Assigned()
            addresses = book(folder, run)
            addresses._entries["192.168.0.249"] = {"interface": "eth0", "prefix": 24, "camera_id": "cam", "link": "smvcam", "mac": "02:00:00:00:00:01"}
            with self.assertNoLogs("onvif_proxy.aliases", level="WARNING"):
                addresses.restore()
            self.assertTrue(any(call[:3] == ["ip", "rule", "add"] for call in run.calls))

    def test_loopback_does_not_reserve_an_address(self):
        with tempfile.TemporaryDirectory() as folder:
            addresses = book(folder, Recorder())
            camera = Camera(id="cam", name="Printer", interface="lo", listen_ip="127.0.0.1")
            place_camera(camera, None, [{"name": "lo", "ip": "127.0.0.1", "prefix": "8"}], addresses)
            self.assertFalse(camera.virtual_ip)
            self.assertEqual(camera.listen_ip, "127.0.0.1")
            self.assertEqual(addresses.entries(), {})


def _ok():
    class Result:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    return Result()


if __name__ == "__main__":
    unittest.main()
