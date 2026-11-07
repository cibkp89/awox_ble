"""AwoX / EGLO Connect (Telink mesh) lamps via Home Assistant Bluetooth / ESPHome proxies."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_KEEP_CONNECTED, CONF_MESH_ID, CONF_MESH_NAME, CONF_MESH_PASSWORD
from .device import AwoxLamp

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT]

type AwoxConfigEntry = ConfigEntry[AwoxLamp]


async def async_setup_entry(hass: HomeAssistant, entry: AwoxConfigEntry) -> bool:
    keep = entry.data.get(CONF_KEEP_CONNECTED, True)
    pending = hass.data.get(DOMAIN, {}).get("pending", {})
    lamp = pending.pop(entry.data[CONF_ADDRESS].upper(), None)
    if lamp is not None:
        _LOGGER.debug("%s adopting connection from config flow (connected=%s)", lamp.tag, lamp.connected)
        lamp.keep_connected = keep
        if not keep:
            await lamp.async_stop()
    else:
        lamp = AwoxLamp(
            hass,
            entry.data[CONF_ADDRESS],
            entry.data[CONF_MESH_NAME],
            entry.data[CONF_MESH_PASSWORD],
            mesh_id=entry.data.get(CONF_MESH_ID),
            keep_connected=keep,
        )
    entry.runtime_data = lamp

    def _persist_mesh_id() -> None:
        if lamp.mesh_id is not None and entry.data.get(CONF_MESH_ID) != lamp.mesh_id:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_MESH_ID: lamp.mesh_id}
            )

    entry.async_on_unload(lamp.add_listener(_persist_mesh_id))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await lamp.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AwoxConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
