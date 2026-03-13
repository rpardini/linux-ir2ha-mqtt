# ir2mqtt

Forward Linux IR remote control events to [Home Assistant](https://www.home-assistant.io/) via MQTT, using [HA's MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) mechanism.

The IR remote appears as a **Device** in Home Assistant with device triggers for each button — exactly like a ZigBee dimmer remote. Short press, long press, and double press are detected locally and mapped to standard HA trigger types, ready for use in automations.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Linux machine with IR receiver             │
│                                             │
│  meson-ir driver → /dev/input/eventX        │
│       ↓                                     │
│  ir2mqtt.py (this script)                   │
│    • reads evdev key events                 │
│    • detects short/long/double press        │
│    • publishes MQTT discovery + triggers    │
│       ↓                                     │
│  MQTT broker                                │
└────────────────┬────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────┐
│  Home Assistant                             │
│    • auto-discovers device + triggers       │
│    • automations fire on button presses     │
└─────────────────────────────────────────────┘
```

## Requirements

- Linux with an IR receiver exposed via evdev (e.g., Amlogic `meson-ir`)
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) (handles dependencies automatically)
- An MQTT broker reachable by both this machine and Home Assistant
- Home Assistant with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) configured

## Quick start

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone
git clone https://github.com/rpardini/linux-ir2ha-mqtt.git
cd linux-ir2ha-mqtt

# Edit configuration at the top of ir2mqtt.py:
#   MQTT_BROKER, MQTT_USER, MQTT_PASSWORD, KEY_MAP, etc.

# Run directly (uv handles the virtualenv and dependencies)
sudo uv run --script ir2mqtt.py
```

### Discover your remote's keys

Before configuring `KEY_MAP`, find out what keycodes your remote sends:

```bash
# List input devices
sudo evtest

# Or with ir-keytable
ir-keytable -t
# Press buttons on the remote and note the keycodes
```

Then map the relevant `ecodes.KEY_*` constants to button names in `KEY_MAP`.

## Installation as a systemd service

```bash
# Deploy the script
sudo mkdir -p /opt/ir2mqtt
sudo cp ir2mqtt.py /opt/ir2mqtt/
sudo chmod +x /opt/ir2mqtt/ir2mqtt.py

# Pre-warm the uv cache
sudo UV_CACHE_DIR=/opt/ir2mqtt/.cache/uv uv run --script /opt/ir2mqtt/ir2mqtt.py --help || true

# Install and enable the service
sudo cp ir2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ir2mqtt

# Check logs
journalctl -u ir2mqtt -f
```

## What it looks like in Home Assistant

Once running, HA auto-discovers a device (default name: **ODROID-N2+ IR Remote**) with device triggers:

| Trigger type           | Subtype     | Fires when                |
| ---------------------- | ----------- | ------------------------- |
| `button_short_press`   | `volume_up` | Button tapped once        |
| `button_double_press`  | `volume_up` | Button tapped twice       |
| `button_long_press`    | `volume_up` | Button held ≥ 0.6 seconds |
| ...                    | ...         | ...                       |

Use them in automations exactly like any ZigBee remote:

```yaml
automation:
  - alias: "IR Remote - Volume Up"
    triggers:
      - trigger: device
        domain: mqtt
        device_id: <auto-filled by HA UI>
        type: button_short_press
        subtype: volume_up
    actions:
      - action: media_player.volume_up
        target:
          entity_id: media_player.living_room
```

## Configuration reference

All configuration is at the top of `ir2mqtt.py`:

| Variable               | Default                | Description                                        |
| ---------------------- | ---------------------- | -------------------------------------------------- |
| `MQTT_BROKER`          | `homeassistant.local`  | MQTT broker hostname or IP                         |
| `MQTT_PORT`            | `1883`                 | MQTT broker port                                   |
| `MQTT_USER`            | `None`                 | MQTT username (optional)                           |
| `MQTT_PASSWORD`        | `None`                 | MQTT password (optional)                           |
| `DEVICE_ID`            | `odroid_n2_ir_remote`  | Unique device identifier in HA                     |
| `DEVICE_NAME`          | `ODROID-N2+ IR Remote` | Display name in HA                                 |
| `IR_DEVICE_NAME_MATCH` | `meson-ir`             | Substring to match the evdev input device name     |
| `LONG_PRESS_THRESHOLD` | `0.6`                  | Seconds of hold before firing long press           |
| `DOUBLE_PRESS_WINDOW`  | `0.35`                 | Seconds to wait for a second tap (double press)    |
| `KEY_MAP`              | *(see source)*         | `ecodes.KEY_*` → button name mapping               |

## Disabling the IR power key shutdown

If your remote has a power button that shuts down the machine, disable it in systemd-logind **before** running ir2mqtt:

```ini
# /etc/systemd/logind.conf
[Login]
HandlePowerKey=ignore
HandlePowerKeyLongPress=ignore
```

```bash
sudo systemctl restart systemd-logind
```

The power button will then simply become another HA trigger button.

## How it works

1. **Startup**: publishes MQTT Discovery configs (retained) for every button × action type. HA auto-creates the device and triggers.
2. **Key press**: evdev emits `key_down` → `key_hold` (repeat) → `key_up`. The daemon tracks timing locally to classify as short press, long press, or double press.
3. **Trigger**: publishes the action payload to the button's MQTT topic. HA matches topic + payload and fires the device trigger.
4. **Availability**: uses MQTT Last Will and Testament — if the daemon crashes, HA sees the device go offline.
5. **HA restart**: retained discovery messages are re-delivered by the broker, so the device is recreated without restarting ir2mqtt.

## Future ideas

- [ ] Configuration via environment variables or a TOML config file
- [ ] Command buttons in HA (reboot, shutdown) via MQTT command topics
- [ ] Auto-discovery of keys from the evdev device capabilities
- [ ] Packaging as a proper Debian `.deb`
- [ ] Multiple IR receiver support

## License

MIT — see [LICENSE](LICENSE).