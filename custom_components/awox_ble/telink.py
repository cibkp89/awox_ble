"""Telink BLE mesh protocol helpers (AwoX / EGLO "Connect" lamps).

Based on the protocol work in python-awox-mesh-light (Leiaz) and
fsaris/EspHome-AwoX-BLE-mesh-hub. Pure functions, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
STATUS_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1911"
COMMAND_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
OTA_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1913"
PAIR_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1914"

# Commands
C_POWER = 0xD0
C_REQUEST_STATUS = 0xDA
C_STATUS_REPORT = 0xDB
C_ONLINE_STATUS_REPORT = 0xDC
C_COLOR = 0xE2
C_WHITE_TEMPERATURE = 0xF0
C_WHITE_BRIGHTNESS = 0xF1
C_COLOR_BRIGHTNESS = 0xF2

# Pair responses
PAIR_OK = 0x0D
PAIR_FAILED = 0x0E

# Value ranges used by the lamps
WHITE_BRIGHTNESS_MIN = 0x01
WHITE_BRIGHTNESS_MAX = 0x7F
COLOR_BRIGHTNESS_MIN = 0x0A
COLOR_BRIGHTNESS_MAX = 0x64
TEMPERATURE_MIN = 0x00  # coldest
TEMPERATURE_MAX = 0x7F  # warmest

BROADCAST = 0xFFFF


def _aes_ecb(key: bytes, data: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(bytes(key)), modes.ECB()).encryptor()
    return encryptor.update(bytes(data)) + encryptor.finalize()


def encrypt(key: bytes, value: bytes) -> bytes:
    """Telink flavour of AES-128-ECB: key and data are byte-reversed."""
    if len(key) != 16:
        raise ValueError("key must be 16 bytes")
    k = bytes(reversed(bytes(key)))
    v = bytes(reversed(bytes(value).ljust(16, b"\x00")[:16]))
    return bytes(reversed(_aes_ecb(k, v)))


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _name_pass(mesh_name: bytes, mesh_password: bytes) -> bytes:
    return _xor(mesh_name.ljust(16, b"\x00"), mesh_password.ljust(16, b"\x00"))


def make_pair_packet(mesh_name: bytes, mesh_password: bytes, session_random: bytes) -> bytes:
    enc = encrypt(session_random.ljust(16, b"\x00"), _name_pass(mesh_name, mesh_password))
    return b"\x0c" + session_random + enc[:8]


def make_session_key(
    mesh_name: bytes, mesh_password: bytes, session_random: bytes, response_random: bytes
) -> bytes:
    return encrypt(_name_pass(mesh_name, mesh_password), session_random + response_random)


def _reversed_mac(mac: str) -> bytes:
    return bytes(reversed(bytes.fromhex(mac.replace(":", ""))))


def _checksum(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    check = encrypt(key, (nonce + bytes([len(payload)])).ljust(16, b"\x00"))
    for i in range(0, len(payload), 16):
        check = encrypt(key, _xor(check, payload[i : i + 16].ljust(16, b"\x00")))
    return check


def _crypt(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    base = bytearray(b"\x00" + nonce)
    base = base.ljust(16, b"\x00")
    out = bytearray()
    for i in range(0, len(payload), 16):
        out += _xor(encrypt(key, bytes(base)), payload[i : i + 16])
        base[0] += 1
    return bytes(out)


def make_command_packet(
    key: bytes, mac: str, dest: int, command: int, data: bytes, seq: bytes | None = None
) -> bytes:
    seq = seq if seq is not None else os.urandom(3)
    nonce = _reversed_mac(mac)[:4] + b"\x01" + seq
    payload = (struct.pack("<H", dest) + bytes([command]) + b"\x60\x01" + data).ljust(15, b"\x00")
    check = _checksum(key, nonce, payload)
    return seq + check[:2] + _crypt(key, nonce, payload)


def decrypt_packet(key: bytes, mac: str, packet: bytes) -> tuple[bytes, bool]:
    """Return (decrypted packet, checksum_ok)."""
    nonce = _reversed_mac(mac)[:3] + bytes(packet[:5])
    payload = _crypt(key, nonce, bytes(packet[7:]))
    check = _checksum(key, nonce, payload)
    return bytes(packet[:7]) + payload, check[:2] == bytes(packet[5:7])


def new_session_random() -> bytes:
    return os.urandom(8)


@dataclass
class LampStatus:
    mesh_id: int
    online: bool
    state: bool
    color_mode: bool
    sequence_mode: bool
    candle_mode: bool
    white_brightness: int
    temperature: int
    color_brightness: int
    red: int
    green: int
    blue: int
    source: str


def parse_status(packet: bytes) -> LampStatus | None:
    """Parse a decrypted 0xDB / 0xDC status packet."""
    if len(packet) < 20:
        return None
    cmd = packet[7]
    if cmd == C_ONLINE_STATUS_REPORT:
        mesh_id = packet[19] * 256 + packet[10]
        online = packet[11] != 0
        mode = packet[12]
        wb, temp, cb, r, g, b = packet[13:19]
        source = "notification"
    elif cmd == C_STATUS_REPORT:
        mesh_id = packet[4] * 256 + packet[3]
        online = True
        mode = packet[10]
        wb, temp, cb, r, g, b = packet[11:17]
        source = "status"
    else:
        return None
    return LampStatus(
        mesh_id=mesh_id,
        online=online,
        state=bool(mode & 1),
        color_mode=bool((mode >> 1) & 1),
        sequence_mode=bool((mode >> 2) & 1),
        candle_mode=bool((mode >> 4) & 1),
        white_brightness=wb,
        temperature=temp,
        color_brightness=cb,
        red=r,
        green=g,
        blue=b,
        source=source,
    )
