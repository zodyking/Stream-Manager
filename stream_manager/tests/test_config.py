import unittest

from onvif_proxy.config import pick_port


class PortTests(unittest.TestCase):
    def test_keeps_the_configured_port_when_free(self):
        self.assertEqual(pick_port(8554, (8564,), check=lambda port, host: True), 8554)

    def test_moves_to_the_first_free_fallback_when_taken(self):
        busy = {8554, 8564}
        self.assertEqual(pick_port(8554, (8564, 8574, 18554), check=lambda port, host: port not in busy), 8574)


if __name__ == "__main__":
    unittest.main()
