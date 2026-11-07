"""Connection handling for a single Telink/AwoX lamp via HA Bluetooth (incl. ESPHome proxies)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import time
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from . import telink

_LOGGER = logging.getLogger(__name__)

CONNECT_LOCK_TIMEOUT = 120
# Background reconnect: single 20 s connect attempts with a short pause in between. While an
# attempt is pending the proxy catches the lamp's first advertisement after power-on (wall switch).
RECONNECT_DELAYS = (2, 5)
BACKGROUND_CONNECT_ATTEMPTS = 1


class AwoxError(Exception):
    """Base error."""


class AwoxNotFound(AwoxError):
    """Lamp not visible to any connectable Bluetooth adapter/proxy."""


class AwoxAuthError(AwoxError):
    """Mesh credentials rejected."""


class AwoxLamp:
    """One AwoX/EGLO Connect (Telink mesh) lamp."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        mesh_name: str,
        mesh_password: str,
        mesh_id: int | None = None,
        keep_connected: bool = True,
    ) -> None:
        self.hass = hass
        self.mac = mac.upper()
        self._mesh_name = mesh_name.encode()
        self._mesh_password = mesh_password.encode()
        self.mesh_id = mesh_id
        self.keep_connected = keep_connected

        self._client: BleakClient | None = None
        self._session_key: bytes | None = None
        self._lock = asyncio.Lock()
        self._stopping = False
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempt = 0
        self._listeners: list[Callable[[], None]] = []

        self._adv_unsub: Callable[[], None] | None = None
        self._last_adv_at: float | None = None
        self._last_attempt_at: float | None = None

        self.notify_mode: str = "none"
        self.via: str | None = None
        self.status: telink.LampStatus | None = None
        self.last_status_at: float | None = None

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected and self._session_key)

    @property
    def tag(self) -> str:
        return f"[{self.mac}]"

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    async def async_start(self) -> None:
        self._stopping = False
        self._adv_unsub = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.mac, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        if self.keep_connected and not self.connected:
            self._schedule_reconnect(initial=True)

    async def async_stop(self) -> None:
        self._stopping = True
        if getattr(self, "_adv_unsub", None):
            self._adv_unsub()
            self._adv_unsub = None
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        await self._disconnect()

    @callback
    def _on_advertisement(
        self, info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        """The lamp only accepts connections while it advertises: use that as the trigger."""
        now = time.monotonic()
        if self._last_adv_at is not None and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "%s advertisement via %s rssi %s (%.1fs since last)%s",
                self.tag, info.source, info.rssi, now - self._last_adv_at,
                "" if self.connected else " - not connected",
            )
        self._last_adv_at = now
        if not self.keep_connected or self._stopping or self.connected or self._lock.locked():
            return
        if self._last_attempt_at is not None and now - self._last_attempt_at < 5:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            # A delayed retry is pending - start now instead, while the lamp is advertising.
            self._reconnect_task.cancel()
        self._reconnect_task = self.hass.async_create_background_task(
            self._reconnect_after(0), f"awox_ble adv-reconnect {self.mac}"
        )

    async def async_command(self, command: int, data: bytes) -> None:
        """Send one command, connecting first if needed (one retry on failure)."""
        for attempt in (1, 2):
            try:
                await self._ensure_connected()
                await self._write_command(command, data)
                return
            except (BleakError, AwoxError, asyncio.TimeoutError) as err:
                _LOGGER.warning(
                    "%s command 0x%02X attempt %s failed: %s: %s",
                    self.tag, command, attempt, type(err).__name__, err,
                )
                await self._disconnect()
                if attempt == 2:
                    raise

    async def async_request_status(self) -> None:
        await self.async_command(telink.C_REQUEST_STATUS, b"\x10")

    async def async_probe(self, timeout: float = 45, keep: bool = False) -> dict[str, Any]:
        """Used by the config flow: connect, pair, wait briefly for a status.

        With keep=True the connection stays open on success so the config entry can adopt it.
        """
        result: dict[str, Any] = {}
        ok = False
        try:
            async with asyncio.timeout(timeout):
                await self._ensure_connected()
                result["via"] = self.via
                result["notify_mode"] = self.notify_mode
                await self._write_command(telink.C_REQUEST_STATUS, b"\x10")
                for _ in range(30):
                    if self.status is not None:
                        break
                    await asyncio.sleep(0.2)
                result["mesh_id"] = self.mesh_id
                result["status"] = self.status
                ok = True
        finally:
            if not (ok and keep):
                await self._disconnect()
        return result

    # ------------------------------------------------------------- connection

    async def _ensure_connected(self, max_attempts: int = 3) -> None:
        if self.connected:
            return
        async with asyncio.timeout(CONNECT_LOCK_TIMEOUT):
            async with self._lock:
                if self.connected:
                    return
                await self._connect(max_attempts)

    async def _connect(self, max_attempts: int = 3) -> None:
        self._last_attempt_at = time.monotonic()
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.mac, connectable=True)
        if ble_device is None:
            raise AwoxNotFound(
                f"{self.mac} is not visible to any connectable Bluetooth adapter/proxy"
            )
        info = bluetooth.async_last_service_info(self.hass, self.mac, connectable=True)
        self.via = info.source if info else None
        _LOGGER.debug(
            "%s connecting via %s (rssi %s, name %r)",
            self.tag, self.via, info.rssi if info else None, info.name if info else None,
        )

        t0 = time.monotonic()
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            f"AwoX {self.mac}",
            disconnected_callback=self._on_disconnected,
            max_attempts=max_attempts,
            ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                self.hass, self.mac, connectable=True
            )
            or ble_device,
        )
        self._client = client
        _LOGGER.info("%s BLE connected via %s in %.1fs", self.tag, self.via, time.monotonic() - t0)
        self._log_gatt(client)

        try:
            await self._pair(client)
            await self._enable_notifications(client)
        except BaseException:
            await self._disconnect()
            raise

        self._reconnect_attempt = 0
        self._notify_listeners()
        # Ask for current state right away.
        try:
            await self._write_command(telink.C_REQUEST_STATUS, b"\x10")
        except BleakError as err:
            _LOGGER.debug("%s initial status request failed: %s", self.tag, err)

    def _log_gatt(self, client: BleakClient) -> None:
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        svc = client.services.get_service(telink.SERVICE_UUID)
        if svc is None:
            _LOGGER.debug("%s Telink service not found! services: %s", self.tag,
                          [s.uuid for s in client.services])
            return
        for char in svc.characteristics:
            _LOGGER.debug(
                "%s char %s handle=%s props=%s descriptors=%s",
                self.tag, char.uuid[-4:], char.handle, char.properties,
                [(d.uuid[4:8], d.handle) for d in char.descriptors],
            )

    def _char(self, client: BleakClient, uuid: str) -> BleakGATTCharacteristic:
        char = client.services.get_characteristic(uuid)
        if char is None:
            raise AwoxError(f"characteristic {uuid} not found - is this a Telink/AwoX Connect lamp?")
        return char

    async def _pair(self, client: BleakClient) -> None:
        pair_char = self._char(client, telink.PAIR_CHAR_UUID)
        session_random = telink.new_session_random()
        await client.write_gatt_char(
            pair_char, telink.make_pair_packet(self._mesh_name, self._mesh_password, session_random),
            response=True,
        )
        reply = bytes(await client.read_gatt_char(pair_char))
        _LOGGER.debug("%s pair reply: %s", self.tag, reply.hex())
        if reply[:1] == bytes([telink.PAIR_OK]):
            self._session_key = telink.make_session_key(
                self._mesh_name, self._mesh_password, session_random, reply[1:9]
            )
            _LOGGER.info("%s mesh login OK", self.tag)
            return
        if reply[:1] == bytes([telink.PAIR_FAILED]):
            raise AwoxAuthError("lamp rejected mesh name/password")
        raise AwoxError(f"unexpected pair reply {reply.hex()}")

    async def _enable_notifications(self, client: BleakClient) -> None:
        """Telink lamps have no CCCD: notifications are enabled by writing 0x01 to the status char.

        Standard bleak start_notify needs a CCCD on ESPHome proxies (v3 connections),
        so we fall back to registering the notify directly with the proxy.
        """
        status_char = self._char(client, telink.STATUS_CHAR_UUID)
        self.notify_mode = "none"

        try:
            await client.start_notify(status_char, self._on_notify)
            self.notify_mode = "standard"
        except (BleakError, ValueError) as err:
            _LOGGER.debug("%s standard start_notify failed (%s), trying ESPHome raw notify", self.tag, err)
            backend = getattr(client, "_backend", None)
            api_client = getattr(backend, "_client", None)
            address_int = getattr(backend, "_address_as_int", None)
            cancels = getattr(backend, "_notify_cancels", None)
            if api_client is not None and address_int is not None and hasattr(
                api_client, "bluetooth_gatt_start_notify"
            ):
                try:
                    handle = status_char.handle
                    result = await api_client.bluetooth_gatt_start_notify(
                        address_int, handle, lambda _h, data: self._on_notify(status_char, data), 10.0
                    )
                    if isinstance(cancels, dict):
                        cancels[handle] = result  # let bleak-esphome clean up on disconnect
                    self.notify_mode = "esphome-raw"
                except Exception as raw_err:  # noqa: BLE001
                    _LOGGER.warning("%s ESPHome raw notify failed: %s", self.tag, raw_err)
            else:
                _LOGGER.warning(
                    "%s no notification path (backend %s); state updates will be optimistic",
                    self.tag, type(backend).__name__,
                )

        # The actual Telink "enable notifications" switch.
        await client.write_gatt_char(status_char, b"\x01", response=True)
        _LOGGER.info("%s notifications: %s", self.tag, self.notify_mode)

    async def _write_command(self, command: int, data: bytes) -> None:
        client = self._client
        if not client or not self._session_key:
            raise AwoxError("not connected")
        dest = self.mesh_id if self.mesh_id is not None else telink.BROADCAST
        packet = telink.make_command_packet(self._session_key, self.mac, dest, command, data)
        _LOGGER.debug("%s -> cmd 0x%02X dest %s data %s", self.tag, command, dest, data.hex())
        await client.write_gatt_char(self._char(client, telink.COMMAND_CHAR_UUID), packet, response=True)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        self._session_key = None
        if client:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s disconnect error: %s", self.tag, err)

    @callback
    def _on_disconnected(self, _client: BleakClient) -> None:
        _LOGGER.info("%s disconnected", self.tag)
        self._session_key = None
        self._client = None
        self._notify_listeners()
        if self.keep_connected and not self._stopping:
            self._schedule_reconnect()

    def _schedule_reconnect(self, initial: bool = False) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        delay = 0 if initial else RECONNECT_DELAYS[min(self._reconnect_attempt, len(RECONNECT_DELAYS) - 1)]
        self._reconnect_attempt += 1
        self._reconnect_task = self.hass.async_create_background_task(
            self._reconnect_after(delay), f"awox_ble reconnect {self.mac}"
        )

    async def _reconnect_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping or self.connected:
            return
        try:
            await self._ensure_connected(BACKGROUND_CONNECT_ATTEMPTS)
        except AwoxAuthError as err:
            _LOGGER.error("%s %s - not retrying", self.tag, err)
        except Exception as err:  # noqa: BLE001
            # Expected while the lamp is switched off at the wall: log the first failure only.
            level = logging.INFO if self._reconnect_attempt <= 1 else logging.DEBUG
            _LOGGER.log(level, "%s reconnect failed (%s: %s)", self.tag, type(err).__name__, err)
            self._reconnect_task = None
            if not self._stopping:
                self._schedule_reconnect()

    # ---------------------------------------------------------- notifications

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        if not self._session_key:
            return
        packet, ok = telink.decrypt_packet(self._session_key, self.mac, bytes(data))
        _LOGGER.debug("%s <- %s (checksum %s)", self.tag, packet.hex(), "ok" if ok else "MISMATCH")
        status = telink.parse_status(packet)
        if status is None:
            return
        if status.mesh_id == 0:
            return
        if self.mesh_id is None:
            self.mesh_id = status.mesh_id
            _LOGGER.info("%s learned mesh id %s", self.tag, self.mesh_id)
        if status.mesh_id != self.mesh_id:
            _LOGGER.debug("%s status for other mesh id %s ignored", self.tag, status.mesh_id)
            return
        _LOGGER.debug("%s status: %s", self.tag, status)
        self.status = status
        self.last_status_at = time.monotonic()
        self._notify_listeners()

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            cb()
