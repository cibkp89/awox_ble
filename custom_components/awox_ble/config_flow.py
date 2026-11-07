"""Config flow for awox_ble."""

from __future__ import annotations

import asyncio

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_KEEP_CONNECTED,
    CONF_MESH_ID,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    CONF_TEST,
    DOMAIN,
    TELINK_MANUFACTURER_ID,
)
from .device import AwoxAuthError, AwoxLamp, AwoxNotFound

_LOGGER = logging.getLogger(__name__)


def _is_telink(info: BluetoothServiceInfoBleak) -> bool:
    return TELINK_MANUFACTURER_ID in info.manufacturer_data



def _hand_over(hass, lamp: AwoxLamp) -> None:
    """Keep the probe connection open so async_setup_entry can adopt it (no reconnect gap)."""
    pending = hass.data.setdefault(DOMAIN, {}).setdefault("pending", {})
    old = pending.pop(lamp.mac, None)
    if old is not None and old is not lamp:
        hass.async_create_task(old.async_stop())
    pending[lamp.mac] = lamp

    async def _expire() -> None:
        await asyncio.sleep(120)
        if pending.get(lamp.mac) is lamp:
            pending.pop(lamp.mac)
            await lamp.async_stop()

    hass.async_create_background_task(_expire(), f"awox_ble pending {lamp.mac}")


class AwoxBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        if (discovery_info.name or "").lower() == "unpaired":
            # Factory-reset Telink device, not part of any mesh - nothing we can log into.
            return self.async_abort(reason="unpaired")
        self._address = discovery_info.address.upper()
        self._discovered[self._address] = discovery_info
        self.context["title_placeholders"] = {
            "name": f"{discovery_info.name or '?'} ({self._address})"
        }
        return await self.async_step_credentials()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS].upper()
            await self.async_set_unique_id(self._address)
            self._abort_if_unique_id_configured()
            return await self.async_step_credentials()

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if (
                _is_telink(info)
                and info.address.upper() not in current
                and (info.name or "").lower() != "unpaired"
            ):
                self._discovered[info.address.upper()] = info

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        options = {
            addr: f"{info.name or '?'} ({addr}, RSSI {info.rssi}, via {info.source})"
            for addr, info in self._discovered.items()
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(options)}),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._address
        info = self._discovered.get(self._address)
        placeholders = {
            "address": self._address,
            "via": info.source if info else "?",
            "rssi": str(info.rssi) if info else "?",
        }

        if user_input is not None:
            data = {
                CONF_ADDRESS: self._address,
                CONF_MESH_NAME: user_input[CONF_MESH_NAME],
                CONF_MESH_PASSWORD: user_input[CONF_MESH_PASSWORD],
                CONF_KEEP_CONNECTED: user_input[CONF_KEEP_CONNECTED],
            }
            if not 1 <= len(data[CONF_MESH_PASSWORD].encode()) <= 16:
                errors[CONF_MESH_PASSWORD] = "too_long"
            elif user_input[CONF_TEST]:
                lamp = AwoxLamp(
                    self.hass, self._address, data[CONF_MESH_NAME], data[CONF_MESH_PASSWORD],
                    keep_connected=False,
                )
                try:
                    result = await lamp.async_probe(keep=True)
                except AwoxNotFound:
                    errors["base"] = "not_found"
                except AwoxAuthError:
                    errors["base"] = "invalid_auth"
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Probe of %s failed: %s: %s", self._address, type(err).__name__, err)
                    errors["base"] = "cannot_connect"
                else:
                    _LOGGER.info("Probe of %s: %s", self._address, result)
                    if result.get("mesh_id") is not None:
                        data[CONF_MESH_ID] = result["mesh_id"]
                    _hand_over(self.hass, lamp)

            if not errors:
                return self.async_create_entry(
                    title=f"AwoX {info.name if info and info.name else self._address}",
                    data=data,
                )

        len16 = vol.All(str, vol.Length(min=1, max=16))  # mesh name
        schema = vol.Schema(
            {
                vol.Required(CONF_MESH_NAME): len16,
                vol.Required(CONF_MESH_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_KEEP_CONNECTED, default=True): bool,
                vol.Required(CONF_TEST, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )
