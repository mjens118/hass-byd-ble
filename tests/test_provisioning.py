"""The QR provisioning client, against a fake BYD server.

Two kinds of check, for the same reason as test_codec.py:
- Known-answer vectors, produced by running the upstream Java (CryptoUtils) and copied from the provisioning
  kit's tests. They catch a port that is self-consistent but wrong.
- A fake server that decrypts every request with the documented keys and refuses a bad signature, then
  answers with an encrypted envelope. It exercises the real request and response code end to end.

Nothing here talks to BYD.
"""
import json
import logging

import aiohttp
import pytest

from custom_components.byd_ble import provisioning as p

DKEY = "00112233445566778899AABBCCDDEEFF"
MAC = "AA:BB:CC:DD:EE:01"
ENC_TOKEN = "enc-token-SECRET-1"
SIGN_TOKEN = "sign-token-SECRET-2"
UNLOGIN = {"watch/login/create/qrcode", "watch/login/check/qrcode", "watch/login/gain/token"}


# ---- known answers from the upstream Java ----


def test_md5_upper():
    assert p.md5_upper("AU") == "6CC98BA2045FE4098D24356D14447085"


def test_sha1_mixed_matches_java_including_the_no_zero_pad_quirk():
    expected = [
        "5Bd8B1f54E4079a2C8149488FdcEBfaAFf3166e",
        "BEdd69f4F8587E89D691E063603aAB027efC3cf",
        "4Dd76A134F7b1Da12D5e4C103Edd612894bd8B2c",
        "16404cd6C357Be992f773a4B56A7645Da788f",
        "1468616d8Cc6F0c8C7e206f48dcC995748bB578",
        "D3a66C47AeaBF463C233B569Bbc53941EeeE4a2",
    ]
    for i, e in enumerate(expected):
        assert p.sha1_mixed(f"test-input-{i}") == e
    assert any(len(e) < 40 for e in expected)  # the quirk is actually exercised


def test_aes_matches_java():
    key = p.md5_key_bytes("AU")
    enc = p.aes_encrypt_hex('{"a":"1","uuid":"X"}', key)
    assert enc == "6C680B812026B73B897C03AF29260B8CC1544F2D1C9E2D4744E9D406DB1053EF"
    assert p.aes_decrypt_utf8(enc, key) == '{"a":"1","uuid":"X"}'


def test_qr_content_matches_java():
    assert p.build_qr_content("U1", "ABC", "AU") == (
        "watchQRCode://88EF2DBFC02075660EAA38E1BE99721A8DCF36F0AC275A9FCDA22E75A2F3C7F6"
        "DAB15A580E5684E5C0F8C9A11A9AE700"
    )


def test_signature_matches_the_upstream_java_computeSign():
    """Produced by running upstream's computeSign, sha1Mixed and md5Hex (copied verbatim) on these inputs.

    Without this the other signature tests only prove that the code agrees with itself.
    """
    unlogin = {
        "uuid": "UUID1", "timeStamp": "1700000000000", "timeZone": "Australia/Sydney", "identifier": "AU",
        "watchImei": "A" * 32, "language": "en", "countryCode": "AU", "emptyField": "",
    }
    assert p.compute_sign(unlogin, "AU") == "8E335F4fFF9a6FfaB6b99Cc93De83967ED1E873"
    login = {"vin": "LC0TESTVIN", "deviceType": "0", "userType": "0", "zeta": "last", "alpha": "first"}
    assert p.compute_sign(login, "sign-token-1") == "947b83b84813E7aCE6f3AdbAD5e269D8428a7"


def test_unlogin_request_shape_and_signature_recipe():
    ctx = p.Ctx("AU", "A" * 32)
    body, inner = p.build_unlogin(ctx, "token", "UUID1", now_ms=1700000000000)
    d = json.loads(body)
    assert list(d)[:3] == ["countryCode", "encryData", "identifier"]
    assert json.loads(p.aes_decrypt_utf8(d["encryData"], p.md5_key_bytes("AU"))) == inner
    assert inner == {"timeStamp": "1700000000000", "uuid": "UUID1", "timeZone": "Australia/Sydney"}
    fields = {
        **inner, "identifier": "AU", "watchImei": "A" * 32, "watchModel": p.WATCH_MODEL,
        "watchName": "SAMSUNGSM-R925N", "watchBrand": "SAMSUNG", "watchAppVersion": "341",
        "watchOs": "0", "reqTimestamp": "1700000000000", "language": "en", "countryCode": "AU",
    }
    assert d["sign"] == p.compute_sign(fields, "AU")


def test_login_identifier_prefers_identifier_then_user_id_then_country():
    ctx = p.Ctx("AU", "C" * 32)
    tok = {"encryToken": "e", "signToken": "s", "userId": "uid-123", "userType": "0"}
    assert json.loads(p.build_login(ctx, tok, {}, now_ms=1))["identifier"] == "uid-123"
    assert json.loads(p.build_login(ctx, tok, {}, now_ms=1, identifier="AU"))["identifier"] == "AU"
    tok["identifier"] = "ident-9"
    assert json.loads(p.build_login(ctx, tok, {}, now_ms=1))["identifier"] == "ident-9"


def test_shape_hides_values():
    s = p.shape({"dk": "SECRET", "n": [{"x": 1}]})
    assert "SECRET" not in json.dumps(s) and s == {"dk": "str", "n": [{"x": "int"}]}


def test_unsupported_country_is_rejected():
    with pytest.raises(ValueError):
        p.Ctx("ZZ", "A" * 32)


# ---- a fake BYD server ----


class FakeResponse:
    def __init__(self, text, status=200):
        self._text, self.status = text, status
        self.headers = {"Content-Type": "application/json"}

    async def read(self):
        return self._text.encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def text(self):
        return self._text


class FakeByd:
    """Checks every signature, decrypts every request, answers with an encrypted envelope."""

    def __init__(self, country="AU"):
        self.country = country
        self.statuses = ["0", "2"]  # what check/qrcode answers, in turn; the last one repeats
        self.calls = []
        self.seen_imei = set()
        self.http_error = None  # an aiohttp exception to raise instead of answering
        self.refuse = {}  # endpoint -> BYD error code
        self.accept_identifier = None  # None accepts any; a string refuses every other identifier
        self.token = {"encryToken": ENC_TOKEN, "signToken": SIGN_TOKEN, "userId": "user-1",
                      "userType": "0", "vin": "LC0TESTVIN"}
        self.send_token = True
        self.send_dkey = True
        self.raw_reply = None  # replaces the whole reply, to test unreadable answers
        self.scripted = {}  # endpoint -> raw replies to give first, in turn, before behaving normally
        self.info_ready = True  # False: the vehicle answer has watchBluetoothInfo = null, as seen on a real car
        self.bom = False  # prefix every reply with a byte order mark

    def session(self):
        return self

    def post(self, url, data, headers, timeout):
        if self.http_error:
            raise self.http_error
        endpoint = url.split(".auto/", 1)[1]
        self.calls.append(endpoint)
        text = self._handle(endpoint, json.loads(data.decode("utf-8")))
        return FakeResponse(("\ufeff" if self.bom else "") + text)

    @staticmethod
    def _error(code):
        return json.dumps({"response": json.dumps({"code": code, "message": "nope"})})

    @staticmethod
    def _reply(payload, key):
        inner = {"code": "0", "message": "ok", "respondData": p.aes_encrypt_hex(json.dumps(payload), key)}
        return json.dumps({"response": json.dumps(inner)})

    def _handle(self, endpoint, body):
        if self.raw_reply is not None:
            return self.raw_reply
        if self.scripted.get(endpoint):
            return self.scripted[endpoint].pop(0)
        if endpoint in self.refuse:
            return self._error(self.refuse[endpoint])
        outer = {k: v for k, v in body.items() if k not in ("encryData", "sign")}
        if endpoint in UNLOGIN:
            inner = json.loads(p.aes_decrypt_utf8(body["encryData"], p.md5_key_bytes(self.country)))
            if body["sign"] != p.compute_sign({**inner, **outer}, self.country):
                return self._error("1005")
            self.seen_imei.add(body["watchImei"])
            key = p.md5_key_bytes(self.country)
            if endpoint.endswith("create/qrcode"):
                return self._reply({"uuid": "QR-UUID-1"}, key)
            assert inner["uuid"] == "QR-UUID-1"
            if endpoint.endswith("check/qrcode"):
                status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
                return self._reply({"codeStatus": status}, key)
            return self._reply({"watchTokenInfo": self.token} if self.send_token else {}, key)

        inner = json.loads(p.aes_decrypt_utf8(body["encryData"], p.md5_key_bytes(ENC_TOKEN)))
        if body["sign"] != p.compute_sign({**inner, **outer}, SIGN_TOKEN):
            return self._error("1005")
        if self.accept_identifier is not None and body["identifier"] != self.accept_identifier:
            return self._error("9013")
        key = p.md5_key_bytes(ENC_TOKEN)
        if endpoint.endswith("gain/vehicle"):
            info = {"keyNumber": 2, "keyValidTo": 4133980799000} if self.info_ready else None
            return self._reply({"cfVechicle": {"watchBluetoothDto": {
                "macAddress": MAC, "watchBluetoothInfo": info}}}, key)
        payload = {"dk": DKEY, "empowerBluetoothKeyNo": 2} if self.send_dkey else {"unrelated": 1}
        return self._reply(payload, key)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(p, "KEY_RETRY_DELAY", 0)


def provisioner(byd, imei=None):
    return p.WatchProvisioner(byd.session(), "AU", imei)


# ---- behaviour ----


async def test_happy_path_from_qr_to_key():
    byd = FakeByd()
    prov = provisioner(byd)
    qr_uuid, content = await prov.async_create_qr()
    assert qr_uuid == "QR-UUID-1"
    assert content == p.build_qr_content(qr_uuid, prov.watch_imei, "AU")

    assert await prov.async_check_qr(qr_uuid) == p.PENDING
    assert await prov.async_check_qr(qr_uuid) == p.APPROVED

    key = await prov.async_fetch_key(qr_uuid)
    assert (key.dkey, key.key_number, key.mac, key.key_valid_to) == (DKEY, 2, MAC, 4133980799000)
    assert DKEY not in repr(key)  # the key cannot leak through a log line that prints the object
    assert "watch/login/gain/token" in byd.calls


@pytest.mark.parametrize("status", ["3", "4"])
async def test_rejected_or_expired_code(status):
    byd = FakeByd()
    byd.statuses = [status]
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    assert await prov.async_check_qr(qr_uuid) == p.REJECTED


async def test_server_error_code_is_reported_with_its_code():
    byd = FakeByd()
    byd.refuse["watch/login/create/qrcode"] = "1005"
    with pytest.raises(p.ServerRefused) as err:
        await provisioner(byd).async_create_qr()
    assert err.value.code == "1005"


@pytest.mark.parametrize("exc", [aiohttp.ClientConnectionError("down"), TimeoutError()])
async def test_unreachable_server(exc):
    byd = FakeByd()
    byd.http_error = exc
    with pytest.raises(p.ServerUnreachable):
        await provisioner(byd).async_create_qr()


@pytest.mark.parametrize(
    "raw,stage",
    [
        ("not json", "outer reply"),
        ("", "outer reply"),
        ("{}", None),
        ('{"response": "x"}', "envelope"),
        ('{"response": "{\\"code\\": \\"0\\", \\"respondData\\": \\"zz\\"}"}', "decryption"),
    ],
)
async def test_unreadable_answers_are_a_refusal_that_names_the_stage(raw, stage):
    byd = FakeByd()
    byd.raw_reply = raw
    with pytest.raises(p.ServerRefused) as err:
        await provisioner(byd).async_create_qr()
    assert stage is None or stage in err.value.server_message


async def test_a_byte_order_mark_does_not_break_a_reply():
    byd = FakeByd()
    byd.bom = True
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    assert (await prov.async_fetch_key(qr_uuid)).dkey == DKEY


async def test_a_key_that_is_not_ready_yet_is_asked_for_again():
    """As on a real car: the vehicle answer has no key block and the key call returns nothing at first."""
    byd = FakeByd()
    byd.info_ready = False
    byd.scripted["watch/login/gain/bluetooth"] = ["", ""]
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    key = await prov.async_fetch_key(qr_uuid)
    assert key.dkey == DKEY and key.mac == MAC and key.key_number == 2
    assert byd.calls.count("watch/login/gain/bluetooth") == 3


def _wrap(payload_text, key):
    """A success envelope whose encrypted payload is the given text, JSON or not."""
    inner = {"code": "0", "message": "success", "respondData": p.aes_encrypt_hex(payload_text, key)}
    return json.dumps({"response": json.dumps(inner)})


def test_an_empty_payload_is_no_data_not_an_error():
    key = p.md5_key_bytes(ENC_TOKEN)
    assert p.parse_response(_wrap("", key), key) is None


def test_a_payload_that_is_not_json_says_what_kind_but_never_what():
    key = p.md5_key_bytes(ENC_TOKEN)
    with pytest.raises(p.ServerRefused) as err:
        p.parse_response(_wrap(DKEY, key), key)
    assert "32 characters, hex digits only" in err.value.server_message
    assert DKEY not in err.value.server_message and DKEY not in str(err.value)
    with pytest.raises(p.ServerRefused) as err:
        p.parse_response(_wrap("hello there", key), key)
    assert "11 characters, other text" in err.value.server_message


async def test_a_key_call_that_answers_success_with_nothing_is_retried_then_reported(caplog):
    byd = FakeByd()
    byd.info_ready = False
    key = p.md5_key_bytes(ENC_TOKEN)
    byd.scripted["watch/login/gain/bluetooth"] = [_wrap("", key)] * 10
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    with caplog.at_level(logging.DEBUG), pytest.raises(p.NoKeyReturned):
        await prov.async_fetch_key(qr_uuid)
    assert byd.calls.count("watch/login/gain/bluetooth") == p.KEY_ATTEMPTS
    assert "no key" in caplog.text.lower()
    assert "watch identity: new" in caplog.text  # says whether a saved identity was reused, never what it is


async def test_a_key_that_never_arrives_gives_up_and_says_how_the_reply_looked(caplog):
    byd = FakeByd()
    byd.info_ready = False
    byd.scripted["watch/login/gain/bluetooth"] = [""] * 10
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    with caplog.at_level(logging.DEBUG), pytest.raises(p.NoKeyReturned):
        await prov.async_fetch_key(qr_uuid)
    assert byd.calls.count("watch/login/gain/bluetooth") == p.KEY_ATTEMPTS  # bounded, not endless
    assert "outer reply" in caplog.text and "0 characters, empty" in caplog.text and "HTTP 200" in caplog.text
    for secret in (DKEY, ENC_TOKEN, SIGN_TOKEN, prov.watch_imei):
        assert secret not in caplog.text


async def test_refused_key_calls_are_not_retried():
    byd = FakeByd()
    byd.accept_identifier = "nobody"
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    with pytest.raises(p.NoKeyReturned):
        await prov.async_fetch_key(qr_uuid)
    assert byd.calls.count("watch/login/gain/bluetooth") <= 3  # one per identifier, no repeat rounds


async def test_the_same_watch_identity_is_reused_and_a_new_one_is_random_hex():
    byd = FakeByd()
    await provisioner(byd, imei="CD" * 16).async_create_qr()
    assert byd.seen_imei == {"CD" * 16}

    a, b = provisioner(FakeByd()).watch_imei, provisioner(FakeByd()).watch_imei
    assert a != b
    for imei in (a, b):
        assert len(imei) == 32 and imei == imei.upper() and int(imei, 16) >= 0


async def test_identifier_fallback_like_upstream():
    """The token carries a userId; if BYD only accepts the country as the identifier, the second try works."""
    byd = FakeByd()
    byd.accept_identifier = "AU"
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    key = await prov.async_fetch_key(qr_uuid)
    assert key.dkey == DKEY


async def test_no_key_on_any_account_type_is_the_plain_error():
    byd = FakeByd()
    byd.send_dkey = False
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    with pytest.raises(p.NoKeyReturned):
        await prov.async_fetch_key(qr_uuid)


async def test_both_key_calls_refused_is_no_key():
    byd = FakeByd()
    byd.accept_identifier = "nobody"
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    with pytest.raises(p.NoKeyReturned):
        await prov.async_fetch_key(qr_uuid)


async def test_no_session_in_the_token_answer_is_no_key():
    byd = FakeByd()
    byd.send_token = False
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    with pytest.raises(p.NoKeyReturned):
        await prov.async_fetch_key(qr_uuid)


async def test_a_reply_with_no_key_logs_only_its_shape_never_a_secret(caplog):
    byd = FakeByd()
    byd.send_dkey = False
    prov = provisioner(byd)
    qr_uuid, _ = await prov.async_create_qr()
    with caplog.at_level(logging.DEBUG), pytest.raises(p.NoKeyReturned):
        await prov.async_fetch_key(qr_uuid)
    assert "shape" in caplog.text  # something useful for an issue report
    for secret in (DKEY, ENC_TOKEN, SIGN_TOKEN, MAC, prov.watch_imei, "LC0TESTVIN"):
        assert secret not in caplog.text


async def test_a_successful_run_logs_no_secret(caplog):
    byd = FakeByd()
    prov = provisioner(byd)
    with caplog.at_level(logging.DEBUG):
        qr_uuid, _ = await prov.async_create_qr()
        await prov.async_check_qr(qr_uuid)
        await prov.async_fetch_key(qr_uuid)
    for secret in (DKEY, ENC_TOKEN, SIGN_TOKEN, MAC, prov.watch_imei, "LC0TESTVIN"):
        assert secret not in caplog.text
