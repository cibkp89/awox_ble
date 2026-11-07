"""Light entity for awox_ble."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AwoxConfigEntry, telink
from .const import DOMAIN
from .device import AwoxLamp

MIN_KELVIN = 2702  # 370 mired  -> temperature 0x7F
MAX_KELVIN = 6535  # 153 mired  -> temperature 0x00
MIN_MIRED, MAX_MIRED = 153, 370


def _scale(value: float, in_min: float, in_max: float, out_min: int, out_max: int) -> int:
    value = max(in_min, min(in_max, value))
    return round(out_min + (value - in_min) * (out_max - out_min) / (in_max - in_min))


def _kelvin_to_temp(kelvin: int) -> int:
    return _scale(1_000_000 / kelvin, MIN_MIRED, MAX_MIRED, telink.TEMPERATURE_MIN, telink.TEMPERATURE_MAX)


def _temp_to_kelvin(temp: int) -> int:
    mired = _scale(temp, telink.TEMPERATURE_MIN, telink.TEMPERATURE_MAX, MIN_MIRED, MAX_MIRED)
    return round(1_000_000 / mired)


async def async_setup_entry(
    hass: HomeAssistant, entry: AwoxConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([AwoxLight(entry.runtime_data, entry.entry_id, entry.title)])


class AwoxLight(LightEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.RGB}
    _attr_min_color_temp_kelvin = MIN_KELVIN
    _attr_max_color_temp_kelvin = MAX_KELVIN

    def __init__(self, lamp: AwoxLamp, entry_id: str, title: str) -> None:
        self._lamp = lamp
        self._attr_unique_id = f"{lamp.mac}_light"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, lamp.mac)},
            connections={(CONNECTION_BLUETOOTH, lamp.mac)},
            name=title,
            manufacturer="AwoX / EGLO",
            model="Connect (Telink mesh)",
        )
        self._attr_is_on = None
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_brightness = None
        self._attr_color_temp_kelvin = None
        self._attr_rgb_color = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._lamp.add_listener(self._handle_update))
        if self._lamp.status is not None:
            # e.g. connection adopted from the config flow: state is already known
            self._handle_update()

    @property
    def available(self) -> bool:
        # In keep-connected mode we only claim availability while the session is up.
        return self._lamp.connected or not self._lamp.keep_connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "connected": self._lamp.connected,
            "via": self._lamp.via,
            "notify_mode": self._lamp.notify_mode,
            "mesh_id": self._lamp.mesh_id,
        }

    def _handle_update(self) -> None:
        st = self._lamp.status
        if st is not None:
            self._attr_is_on = st.state
            if st.color_mode:
                self._attr_color_mode = ColorMode.RGB
                self._attr_rgb_color = (st.red, st.green, st.blue)
                self._attr_brightness = _scale(
                    st.color_brightness, telink.COLOR_BRIGHTNESS_MIN, telink.COLOR_BRIGHTNESS_MAX, 1, 255
                )
            else:
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_color_temp_kelvin = _temp_to_kelvin(st.temperature)
                self._attr_brightness = _scale(
                    st.white_brightness, telink.WHITE_BRIGHTNESS_MIN, telink.WHITE_BRIGHTNESS_MAX, 1, 255
                )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        lamp = self._lamp
        if not self._attr_is_on:
            await lamp.async_command(telink.C_POWER, b"\x01\x00\x00")
            self._attr_is_on = True

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            await lamp.async_command(telink.C_COLOR, bytes([0x04, r, g, b]))
            self._attr_color_mode = ColorMode.RGB
            self._attr_rgb_color = (r, g, b)
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            await lamp.async_command(telink.C_WHITE_TEMPERATURE, bytes([_kelvin_to_temp(kelvin)]))
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = kelvin

        if ATTR_BRIGHTNESS in kwargs:
            bri = kwargs[ATTR_BRIGHTNESS]
            if self._attr_color_mode == ColorMode.RGB:
                value = _scale(bri, 1, 255, telink.COLOR_BRIGHTNESS_MIN, telink.COLOR_BRIGHTNESS_MAX)
                await lamp.async_command(telink.C_COLOR_BRIGHTNESS, bytes([value]))
            else:
                value = _scale(bri, 1, 255, telink.WHITE_BRIGHTNESS_MIN, telink.WHITE_BRIGHTNESS_MAX)
                await lamp.async_command(telink.C_WHITE_BRIGHTNESS, bytes([value]))
            self._attr_brightness = bri

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._lamp.async_command(telink.C_POWER, b"\x00\x00\x00")
        self._attr_is_on = False
        self.async_write_ha_state()
