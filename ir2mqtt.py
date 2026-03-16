#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "evdev>=1.7.0",
#     "aiomqtt>=2.3.0",
# ]
# ///
"""ir2mqtt - Forward Linux IR input events to Home Assistant via MQTT device triggers."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import aiomqtt
from evdev import InputDevice, ecodes, list_devices

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ir2mqtt")

# ---------------------------------------------------------------------------
# Configuration — adapt these or load from env / config file
# ---------------------------------------------------------------------------

MQTT_BROKER = os.environ.get("MQTT_BROKER", "192.168.0.50")
MQTT_PORT = 1883
MQTT_USER: str | None = os.environ.get("MQTT_USER", "mqtt")
MQTT_PASSWORD: str | None = os.environ.get("MQTT_PASSWORD", "changeme")

DEVICE_ID = os.environ.get("DEVICE_ID", "odroid_n2_ir_remote")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "ODROID-N2+ IR Remote")
DEVICE_MANUFACTURER = os.environ.get("DEVICE_MANUFACTURER", "Hardkernel")
DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "ODROID-N2+")

IR_DEVICE_NAME_MATCH = os.environ.get("IR_DEVICE_NAME_MATCH", "meson-ir")

DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = f"ir2mqtt/{DEVICE_ID}"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/status"

LONG_PRESS_THRESHOLD = 0.9
DOUBLE_PRESS_WINDOW = 0.15

KEY_MAP: dict[int, str] = {
	ecodes.KEY_HOME: "home",
	ecodes.KEY_UP: "up",
	ecodes.KEY_LEFT: "left",
	ecodes.KEY_RIGHT: "right",
	ecodes.KEY_DOWN: "down",
	ecodes.KEY_MUTE: "mute",
	ecodes.KEY_VOLUMEDOWN: "volume_down",
	ecodes.KEY_VOLUMEUP: "volume_up",
	ecodes.KEY_POWER: "power",
	ecodes.KEY_MENU: "menu",
	ecodes.KEY_BACK: "back",
	ecodes.KEY_OK: "ok"
}

ACTION_TYPES = ["button_short_press", "button_long_press", "button_double_press"]

@dataclass
class ButtonState:
	"""Track state for multi-action detection on a single key."""

	key_down_time: float = 0.0
	is_held: bool = False
	long_press_fired: bool = False
	press_count: int = 0
	double_press_task: asyncio.Task[None] | None = None


@dataclass
class IR2MQTT:
	"""Main application state."""

	client: aiomqtt.Client | None = None
	device: InputDevice | None = None
	button_states: dict[int, ButtonState] = field(default_factory=dict)
	_running: bool = True

	def _device_payload(self) -> dict[str, Any]:
		"""Return the HA device descriptor (shared by all discovery messages)."""
		return {
			"identifiers": [DEVICE_ID],
			"name": DEVICE_NAME,
			"manufacturer": DEVICE_MANUFACTURER,
			"model": DEVICE_MODEL,
		}

	def _discovery_topic(self, button_name: str, action_type: str) -> str:
		object_id = f"{DEVICE_ID}_{button_name}_{action_type}"
		return f"{DISCOVERY_PREFIX}/device_automation/{object_id}/config"

	def _trigger_topic(self, button_name: str) -> str:
		return f"{BASE_TOPIC}/triggers/{button_name}"

	async def publish_discovery(self) -> None:
		"""Publish MQTT discovery messages for all buttons and action types."""
		assert self.client is not None
		for _keycode, button_name in KEY_MAP.items():
			trigger_topic = self._trigger_topic(button_name)
			for action_type in ACTION_TYPES:
				config = {
					"automation_type": "trigger",
					"type": action_type,
					"subtype": button_name,
					"topic": trigger_topic,
					"payload": action_type,
					"device": self._device_payload(),
					"o": {"name": "ir2mqtt", "sw": "1.0.0"},
				}
				await self.client.publish(
					self._discovery_topic(button_name, action_type),
					json.dumps(config),
					qos=1,
					retain=True,
				)
		await self.client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
		log.info("Discovery published for %d buttons", len(KEY_MAP))

	async def publish_trigger(self, button_name: str, action_type: str) -> None:
		"""Publish a trigger event for a button action."""
		if self.client is None:
			return
		log.info("Trigger: %s → %s", button_name, action_type)
		await self.client.publish(self._trigger_topic(button_name), action_type, qos=1)

	def find_ir_device(self) -> InputDevice | None:
		"""Find the IR input device by name substring match."""
		for path in list_devices():
			dev = InputDevice(path)
			if IR_DEVICE_NAME_MATCH.lower() in dev.name.lower():
				log.info("Found IR device: %s (%s)", dev.name, dev.path)
				return dev
			dev.close()
		return None

	def _get_state(self, keycode: int) -> ButtonState:
		if keycode not in self.button_states:
			self.button_states[keycode] = ButtonState()
		return self.button_states[keycode]

	async def _handle_double_press_timeout(self, keycode: int) -> None:
		"""Wait for double-press window, then fire single press if no second tap."""
		button_name = KEY_MAP.get(keycode)
		if button_name is None:
			return
		state = self._get_state(keycode)
		await asyncio.sleep(DOUBLE_PRESS_WINDOW)
		if state.press_count == 1:
			await self.publish_trigger(button_name, "button_short_press")
		elif state.press_count >= 2:
			await self.publish_trigger(button_name, "button_double_press")
		state.press_count = 0
		state.double_press_task = None

	async def handle_key_event(self, keycode: int, value: int) -> None:
		"""Process an evdev key event. value: 0=up, 1=down, 2=hold."""
		button_name = KEY_MAP.get(keycode)
		if button_name is None:
			return
		state = self._get_state(keycode)
		if value == 1:  # key_down
			state.key_down_time = time.monotonic()
			state.is_held = True
			state.long_press_fired = False
		elif value == 2:  # key_hold
			if (
					state.is_held
					and not state.long_press_fired
					and (time.monotonic() - state.key_down_time) >= LONG_PRESS_THRESHOLD
			):
				state.long_press_fired = True
				if state.double_press_task is not None:
					state.double_press_task.cancel()
					state.double_press_task = None
					state.press_count = 0
				await self.publish_trigger(button_name, "button_long_press")
		elif value == 0:  # key_up
			state.is_held = False
			if not state.long_press_fired:
				state.press_count += 1
				if state.double_press_task is None:
					state.double_press_task = asyncio.create_task(self._handle_double_press_timeout(keycode))
			state.long_press_fired = False

	async def monitor_input(self) -> None:
		"""Read events from the IR input device forever."""
		assert self.device is not None
		log.info("Monitoring input from: %s", self.device.name)
		async for event in self.device.async_read_loop():
			if not self._running:
				break
			if event.type == ecodes.EV_KEY:
				await self.handle_key_event(event.code, event.value)

	async def run(self) -> None:
		"""Main entry point."""
		self.device = self.find_ir_device()
		if self.device is None:
			log.error("No IR input device matching '%s'. Available:", IR_DEVICE_NAME_MATCH)
			for path in list_devices():
				dev = InputDevice(path)
				log.error("  %s: %s", dev.path, dev.name)
				dev.close()
			sys.exit(1)

		will = aiomqtt.Will(topic=AVAILABILITY_TOPIC, payload="offline", qos=1, retain=True)
		async with aiomqtt.Client(
				hostname=MQTT_BROKER,
				port=MQTT_PORT,
				username=MQTT_USER,
				password=MQTT_PASSWORD,
				will=will,
		) as client:
			self.client = client
			await self.publish_discovery()
			await self.monitor_input()

	def stop(self) -> None:
		"""Signal the daemon to stop."""
		self._running = False
		if self.device is not None:
			self.device.close()


def main() -> None:
	app = IR2MQTT()
	loop = asyncio.new_event_loop()

	def _signal_handler() -> None:
		log.info("Shutting down...")
		app.stop()
		exit(1)

	for sig in (signal.SIGTERM, signal.SIGINT):
		loop.add_signal_handler(sig, _signal_handler)

	try:
		loop.run_until_complete(app.run())
	except (KeyboardInterrupt, OSError):
		pass
	finally:
		loop.close()


if __name__ == "__main__":
	main()
