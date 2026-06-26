"""Thin MQTT publisher around paho-mqtt.

Publishes one JSON document per polled command to:

    <topic_prefix>/<command_name>

plus a small availability topic at <topic_prefix>/status (online/offline,
retained) so consumers can tell whether the poller is alive.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        topic_prefix: str,
        username: str | None = None,
        password: str | None = None,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix.rstrip("/")
        self.qos = qos
        self.retain = retain

        # paho-mqtt 2.x requires an explicit callback API version; fall back
        # gracefully on 1.x where that argument does not exist.
        try:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=client_id
            )
        except (AttributeError, TypeError):
            self._client = mqtt.Client(client_id=client_id)
        if username:
            self._client.username_pw_set(username, password or "")
        # Last will so an unclean exit still flips availability to offline.
        self._client.will_set(
            self._topic("status"), payload="offline", qos=1, retain=True
        )

    def _topic(self, leaf: str) -> str:
        return f"{self.topic_prefix}/{leaf}"

    def connect(self) -> None:
        self._client.connect(self.host, self.port)
        self._client.loop_start()
        self._client.publish(
            self._topic("status"), payload="online", qos=1, retain=True
        )
        log.info("MQTT connected to %s:%d", self.host, self.port)

    def disconnect(self) -> None:
        try:
            self._client.publish(
                self._topic("status"), payload="offline", qos=1, retain=True
            )
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as e:
            log.debug("Error during MQTT disconnect: %s", e)

    def publish_command(self, name: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, default=str, separators=(",", ":"))
        topic = self._topic(name)
        self._client.publish(topic, payload, qos=self.qos, retain=self.retain)
        log.debug("Published %s (%d bytes)", topic, len(payload))
