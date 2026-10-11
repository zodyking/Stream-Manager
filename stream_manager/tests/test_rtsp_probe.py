import base64
import socket
import struct
import threading
import unittest

from onvif_proxy.rtsp_probe import h264_size, probe

SPS = base64.b64decode("Z01AKdoB4AiflwEQAAADABAAAAMB6PGDKg==")


def _rtp(payload: bytes, marker: int, stamp: int) -> bytes:
    header = struct.pack("!BBHII", 0x80, (marker << 7) | 96, 1, stamp, 7)
    return header + payload


def _send(conn, payload: bytes, marker: int, stamp: int) -> None:
    packet = _rtp(payload, marker, stamp)
    conn.sendall(b"$" + bytes([0]) + struct.pack("!H", len(packet)) + packet)


def _read_request(conn) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        data += conn.recv(4096)


class ProbeTests(unittest.TestCase):
    def test_sps_reports_the_coded_size(self):
        self.assertEqual(h264_size(SPS), (1920, 1080))

    def test_probe_reads_frames_and_counts_slices(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve() -> None:
            conn, _ = server.accept()
            _read_request(conn)
            body = b"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\n"
            conn.sendall(
                b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Base: rtsp://127.0.0.1/live/\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            _read_request(conn)
            conn.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 2\r\nSession: 1\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n")
            _read_request(conn)
            conn.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 3\r\nSession: 1\r\n\r\n")
            _send(conn, SPS, 0, 1000)
            _send(conn, bytes([0x65, 0x88]), 0, 1000)
            _send(conn, bytes([0x65, 0x89]), 1, 1000)
            _send(conn, bytes([0x65, 0x88]), 1, 2000)

        threading.Thread(target=serve, daemon=True).start()
        result = probe(f"rtsp://user:secret@127.0.0.1:{port}/live", timeout=5)
        server.close()
        self.assertNotIn("secret", result["url"])
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["codec"], "H264")
        self.assertEqual((result["width"], result["height"]), (1920, 1080))
        self.assertGreaterEqual(result["frames"], 1)
        self.assertEqual(result["slices"], 2)

    def test_unreachable_stream_reports_an_error(self):
        result = probe("rtsps://user:secret@127.0.0.1:1/live", timeout=2)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        self.assertNotIn("secret", result["url"])
        self.assertNotIn("secret", result["error"])


if __name__ == "__main__":
    unittest.main()
