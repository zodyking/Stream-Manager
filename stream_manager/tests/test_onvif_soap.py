import base64
import unittest

from onvif_proxy.discovery import build_probe_match, probe_relevant, scopes_match
from onvif_proxy.models import Endpoints, camera_from_dict
from onvif_proxy.onvif_soap import (
    build_operation,
    digest_response,
    dispatch,
    operation_name,
    password_digest,
    wsse_ok,
)

ENDPOINTS = Endpoints(
    device="http://192.0.2.10:8080/onvif/cam1/device_service",
    media="http://192.0.2.10:8080/onvif/cam1/media_service",
    stream="rtsp://192.0.2.10:8554/cam1",
    snapshot="http://192.0.2.10:8080/onvif/cam1/snapshot.jpg",
)


def camera():
    return camera_from_dict(
        {
            "id": "cam1",
            "name": "Lab",
            "listen_ip": "192.0.2.10",
            "fallback_source_id": "src",
            "sources": [{"id": "src", "type": "rtsp", "url": "rtsp://camera.example/stream"}],
        }
    )


def envelope(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


class OnvifSoapTests(unittest.TestCase):
    def test_probe_match_contains_xaddr(self):
        packet = build_probe_match(
            "uuid:probe-1",
            "11111111-1111-1111-1111-111111111111",
            ["onvif://www.onvif.org/Profile/Streaming"],
            ENDPOINTS.device,
        ).decode()
        self.assertIn("<d:XAddrs>" + ENDPOINTS.device + "</d:XAddrs>", packet)
        self.assertIn("ProbeMatches", packet)
        self.assertIn("uuid:probe-1", packet)

    def test_probe_filter(self):
        self.assertTrue(probe_relevant("<d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>"))
        self.assertFalse(probe_relevant("<d:ProbeMatches></d:ProbeMatches>"))
        self.assertFalse(probe_relevant("<d:Probe><d:Types>dn:Printer</d:Types></d:Probe>"))
        scopes = ["onvif://www.onvif.org/Profile/Streaming"]
        self.assertTrue(scopes_match("<d:Scopes>onvif://www.onvif.org/Profile</d:Scopes>", scopes))
        self.assertFalse(scopes_match("<d:Scopes>onvif://www.onvif.org/name/other</d:Scopes>", scopes))

    def test_handshake_capabilities_profiles_stream(self):
        cam = camera()
        capabilities = build_operation("GetCapabilities", cam, ENDPOINTS, "")
        self.assertIn(ENDPOINTS.device, capabilities)
        self.assertIn(ENDPOINTS.media, capabilities)
        profiles_xml = build_operation(
            "GetProfiles",
            cam,
            ENDPOINTS,
            envelope("<trt:GetProfiles/>"),
        )
        self.assertIn('token="main"', profiles_xml)
        self.assertIn("<tt:Width>1920</tt:Width>", profiles_xml)
        self.assertIn("<tt:H264Profile>Main</tt:H264Profile>", profiles_xml)
        self.assertIn("<tt:FrameRateLimit>15</tt:FrameRateLimit>", profiles_xml)
        self.assertIn("<tt:BitrateLimit>2048</tt:BitrateLimit>", profiles_xml)
        stream_xml = build_operation("GetStreamUri", cam, ENDPOINTS, envelope("<trt:GetStreamUri/>"))
        self.assertIn(ENDPOINTS.stream, stream_xml)
        self.assertIn("<tt:GovLength>30</tt:GovLength>", profiles_xml)
        self.assertIn("RTP_RTSP_TCP>true", capabilities)
        encoder_xml = build_operation("GetVideoEncoderConfigurations", cam, ENDPOINTS, "")
        self.assertIn("<tt:Encoding>H264</tt:Encoding>", encoder_xml)
        self.assertIn("<tt:H264Profile>Main</tt:H264Profile>", encoder_xml)
        self.assertIn('token="vec_1"', encoder_xml)
        silent_audio = build_operation("GetAudioSources", cam, ENDPOINTS, "")
        self.assertNotIn("AAC", build_operation("GetProfiles", cam, ENDPOINTS, envelope("<trt:GetProfiles/>")))
        self.assertNotIn("not implemented", silent_audio)
        self.assertNotIn("as_1", silent_audio)
        profiles_xml = build_operation(
            "GetProfiles",
            cam,
            ENDPOINTS,
            envelope("<trt:GetProfiles/>"),
            audio=True,
        )
        self.assertIn("<tt:Encoding>AAC</tt:Encoding>", profiles_xml)
        self.assertIn('token="aec_1"', profiles_xml)
        audio = build_operation("GetAudioSources", cam, ENDPOINTS, "", audio=True)
        self.assertIn('token="as_1"', audio)
        self.assertIn("<tt:Channels>2</tt:Channels>", audio)
        self.assertNotIn("not implemented", audio)
        audio_encoder = build_operation("GetAudioEncoderConfigurations", cam, ENDPOINTS, "", audio=True)
        self.assertIn("<tt:Encoding>AAC</tt:Encoding>", audio_encoder)
        self.assertIn("<tt:SampleRate>48</tt:SampleRate>", audio_encoder)
        outputs = build_operation("GetAudioOutputs", cam, ENDPOINTS, "")
        self.assertNotIn("AAC", outputs)
        self.assertNotIn("not implemented", outputs)
        self.assertEqual(operation_name(envelope("<tds:GetCapabilities/>")), "GetCapabilities")

    def test_custom_output_is_advertised(self):
        cam = camera_from_dict(
            {
                "id": "cam1",
                "name": "Lab",
                "listen_ip": "192.0.2.10",
                "fallback_source_id": "src",
                "sources": [{"id": "src", "type": "rtsp", "url": "rtsp://camera.example/stream"}],
                "output": {
                    "width": 1280,
                    "height": 720,
                    "framerate": 10,
                    "bitrate_kbps": 1024,
                    "h264_profile": "High",
                    "gov_length": 20,
                },
            }
        )
        profiles_xml = build_operation("GetProfiles", cam, ENDPOINTS, envelope("<trt:GetProfiles/>"))
        self.assertIn("<tt:Width>1280</tt:Width>", profiles_xml)
        self.assertIn("<tt:Height>720</tt:Height>", profiles_xml)
        self.assertIn("<tt:FrameRateLimit>10</tt:FrameRateLimit>", profiles_xml)
        self.assertIn("<tt:BitrateLimit>1024</tt:BitrateLimit>", profiles_xml)
        self.assertIn("<tt:GovLength>20</tt:GovLength>", profiles_xml)
        self.assertIn("<tt:H264Profile>High</tt:H264Profile>", profiles_xml)
        self.assertIn('fixed="true"', profiles_xml)

    def test_dispatch_round_trip(self):
        cam = camera()
        result = dispatch(
            envelope("<tds:GetServices/>"),
            cam,
            ENDPOINTS,
            method="POST",
            authorization="",
            nonce="abc",
        )
        self.assertEqual(result.status, 200)
        self.assertIn(ENDPOINTS.media, result.body)
        self.assertIn(ENDPOINTS.device, result.body)

    def test_digest_rfc2617_vector(self):
        response = digest_response(
            "Mufasa",
            "testrealm@host.com",
            "Circle Of Life",
            "dcd98b7102dd2f0e8b11d0f600bfb0c093",
            "GET",
            "/dir/index.html",
            "auth",
            "00000001",
            "0a4f113b",
        )
        self.assertEqual(response, "6629fae49393a05397450978507c4ef1")

    def test_username_token_and_protected_stream_uri(self):
        cam = camera()
        cam.username = "user"
        cam.password = "secret"
        nonce = base64.b64encode(b"nonce-bytes").decode()
        created = "2026-01-01T00:00:00Z"
        digest = password_digest(b"nonce-bytes", created, "secret")
        token = (
            "<wsse:Security>"
            "<wsse:UsernameToken>"
            "<wsse:Username>user</wsse:Username>"
            f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
            f"<wsse:Nonce>{nonce}</wsse:Nonce>"
            f"<wsu:Created>{created}</wsu:Created>"
            "</wsse:UsernameToken></wsse:Security>"
        )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"<s:Header>{token}</s:Header><s:Body><trt:GetStreamUri/></s:Body></s:Envelope>"
        )
        self.assertTrue(wsse_ok(body, "user", "secret"))
        open_call = dispatch(envelope("<trt:GetStreamUri/>"), cam, ENDPOINTS, method="POST", authorization="", nonce="n1")
        self.assertEqual(open_call.status, 401)
        authed = dispatch(body, cam, ENDPOINTS, method="POST", authorization="", nonce="n1")
        self.assertEqual(authed.status, 200)
        self.assertIn(ENDPOINTS.stream, authed.body)
        pre = dispatch(envelope("<tds:GetSystemDateAndTime/>"), cam, ENDPOINTS, method="POST", authorization="", nonce="n1")
        self.assertEqual(pre.status, 200)
        stale = body.replace("<trt:GetStreamUri/>", "<trt:GetSystemDateAndTime/>")
        stale = stale.replace(digest, password_digest(b"nonce-bytes", created, "old"))
        self.assertEqual(dispatch(stale, cam, ENDPOINTS, method="POST", authorization="", nonce="n1").status, 200)

    def test_protect_sees_two_profiles_and_the_camera_mac(self):
        cam = camera()
        cam.mac = "02:11:22:33:44:55"
        profiles_xml = build_operation("GetProfiles", cam, ENDPOINTS, envelope("<trt:GetProfiles/>"))
        self.assertIn('token="main"', profiles_xml)
        self.assertIn('token="sub"', profiles_xml)
        sub = build_operation("GetProfile", cam, ENDPOINTS, envelope("<trt:GetProfile><trt:ProfileToken>sub</trt:ProfileToken></trt:GetProfile>"))
        self.assertIn('<trt:Profile token="sub"', sub)
        nic = build_operation("GetNetworkInterfaces", cam, ENDPOINTS, "")
        self.assertIn("<tt:HwAddress>02:11:22:33:44:55</tt:HwAddress>", nic)
        info = build_operation("GetDeviceInformation", cam, ENDPOINTS, "")
        self.assertIn("<tds:HardwareId>021122334455</tds:HardwareId>", info)
        clock = build_operation("GetSystemDateAndTime", cam, ENDPOINTS, "")
        self.assertIn("<tt:LocalDateTime>", clock)


if __name__ == "__main__":
    unittest.main()
