"""Protocol tests for custom_components/awox_ble/telink.py.

Runs without Home Assistant: telink.py only needs `cryptography`.

Golden vectors were generated with the reference implementation
fsaris/home-assistant-awox, awoxmeshlight/packetutils.py (pycryptodome), using the
fixed inputs below. Status packets are real (decrypted) notifications from an
EGLO Savatarila-C 99312, captured on 7 Nov 2026.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

_PATH = Path(__file__).parents[1] / "custom_components" / "awox_ble" / "telink.py"
_spec = importlib.util.spec_from_file_location("telink", _PATH)
telink = importlib.util.module_from_spec(_spec)
sys.modules["telink"] = telink  # needed by @dataclass
_spec.loader.exec_module(telink)

NAME = b"TestMesh"
PASSWORD = b"secret12"
SESSION_RANDOM = bytes.fromhex("0102030405060708")
RESPONSE_RANDOM = bytes.fromhex("1112131415161718")
MAC = "A4:C1:38:00:11:22"
SEQ = bytes.fromhex("0a0b0c")
MESH_ID = 18028

SESSION_KEY = bytes.fromhex("886ab80ea30c27f04ca42d1b4b449e73")


# --------------------------------------------------------------- crypto vs fsaris


def test_pair_packet_matches_reference() -> None:
    assert telink.make_pair_packet(NAME, PASSWORD, SESSION_RANDOM) == bytes.fromhex(
        "0c0102030405060708227dd77afdb4fc31"
    )


def test_session_key_matches_reference() -> None:
    key = telink.make_session_key(NAME, PASSWORD, SESSION_RANDOM, RESPONSE_RANDOM)
    assert key == SESSION_KEY


@pytest.mark.parametrize(
    ("command", "data", "expected"),
    [
        (telink.C_POWER, b"\x01\x00\x00", "0a0b0c6644590e34db0141bde03745af0b19ca4e"),
        (telink.C_WHITE_BRIGHTNESS, b"\x28", "0a0b0cce41590e15db0168bde03745af0b19ca4e"),
        (telink.C_COLOR, b"\x04\x11\x31\xff", "0a0b0c0c4b590e06db0144acd1c845af0b19ca4e"),
        (telink.C_REQUEST_STATUS, b"\x10", "0a0b0c8491590e3edb0150bde03745af0b19ca4e"),
    ],
)
def test_command_packet_matches_reference(command: int, data: bytes, expected: str) -> None:
    packet = telink.make_command_packet(SESSION_KEY, MAC, MESH_ID, command, data, seq=SEQ)
    assert packet == bytes.fromhex(expected)
    assert len(packet) == 20


def test_command_packet_uses_random_sequence() -> None:
    a = telink.make_command_packet(SESSION_KEY, MAC, MESH_ID, telink.C_POWER, b"\x01\x00\x00")
    b = telink.make_command_packet(SESSION_KEY, MAC, MESH_ID, telink.C_POWER, b"\x01\x00\x00")
    assert a[:3] != b[:3]


def test_decrypt_notification_matches_reference() -> None:
    encrypted = bytes.fromhex("48154400004ee44a254fac3eefb7630b032fef76")
    packet, ok = telink.decrypt_packet(SESSION_KEY, MAC, encrypted)
    assert ok
    assert packet == bytes.fromhex("48154400004ee4dc60016c1d014c323bf2fff946")


def test_decrypt_detects_corruption() -> None:
    encrypted = bytearray.fromhex("48154400004ee44a254fac3eefb7630b032fef76")
    encrypted[10] ^= 0xFF
    _, ok = telink.decrypt_packet(SESSION_KEY, MAC, bytes(encrypted))
    assert not ok


def test_encrypt_rejects_bad_key() -> None:
    with pytest.raises(ValueError):
        telink.encrypt(b"short", b"data")


# ------------------------------------------------------ status from a real lamp


def test_parse_online_status_white() -> None:
    st = telink.parse_status(bytes.fromhex("4815440000bd2ddc60016c1d014c323bf2fff946"))
    assert st is not None
    assert st.source == "notification"
    assert st.mesh_id == MESH_ID
    assert st.online and st.state and not st.color_mode
    assert (st.white_brightness, st.temperature, st.color_brightness) == (76, 50, 59)
    assert (st.red, st.green, st.blue) == (242, 255, 249)


def test_parse_online_status_off() -> None:
    st = telink.parse_status(bytes.fromhex("e0766100009eefdc60016c3a004e323df2fff946"))
    assert st is not None
    assert not st.state
    assert st.white_brightness == 78


def test_parse_online_status_rgb() -> None:
    st = telink.parse_status(bytes.fromhex("606efc0000fa4bdc60016c53034e323d1131ff46"))
    assert st is not None
    assert st.state and st.color_mode
    assert (st.red, st.green, st.blue) == (0x11, 0x31, 0xFF)


def test_parse_status_report() -> None:
    st = telink.parse_status(bytes.fromhex("b00e5c6c46ca68db6001014c323bf2fff9000000"))
    assert st is not None
    assert st.source == "status"
    assert st.mesh_id == MESH_ID
    assert st.state and not st.color_mode
    assert (st.white_brightness, st.temperature, st.color_brightness) == (76, 50, 59)


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        bytes(19),
        bytes.fromhex("4815440000bd2dd060016c1d014c323bf2fff946"),  # not a status command
    ],
)
def test_parse_ignores_other_packets(packet: bytes) -> None:
    assert telink.parse_status(packet) is None
