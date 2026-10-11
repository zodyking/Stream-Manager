"""ONVIF Profile S SOAP and WS-Security helpers."""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .models import Camera, Endpoints, xml_escape

PRE_AUTH = {
    "GetSystemDateAndTime",
    "GetCapabilities",
    "GetServices",
    "GetServiceCapabilities",
    "GetHostname",
    "GetWsdlUrl",
}

PROFILE_TOKEN = "main"
SUB_TOKEN = "sub"
FIRMWARE = "1.0.11"


@dataclass
class SoapResult:
    status: int
    body: str
    www_authenticate: str | None = None


def envelope(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:ter="http://www.onvif.org/ver10/error">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


def fault(reason: str, code: str = "ter:ActionNotSupported") -> str:
    return envelope(
        "<s:Fault><s:Code><s:Value>s:Sender</s:Value>"
        f"<s:Subcode><s:Value>{code}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="en">{xml_escape(reason)}</s:Text></s:Reason>'
        "</s:Fault>"
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def operation_name(xml: str) -> str:
    root = ET.fromstring(xml)
    for element in root.iter():
        if local_name(element.tag) != "Body":
            continue
        for child in list(element):
            name = local_name(child.tag)
            if name in {"Security", "UsernameToken"}:
                continue
            return name
        raise ValueError("SOAP body is empty")
    raise ValueError("SOAP body is missing")


def _text(xml: str, name: str) -> str:
    match = re.search(rf"<(?:\w+:)?{name}[^>]*>([^<]*)</(?:\w+:)?{name}>", xml)
    return match.group(1).strip() if match else ""


def password_digest(nonce_raw: bytes, created: str, password: str) -> str:
    digest = hashlib.sha1(nonce_raw + created.encode("utf-8") + password.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def wsse_ok(xml: str, username: str, password: str) -> bool | None:
    """Return True/False when a UsernameToken is present, else None."""
    if "UsernameToken" not in xml:
        return None
    user = _text(xml, "Username")
    if user != username:
        return False
    password_node = re.search(
        r"<(?:\w+:)?Password([^>]*)>([^<]*)</(?:\w+:)?Password>",
        xml,
    )
    nonce_b64 = _text(xml, "Nonce")
    created = _text(xml, "Created")
    if not password_node:
        return False
    attrs, presented = password_node.group(1), password_node.group(2).strip()
    if "PasswordText" in attrs or ("Type" not in attrs and nonce_b64 == ""):
        return presented == password
    try:
        nonce_raw = base64.b64decode(nonce_b64)
    except (ValueError, TypeError):
        return False
    return presented == password_digest(nonce_raw, created, password)


def digest_response(
    username: str,
    realm: str,
    password: str,
    nonce: str,
    method: str,
    uri: str,
    qop: str = "",
    nc: str = "",
    cnonce: str = "",
) -> str:
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        material = f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}"
    else:
        material = f"{ha1}:{nonce}:{ha2}"
    return hashlib.md5(material.encode()).hexdigest()


def _header_map(header: str) -> dict[str, str]:
    parts = header.split(" ", 1)
    body = parts[1] if len(parts) > 1 else header
    found: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', body):
        found[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return found


def digest_ok(authorization: str, method: str, username: str, password: str, nonce: str) -> bool:
    if not authorization.lower().startswith("digest "):
        return False
    fields = _header_map(authorization)
    if fields.get("username") != username or fields.get("nonce") != nonce:
        return False
    expected = digest_response(
        username,
        fields.get("realm", "onvif-proxy"),
        password,
        nonce,
        method,
        fields.get("uri", "/"),
        fields.get("qop", ""),
        fields.get("nc", ""),
        fields.get("cnonce", ""),
    )
    return fields.get("response", "").lower() == expected.lower()


def _media_uri(uri: str) -> str:
    return (
        "<tt:Uri>" + xml_escape(uri) + "</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
        "<tt:Timeout>PT60S</tt:Timeout>"
    )


def _source_configuration(camera: Camera) -> str:
    output = camera.output
    return (
        '<tt:Name>VideoSource</tt:Name><tt:UseCount>2</tt:UseCount>'
        "<tt:SourceToken>vs_1</tt:SourceToken>"
        f'<tt:Bounds x="0" y="0" width="{output.width}" height="{output.height}"/>'
    )


def _encoder_configuration(camera: Camera) -> str:
    output = camera.output
    return (
        "<tt:Name>H264</tt:Name><tt:UseCount>2</tt:UseCount>"
        "<tt:Encoding>H264</tt:Encoding>"
        f"<tt:Resolution><tt:Width>{output.width}</tt:Width><tt:Height>{output.height}</tt:Height></tt:Resolution>"
        "<tt:Quality>5</tt:Quality>"
        f"<tt:RateControl><tt:FrameRateLimit>{output.framerate}</tt:FrameRateLimit>"
        f"<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{output.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
        f"<tt:H264><tt:GovLength>{output.gov_length}</tt:GovLength>"
        f"<tt:H264Profile>{xml_escape(output.h264_profile)}</tt:H264Profile></tt:H264>"
        "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
        "<tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
        "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
    )


def _named_config(operation: str, token: str, body: str) -> str:
    return envelope(
        f'<trt:{operation}Response><trt:Configurations token="{token}">'
        + body
        + f"</trt:Configurations></trt:{operation}Response>"
    )


def _profile_xml(camera: Camera, token: str = PROFILE_TOKEN, tag: str = "trt:Profiles") -> str:
    label = "MainStream" if token == PROFILE_TOKEN else "SubStream"
    return (
        f'<{tag} token="{token}" fixed="true">'
        f"<tt:Name>{label}</tt:Name>"
        '<tt:VideoSourceConfiguration token="vsc_1">'
        + _source_configuration(camera)
        + "</tt:VideoSourceConfiguration>"
        '<tt:VideoEncoderConfiguration token="vec_1">'
        + _encoder_configuration(camera)
        + "</tt:VideoEncoderConfiguration>"
        f"</{tag}>"
    )


def _scopes(camera: Camera) -> list[tuple[str, str]]:
    from urllib.parse import quote

    items = [
        ("Fixed", "onvif://www.onvif.org/Profile/Streaming"),
        ("Fixed", "onvif://www.onvif.org/Profile/S"),
        ("Fixed", "onvif://www.onvif.org/type/video_encoder"),
        ("Fixed", "onvif://www.onvif.org/type/NetworkVideoTransmitter"),
        ("Configurable", "onvif://www.onvif.org/name/" + quote(camera.name, safe="")),
        ("Configurable", "onvif://www.onvif.org/location/virtual"),
    ]
    for extra in camera.scopes_extra:
        items.append(("Configurable", extra))
    return items


def build_operation(operation: str, camera: Camera, endpoints: Endpoints, xml: str) -> str:
    if operation == "GetSystemDateAndTime":
        now = datetime.now(timezone.utc)
        stamp = (
            f"<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute>"
            f"<tt:Second>{now.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
            f"<tt:Day>{now.day}</tt:Day></tt:Date>"
        )
        return envelope(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
            f"<tt:UTCDateTime>{stamp}</tt:UTCDateTime><tt:LocalDateTime>{stamp}</tt:LocalDateTime>"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )
    if operation == "GetCapabilities":
        return envelope(
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>{xml_escape(endpoints.device)}</tt:XAddr>"
            "<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
            "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>"
            "<tt:RemoteDiscovery>true</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>"
            "<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
            "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tt:SupportedVersions>"
            "</tt:System></tt:Device>"
            f"<tt:Media><tt:XAddr>{xml_escape(endpoints.media)}</tt:XAddr>"
            "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities>"
            "<tt:Extension><tt:ProfileCapabilities><tt:MaximumNumberOfProfiles>2</tt:MaximumNumberOfProfiles>"
            "</tt:ProfileCapabilities></tt:Extension></tt:Media>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )
    if operation == "GetServices":
        return envelope(
            "<tds:GetServicesResponse>"
            "<tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>"
            f"<tds:XAddr>{xml_escape(endpoints.device)}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>"
            "<tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>"
            f"<tds:XAddr>{xml_escape(endpoints.media)}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>6</tt:Minor></tds:Version></tds:Service>"
            "</tds:GetServicesResponse>"
        )
    if operation == "GetServiceCapabilities":
        return envelope("<tds:GetServiceCapabilitiesResponse><tds:Capabilities></tds:Capabilities></tds:GetServiceCapabilitiesResponse>")
    if operation == "GetHostname":
        return envelope(
            "<tds:GetHostnameResponse><tds:HostnameInformation>"
            "<tt:FromDHCP>false</tt:FromDHCP>"
            f"<tt:Name>{xml_escape(camera.name)}</tt:Name>"
            "</tds:HostnameInformation></tds:GetHostnameResponse>"
        )
    if operation == "GetDeviceInformation":
        return envelope(
            "<tds:GetDeviceInformationResponse>"
            "<tds:Manufacturer>Stream Manager</tds:Manufacturer>"
            f"<tds:Model>{xml_escape(camera.name or 'Virtual Camera')}</tds:Model>"
            f"<tds:FirmwareVersion>{FIRMWARE}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>SM-{xml_escape(camera.id.upper())}</tds:SerialNumber>"
            f"<tds:HardwareId>{xml_escape((camera.mac or camera.id).replace(':', '').upper())}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )
    if operation == "GetScopes":
        chunks = []
        for definition, item in _scopes(camera):
            chunks.append(
                "<tds:Scopes><tt:ScopeDef>"
                + definition
                + "</tt:ScopeDef><tt:ScopeItem>"
                + xml_escape(item)
                + "</tt:ScopeItem></tds:Scopes>"
            )
        return envelope("<tds:GetScopesResponse>" + "".join(chunks) + "</tds:GetScopesResponse>")
    if operation in {"SetScopes", "AddScopes", "RemoveScopes"}:
        _apply_scopes(camera, operation, xml)
        return envelope(f"<tds:{operation}Response/>")
    if operation == "GetDiscoveryMode":
        mode = camera.discovery_mode or "Discoverable"
        return envelope(f"<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>{xml_escape(mode)}</tds:DiscoveryMode></tds:GetDiscoveryModeResponse>")
    if operation == "SetDiscoveryMode":
        mode = _text(xml, "DiscoveryMode") or "Discoverable"
        camera.discovery_mode = mode
        return envelope("<tds:SetDiscoveryModeResponse/>")
    if operation == "GetNetworkInterfaces":
        return envelope(
            '<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token="eth0">'
            "<tt:Enabled>true</tt:Enabled>"
            + (
                f"<tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>{xml_escape(camera.mac)}</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info>"
                if camera.mac
                else ""
            )
            + "<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>"
            "<tt:Manual><tt:Address>"
            + xml_escape(camera.listen_ip)
            + f"</tt:Address><tt:PrefixLength>{int(camera.prefix_length or 24)}</tt:PrefixLength></tt:Manual>"
            "<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>"
        )
    if operation == "GetProfiles":
        return envelope(
            "<trt:GetProfilesResponse>"
            + _profile_xml(camera, PROFILE_TOKEN)
            + _profile_xml(camera, SUB_TOKEN)
            + "</trt:GetProfilesResponse>"
        )
    if operation == "GetProfile":
        token = SUB_TOKEN if _text(xml, "ProfileToken") == SUB_TOKEN else PROFILE_TOKEN
        return envelope("<trt:GetProfileResponse>" + _profile_xml(camera, token, "trt:Profile") + "</trt:GetProfileResponse>")
    if operation == "GetVideoSources":
        output = camera.output
        return envelope(
            '<trt:GetVideoSourcesResponse><trt:VideoSources token="vs_1">'
            f"<tt:Framerate>{output.framerate}</tt:Framerate>"
            f"<tt:Resolution><tt:Width>{output.width}</tt:Width><tt:Height>{output.height}</tt:Height></tt:Resolution>"
            "</trt:VideoSources></trt:GetVideoSourcesResponse>"
        )
    if operation in {"GetVideoSourceConfigurations", "GetVideoSourceConfiguration"}:
        return _named_config(operation, "vsc_1", _source_configuration(camera))
    if operation in {"GetVideoEncoderConfigurations", "GetVideoEncoderConfiguration", "GetCompatibleVideoEncoderConfigurations"}:
        reported = "GetVideoEncoderConfigurations" if operation == "GetCompatibleVideoEncoderConfigurations" else operation
        return _named_config(reported, "vec_1", _encoder_configuration(camera))
    if operation == "GetVideoEncoderConfigurationOptions":
        return envelope(
            "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
            "<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:QualityRange>"
            "<tt:H264>"
            + "".join(
                f"<tt:ResolutionsAvailable><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height></tt:ResolutionsAvailable>"
                for width, height in ((1920, 1080), (1280, 720), (640, 480), (640, 360))
            )
            + "<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>120</tt:Max></tt:GovLengthRange>"
            "<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>30</tt:Max></tt:FrameRateRange>"
            "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
            "<tt:H264ProfilesSupported>Baseline</tt:H264ProfilesSupported>"
            "<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>"
            "<tt:H264ProfilesSupported>High</tt:H264ProfilesSupported>"
            "</tt:H264></trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>"
        )
    if operation in {
        "GetAudioSources",
        "GetAudioOutputs",
        "GetAudioSourceConfigurations",
        "GetAudioEncoderConfigurations",
        "GetAudioOutputConfigurations",
        "GetAudioDecoderConfigurations",
        "GetOSDs",
        "GetMetadataConfigurations",
        "GetVideoAnalyticsConfigurations",
    }:
        return envelope(f"<trt:{operation}Response/>")
    if operation == "GetStreamUri":
        return envelope("<trt:GetStreamUriResponse><trt:MediaUri>" + _media_uri(endpoints.stream) + "</trt:MediaUri></trt:GetStreamUriResponse>")
    if operation == "GetSnapshotUri":
        return envelope(
            "<trt:GetSnapshotUriResponse><trt:MediaUri>" + _media_uri(endpoints.snapshot) + "</trt:MediaUri></trt:GetSnapshotUriResponse>"
        )
    return fault(f"{operation} is not implemented")


def _apply_scopes(camera: Camera, operation: str, xml: str) -> None:
    found = re.findall(r"<(?:\w+:)?ScopeItem[^>]*>([^<]*)</(?:\w+:)?ScopeItem>", xml)
    items = [item.strip() for item in found if item.strip()]
    if not items:
        items = re.findall(r"<(?:\w+:)?Scopes[^>]*>([^<]*)</(?:\w+:)?Scopes>", xml)
        items = [item.strip() for item in items if item.strip()]
    if operation == "SetScopes":
        camera.scopes_extra = items
    elif operation == "AddScopes":
        for item in items:
            if item not in camera.scopes_extra:
                camera.scopes_extra.append(item)
    elif operation == "RemoveScopes":
        camera.scopes_extra = [item for item in camera.scopes_extra if item not in items]


def dispatch(
    xml: str,
    camera: Camera,
    endpoints: Endpoints,
    *,
    method: str,
    authorization: str,
    nonce: str,
) -> SoapResult:
    try:
        operation = operation_name(xml)
    except (ET.ParseError, ValueError) as exc:
        return SoapResult(400, fault(str(exc)))
    if camera.username and operation not in PRE_AUTH:
        token = wsse_ok(xml, camera.username, camera.password)
        if token is False:
            return SoapResult(400, fault("invalid username token", "ter:NotAuthorized"))
        if token is None:
            if not digest_ok(authorization, method, camera.username, camera.password, nonce):
                challenge = (
                    f'Digest realm="onvif-proxy", nonce="{nonce}", qop="auth", algorithm=MD5, '
                    f'opaque="{uuid.uuid4().hex}"'
                )
                return SoapResult(401, fault("not authorized", "ter:NotAuthorized"), challenge)
    return SoapResult(200, build_operation(operation, camera, endpoints, xml))
