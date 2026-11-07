# AwoX Connect (BLE Proxy) for Home Assistant

Control AwoX / EGLO Connect lamps (Telink BLE mesh) from Home Assistant through
**Home Assistant's own Bluetooth stack**, including **ESPHome Bluetooth proxies**.
No AwoX cloud, no extra ESP32 hub, no MQTT.

> **Experimental.** One lamp per config entry, tested only over ESPHome Bluetooth
> proxies. See [Limitations](#limitations).

## Why

[fsaris/home-assistant-awox](https://github.com/fsaris/home-assistant-awox) talks to the
local Bluetooth adapter directly (pygatt/gatttool) and locks it for everything else.
Moving it to HA's Bluetooth stack was blocked
([#110](https://github.com/fsaris/home-assistant-awox/issues/110)) because the lamps'
status characteristic has no CCCD (`0x2902`), so a standard `start_notify()` fails.

Telink lamps enable notifications by writing `0x01` to the status characteristic itself.
Over an ESPHome proxy this works as long as the proxy-side subscription is kept. This
integration does exactly that, so the lamp shares the proxy with all your other
Bluetooth devices.

## How it works

1. HA picks the proxy with the best signal and a free connection slot.
2. Mesh login on the pair characteristic (`…1914`) and session key derivation.
3. `start_notify()` is tried first. If it fails with the missing-CCCD error, the
   integration registers the notification directly with the ESPHome proxy.
4. `0x01` is written to the status characteristic (`…1911`), which is the actual
   Telink switch for notifications.
5. Commands are encrypted and written to `…1912`. The lamp confirms every change with a
   status notification, typically within 0.1–0.5 s.

Step 3 uses private attributes of `bleak-esphome`. A proposal for an official option is
open upstream; once it lands, the fallback reduces to a single call.

## Requirements

- Home Assistant 2026.10 or newer
- An ESPHome Bluetooth proxy with `bluetooth_proxy: active: true` within good range of
  the lamp. **-75 dBm or better** is a good rule of thumb: at -72 dBm connections are
  stable, around -75 dBm the first attempt often fails, and at -82 dBm or worse
  connections usually fail even though advertisements still arrive.
- The lamp's **mesh name and mesh password**, for example from the YAML of
  [EspHome-AwoX-BLE-mesh-hub](https://github.com/fsaris/EspHome-AwoX-BLE-mesh-hub) or
  from an existing fsaris setup.

## Installation

**HACS:** add `https://github.com/cibkp89/awox_ble` as a custom repository (type
*Integration*), install **AwoX Connect (BLE Proxy)** and restart Home Assistant.

**Manual:** copy `custom_components/awox_ble` to `/config/custom_components/awox_ble/`
(`manifest.json` must be directly inside that folder) and restart Home Assistant.

## Setup

1. Switch off any other AwoX hub and close the AwoX / EGLO app. The lamp accepts only
   one BLE connection.
2. Go to **Settings → Devices & services**. The lamp shows up under **Discovered**
   (Bluetooth manufacturer ID 352 / `0x0160`). Factory-reset devices advertising as
   `unpaired` are ignored.
3. Enter mesh name and password. Leave **Keep connected** and **Test connection now**
   checked. The test connection is adopted directly by the new entry.

The lamp's mesh address is not in its advertisement; it is learned from the first status
message after login and stored.

## Wall switches

If the lamp is switched off at the wall it becomes *unavailable*. With *Keep connected*
the integration keeps a connection request pending, and the lamp is usable again roughly
15 seconds after power returns. This keeps one of the proxy's connection slots busy, even
while the lamp has no power.

## Diagnostics

The light entity exposes:

| Attribute | Expected | Meaning |
| --- | --- | --- |
| `connected` | `true` | BLE session is up |
| `via` | proxy MAC | which proxy HA uses |
| `notify_mode` | `esphome-raw` | notification fallback active; `none` means no state feedback |
| `mesh_id` | number | learned mesh address |

**Check `notify_mode` after every Home Assistant update.** If `bleak-esphome` renames
its internals, control keeps working but state feedback stops, `notify_mode` turns to
`none` and a warning is logged.

Debug logging:

```yaml
logger:
  logs:
    custom_components.awox_ble: debug
    bleak_esphome: debug
    bleak_retry_connector: debug
```

> When sharing logs, replace the lamp's advertised name (shown as `name '…'`): for AwoX
> lamps it is usually the mesh name, i.e. half of your credentials.

Typical messages:

- `ESP_GATT_ERROR` / status 133 / `0x3E` while connecting: signal too weak or the lamp
  is briefly not connectable. Move the proxy closer.
- `does not have a characteristic client config descriptor` (debug): expected; it must
  be followed by `notifications: esphome-raw`.
- `lamp rejected mesh name/password`: wrong credentials.

## Limitations

- One lamp per entry. No mesh relaying to other lamps, no groups, no plugs, no effects
  (candle, color cycle).
- Only tested over ESPHome proxies. Whether notifications work over a local BlueZ
  adapter is untested.
- Tested with: EGLO Savatarila-C 99312, ESPHome 2026.10.2, bleak 3.0.2,
  bleak-esphome 4.0.0, habluetooth 7.1.2, Home Assistant 2026.10 and 2026.11.

## Credits

The protocol implementation follows
[python-awox-mesh-light](https://github.com/Leiaz/python-awox-mesh-light) (Leiaz),
[home-assistant-awox](https://github.com/fsaris/home-assistant-awox) and
[EspHome-AwoX-BLE-mesh-hub](https://github.com/fsaris/EspHome-AwoX-BLE-mesh-hub)
(Frans Saris). Thanks to both for years of reverse engineering.

The code was written with AI assistance (Claude) and tested on a real lamp.

AwoX and EGLO are trademarks of their respective owners. This project is not affiliated
with or endorsed by them.

## License

[MIT](LICENSE)
