"""Get a Bluetooth key from BYD's servers by approving a QR code in the official BYD app.

Ported from PoorGrammerA/BydBleAutoLock (BydWatchKeyService.java, CryptoUtils.java), MIT licensed. See
THIRD_PARTY_NOTICES.md. Unofficial: it presents itself to BYD's servers as a Samsung smartwatch, exactly as
the upstream project does. No password is handled. The user approves a QR code in the BYD app, and only the
minimum needed for Bluetooth (address, key, key number, expiry) is returned. Session tokens and the
remote-control password are never kept.

Nothing here is sent to anyone but BYD, and nothing secret is logged: only response codes, and the shape
(key names and types, never values) of a response that held no key.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

# country -> (base url, timezone). AU and NZ share a server upstream. Only AU has been tried on a real car.
SERVERS: dict[str, tuple[str, str]] = {
    "AU": ("https://dilinkappoversea-au.byd.auto", "Australia/Sydney"),
    "NZ": ("https://dilinkappoversea-au.byd.auto", "Pacific/Auckland"),
    "KR": ("https://dilinkappoversea-kr-ali.byd.auto", "Asia/Seoul"),
}
VERIFIED_COUNTRIES = frozenset({"AU"})
DEFAULT_COUNTRY = "AU"

WATCH_MODEL = "SM-R925N"
WATCH_BRAND = "SAMSUNG"
WATCH_APP_VERSION = "341"  # upstream: matches official watch app v3.4.1
QR_TIMEOUT_SECONDS = 150
REQUEST_TIMEOUT_SECONDS = 15
# The key may not be ready the instant the code is approved. Ask again a couple of times before giving up.
KEY_ATTEMPTS = 3
KEY_RETRY_DELAY = 2.0

# codeStatus values seen from check/qrcode: 2 approved, 3 and 4 rejected or expired, anything else waiting.
STATUS_APPROVED = "2"
STATUS_REJECTED = ("3", "4")

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"


class ProvisionError(Exception):
    """Base class for everything that can go wrong while getting a key."""


class ServerUnreachable(ProvisionError):
    """BYD's server could not be reached (network, timeout or HTTP error)."""


class ServerRefused(ProvisionError):
    """BYD answered, but with an error code or something unreadable."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"BYD answered with code {code}")
        self.code = code
        self.server_message = message


class NoKeyReturned(ProvisionError):
    """The QR was approved but the answer held no usable Bluetooth key."""


@dataclass
class IssuedKey:
    """What BYD handed back. The key is kept out of repr so it cannot end up in a log by accident."""

    dkey: str = field(repr=False)
    key_number: int
    mac: str | None
    key_valid_to: Any = None


# --------------------------------------------------------------------------
# Crypto helpers (mirror CryptoUtils.java)
# --------------------------------------------------------------------------


def md5_upper(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()  # noqa: S324 - protocol requires MD5


def md5_key_bytes(value: str) -> bytes:
    return hashlib.md5(value.encode("utf-8")).digest()  # noqa: S324 - protocol requires MD5


def sha1_mixed(value: str) -> str:
    """Upstream quirk: hex per byte with NO zero padding, alternating case."""
    out = []
    for i, b in enumerate(hashlib.sha1(value.encode("utf-8")).digest()):  # noqa: S324 - protocol requires SHA-1
        h = format(b, "x")
        out.append(h.upper() if i % 2 == 0 else h.lower())
    return "".join(out)


def aes_encrypt_hex(plaintext: str, key: bytes) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return (enc.update(data) + enc.finalize()).hex().upper()


def aes_decrypt_utf8(cipher_hex: str, key: bytes) -> str:
    dec = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
    raw = dec.update(bytes.fromhex(cipher_hex)) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    return (unpad.update(raw) + unpad.finalize()).decode("utf-8")


def compact_json(d: dict) -> str:
    # Mirrors Gson JsonObject.toString() over a sorted TreeMap
    return json.dumps(dict(sorted(d.items())), separators=(",", ":"), ensure_ascii=False)


def compute_sign(fields: dict, password_source: str) -> str:
    parts = "".join(f"{k}={'' if v is None else v}&" for k, v in sorted(fields.items()) if k)
    return sha1_mixed(parts + "password=" + md5_upper(password_source))


def build_qr_content(uuid_: str, watch_imei: str, country: str) -> str:
    plain = f"watchImei={watch_imei}&uuid={uuid_}&countryCode={country}"
    return "watchQRCode://" + aes_encrypt_hex(plain, md5_key_bytes("watch.bydautolink"))


def new_watch_imei() -> str:
    """A random identifier for this Home Assistant install, standing in for a watch's IMEI."""
    return md5_upper(uuid.uuid4().hex.upper())


# --------------------------------------------------------------------------
# Request builders (mirror BydWatchKeyService.java)
# --------------------------------------------------------------------------


class Ctx:
    def __init__(self, country: str, watch_imei: str) -> None:
        if country not in SERVERS:
            raise ValueError(f"country {country} is not supported")
        self.country = country
        self.base_url, self.timezone = SERVERS[country]
        self.watch_imei = watch_imei
        self.language = "en" if country != "KR" else "ko"


def _rand() -> str:
    return uuid.uuid4().hex.upper()


def build_unlogin(ctx: Ctx, kind: str, qr_uuid: str | None = None, now_ms: int | None = None) -> tuple[str, dict]:
    """kind: 'create' | 'check' | 'token'. Returns (json body, inner params)."""
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    if kind == "create":
        inner = {"timeStamp": ts, "random": _rand(), "networkType": "wifi", "version": WATCH_APP_VERSION}
    elif kind == "check":
        inner = {
            "timeStamp": ts, "random": _rand(), "networkType": "wifi",
            "version": WATCH_APP_VERSION, "uuid": qr_uuid,
        }
    elif kind == "token":
        inner = {"timeStamp": ts, "uuid": qr_uuid, "timeZone": ctx.timezone}
    else:
        raise ValueError(kind)
    encry = aes_encrypt_hex(compact_json(inner), md5_key_bytes(ctx.country))
    outer_fields = {
        "identifier": ctx.country, "watchImei": ctx.watch_imei, "watchModel": WATCH_MODEL,
        "watchName": WATCH_BRAND + WATCH_MODEL, "watchBrand": WATCH_BRAND,
        "watchAppVersion": WATCH_APP_VERSION, "watchOs": "0", "reqTimestamp": ts,
        "language": ctx.language, "countryCode": ctx.country,
    }
    sign = compute_sign({**inner, **outer_fields}, ctx.country)
    body = {
        "countryCode": ctx.country, "encryData": encry, "identifier": ctx.country,
        "language": ctx.language, "reqTimestamp": ts, "sign": sign,
        "watchAppVersion": WATCH_APP_VERSION, "watchBrand": WATCH_BRAND,
        "watchImei": ctx.watch_imei, "watchModel": WATCH_MODEL,
        "watchName": WATCH_BRAND + WATCH_MODEL, "watchOs": "0",
    }
    return json.dumps(body, separators=(",", ":")), inner


def build_login(
    ctx: Ctx, token: dict, extra: dict, now_ms: int | None = None, identifier: str | None = None
) -> str:
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    inner = {
        "timeStamp": ts, "random": _rand(), "watchImei": ctx.watch_imei,
        "deviceType": "0", "networkType": "wifi", **extra,
    }
    encry = aes_encrypt_hex(compact_json(inner), md5_key_bytes(token["encryToken"]))
    # AU tokens carry userId but no "identifier"; upstream's KR flow uses the user id here.
    identifier = identifier or token.get("identifier") or token.get("userId") or ctx.country
    user_type = token.get("userType") or ""
    outer_fields = {
        "identifier": identifier, "watchModel": WATCH_MODEL, "watchName": WATCH_BRAND + WATCH_MODEL,
        "watchBrand": WATCH_BRAND, "watchAppVersion": WATCH_APP_VERSION, "watchOs": "0",
        "reqTimestamp": ts, "language": ctx.language, "countryCode": ctx.country, "userType": user_type,
    }
    sign = compute_sign({**inner, **outer_fields}, token["signToken"])
    body = {
        "identifier": identifier, "watchImei": ctx.watch_imei, "watchModel": WATCH_MODEL,
        "watchName": WATCH_BRAND + WATCH_MODEL, "watchBrand": WATCH_BRAND,
        "watchAppVersion": WATCH_APP_VERSION, "watchOs": "0", "reqTimestamp": ts,
        "language": ctx.language, "countryCode": ctx.country, "userType": user_type,
        "encryData": encry, "sign": sign,
    }
    return json.dumps(body, separators=(",", ":"))


def parse_response(raw_text: str, key: bytes) -> Any:
    """Unwrap BYD's envelope and decrypt the payload. Raises ServerRefused for an error or a bad shape."""
    stage = "outer reply"
    try:
        outer = json.loads(raw_text.lstrip("\ufeff"))
        inner_str = outer.get("response") or outer.get("respondData") or ""
        if not inner_str:
            raise ServerRefused("malformed", "no 'response' field")
        stage = "envelope"
        env = json.loads(inner_str)
        code = str(env.get("code", "-1"))
        if code != "0":
            raise ServerRefused(code, str(env.get("message", "")))
        data = env.get("respondData") or env.get("response") or ""
        if not data:
            return None
        stage = "decryption"
        plain = aes_decrypt_utf8(data, key)
        stage = "payload"
        plain = plain.lstrip("\ufeff")
        if not plain.strip():
            return None  # BYD said "success" and sent nothing, which is not the same as an unreadable reply
        try:
            return json.loads(plain)
        except ValueError as err:
            # Say what kind of thing it was (length and character kind only), never what it said.
            raise ServerRefused("unreadable", f"{type(err).__name__} in the payload ({describe_plain(plain)})") from err
    except (ValueError, AttributeError, TypeError) as err:  # JSON, hex, padding, UTF-8 and shape problems
        raise ServerRefused("unreadable", f"{type(err).__name__} in the {stage}") from err


def describe_plain(plain: str) -> str:
    """Length and kind of a decrypted payload that was not JSON. Never its content."""
    kind = "hex digits only" if all(c in "0123456789abcdefABCDEF" for c in plain.strip()) else "other text"
    return f"{len(plain.strip())} characters, {kind}"


def describe_reply(text: str) -> str:
    """How a reply looked, without its content: safe for a log or an issue."""
    stripped = text.lstrip("\ufeff").lstrip()
    if not stripped:
        kind = "empty"
    elif stripped[0] == "{":
        kind = "an object"
    elif stripped[0] == "[":
        kind = "a list"
    else:
        kind = "not JSON"
    return f"{len(text)} characters, {kind}"


def shape(obj: Any) -> Any:
    """Keys and types only, never values. Safe to put in a log or an issue."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0])] if obj else []
    return type(obj).__name__


def pick(d: Any, *names: str) -> Any:
    if not isinstance(d, dict):
        return None
    lower = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lower and lower[n.lower()] not in (None, ""):
            return lower[n.lower()]
    return None


# --------------------------------------------------------------------------
# The three steps, over Home Assistant's shared aiohttp session
# --------------------------------------------------------------------------


class WatchProvisioner:
    """Create a QR code, see whether it was approved, then collect the Bluetooth key."""

    def __init__(self, session: aiohttp.ClientSession, country: str, watch_imei: str | None = None) -> None:
        self._session = session
        self.identity_reused = bool(watch_imei)
        self.watch_imei = watch_imei or new_watch_imei()
        self._ctx = Ctx(country, self.watch_imei)
        self.last_http = ""

    async def _post(self, endpoint: str, body: str) -> str:
        """POST and return the body as text. Decoded as UTF-8 here, not guessed from headers."""
        try:
            async with self._session.post(
                f"{self._ctx.base_url}/{endpoint}",
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/json; charset=UTF-8", "User-Agent": "okhttp/4.12.0"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                headers = getattr(resp, "headers", None) or {}
                self.last_http = (
                    f"HTTP {resp.status}, content-type {headers.get('Content-Type')!r}, "
                    f"content-encoding {headers.get('Content-Encoding')!r}"
                )
                return (await resp.read()).decode("utf-8-sig", errors="replace")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ServerUnreachable(f"could not reach BYD ({type(err).__name__})") from err

    async def _call(self, endpoint: str, body: str, key: bytes) -> Any:
        text = await self._post(endpoint, body)
        try:
            return parse_response(text, key)
        except ServerRefused as err:
            if err.code in ("unreadable", "malformed"):
                err.server_message = f"{err.server_message}; reply was {describe_reply(text)}; {self.last_http}"
            raise

    async def _try(self, endpoint: str, body: str, key: bytes) -> Any:
        """Like upstream: a refused call is noted and is not fatal. An unreachable server still is."""
        try:
            return await self._call(endpoint, body, key)
        except ServerRefused as err:
            _LOGGER.warning("BYD refused %s: code=%s message=%s", endpoint, err.code, err.server_message)
            return None

    async def async_create_qr(self) -> tuple[str, str]:
        """Returns (uuid, text to show as a QR code)."""
        body, _ = build_unlogin(self._ctx, "create")
        resp = await self._call("watch/login/create/qrcode", body, md5_key_bytes(self._ctx.country))
        qr_uuid = pick(resp, "uuid")
        if not qr_uuid:
            _LOGGER.warning("BYD returned no QR id, response shape: %s", shape(resp))
            raise ServerRefused("no_uuid")
        qr_uuid = str(qr_uuid)
        return qr_uuid, build_qr_content(qr_uuid, self._ctx.watch_imei, self._ctx.country)

    async def async_check_qr(self, qr_uuid: str) -> str:
        """PENDING, APPROVED or REJECTED (rejected includes expired)."""
        body, _ = build_unlogin(self._ctx, "check", qr_uuid)
        resp = await self._call("watch/login/check/qrcode", body, md5_key_bytes(self._ctx.country))
        status = str(pick(resp, "codeStatus", "status"))
        if status == STATUS_APPROVED:
            return APPROVED
        if status in STATUS_REJECTED:
            return REJECTED
        return PENDING

    async def async_fetch_key(self, qr_uuid: str) -> IssuedKey:
        """After approval: swap the QR for a session, then ask for the vehicle and the Bluetooth key."""
        ctx = self._ctx
        body, _ = build_unlogin(ctx, "token", qr_uuid)
        tok_resp = await self._call("watch/login/gain/token", body, md5_key_bytes(ctx.country))
        token = tok_resp.get("watchTokenInfo") if isinstance(tok_resp, dict) else None
        if not isinstance(token, dict) or not token.get("encryToken") or not token.get("signToken"):
            raise NoKeyReturned("BYD did not return a session")
        tkey = md5_key_bytes(token["encryToken"])
        user_type = str(token.get("userType") or "")

        extra = {"appVersion": "2", "vin": token.get("vin", "")}
        candidates = [(n, token[n]) for n in ("identifier", "userId") if token.get(n)] + [("country", ctx.country)]
        veh = blue = None
        accepted: tuple[str, Any] | None = None
        for attempt in range(KEY_ATTEMPTS):
            for name, ident in [accepted] if accepted else candidates:
                v = await self._try("watch/login/gain/vehicle", build_login(ctx, token, extra, identifier=ident), tkey)
                b = await self._try("watch/login/gain/bluetooth", build_login(ctx, token, extra, identifier=ident), tkey)
                veh = v if v is not None else veh
                blue = b if b is not None else blue
                if v is not None or b is not None:
                    accepted = (name, ident)
                    break
            if accepted is None:
                raise NoKeyReturned("both key calls were refused")
            issued = self._read_key(veh, blue)
            if issued is not None:
                return issued
            if attempt < KEY_ATTEMPTS - 1:
                _LOGGER.debug("No key yet (attempt %s of %s), asking again", attempt + 1, KEY_ATTEMPTS)
                await asyncio.sleep(KEY_RETRY_DELAY)

        _LOGGER.warning(
            "BYD approved the code but sent no key after %s tries (watch identity: %s, account type: %s). "
            "Vehicle response shape: %s. Key response shape: %s",
            KEY_ATTEMPTS, "reused" if self.identity_reused else "new", user_type or "unknown",
            shape(veh), shape(blue),
        )
        raise NoKeyReturned("no key in the response")

    @staticmethod
    def _read_key(veh: Any, blue: Any) -> IssuedKey | None:
        """The key out of the two answers, or None if neither holds one (yet)."""
        dto = pick(pick(veh, "cfVechicle"), "watchBluetoothDto") or {}
        info = pick(dto, "watchBluetoothInfo") or {}
        dkey = pick(blue, "dk", "dkey") or pick(info, "dkey")
        if not dkey:
            return None
        mac = pick(dto, "macAddress") or pick(blue, "bluetoothMacAddress", "macAddress", "mac")
        key_no = pick(blue, "empowerBluetoothKeyNo", "keyNumber", "keyNo")
        if key_no is None:
            key_no = pick(info, "keyNumber")
        valid_to = pick(info, "keyValidTo") or pick(blue, "empowerExpireTime")
        try:
            number = int(key_no) if key_no is not None else 0
        except (TypeError, ValueError) as err:
            raise NoKeyReturned("unreadable key number") from err
        return IssuedKey(dkey=str(dkey), key_number=number, mac=str(mac) if mac else None, key_valid_to=valid_to)
