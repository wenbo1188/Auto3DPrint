"""Bambu Lab printer control over the local MQTT channel (LAN + developer mode).

The printer runs an MQTT broker on port 8883 (TLS, self-signed certificate).
Login is user ``bblp`` with the LAN access code shown on the printer screen.
Status arrives on ``device/<serial>/report``; commands go to
``device/<serial>/request``. P1/P2 series printers send partial reports, so
we merge every report into one state dict and ask for a full ``pushall``
right after connecting.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import paho.mqtt.client as mqtt

# gcode_state values reported by the printer
STATE_IDLE = "IDLE"
STATE_PREPARE = "PREPARE"
STATE_RUNNING = "RUNNING"
STATE_PAUSE = "PAUSE"
STATE_FINISH = "FINISH"
STATE_FAILED = "FAILED"

STATE_LABELS = {
    STATE_IDLE: "空闲",
    STATE_PREPARE: "准备中",
    STATE_RUNNING: "打印中",
    STATE_PAUSE: "已暂停",
    STATE_FINISH: "已完成",
    STATE_FAILED: "失败",
}


def deep_merge(base: dict, update: dict) -> dict:
    """Merge ``update`` into ``base`` in place (nested dicts merged, rest replaced)."""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@dataclass
class PrinterStatus:
    """A readable snapshot of the fields we care about."""

    gcode_state: str = ""
    percent: int | None = None
    remaining_min: int | None = None
    layer: int | None = None
    total_layers: int | None = None
    nozzle_temp: float | None = None
    nozzle_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    file: str = ""
    error_code: int | None = None
    raw: dict = field(default_factory=dict)

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.gcode_state, self.gcode_state or "未知")

    @classmethod
    def from_print(cls, p: dict) -> "PrinterStatus":
        def num(key, cast=float):
            v = p.get(key)
            try:
                return cast(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            gcode_state=str(p.get("gcode_state", "")),
            percent=num("mc_percent", int),
            remaining_min=num("mc_remaining_time", int),
            layer=num("layer_num", int),
            total_layers=num("total_layer_num", int),
            nozzle_temp=num("nozzle_temper"),
            nozzle_target=num("nozzle_target_temper"),
            bed_temp=num("bed_temper"),
            bed_target=num("bed_target_temper"),
            file=str(p.get("subtask_name") or p.get("gcode_file") or ""),
            error_code=num("print_error", int),
            raw=p,
        )


class BambuPrinter:
    """Thin wrapper around the printer's MQTT interface.

    Callbacks run on the MQTT network thread; GUI code must hand them over to
    its own thread.
    """

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        port: int = 8883,
        use_tls: bool = True,
        username: str = "bblp",
    ):
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.port = port
        self.use_tls = use_tls
        self.username = username

        self.state: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._connected = threading.Event()
        self._got_report = threading.Event()

        self.on_status: Callable[[PrinterStatus], None] | None = None
        self.on_connection: Callable[[bool, str], None] | None = None

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"p2s-autoprint-{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(username, access_code)
        if use_tls:
            ctx = ssl.create_default_context()
            # The printer uses a self-signed certificate.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.client.tls_set_context(ctx)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---- topics -------------------------------------------------------
    @property
    def report_topic(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def request_topic(self) -> str:
        return f"device/{self.serial}/request"

    # ---- connection ---------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        """Connect and wait until the broker accepts us. Raises on failure."""
        self._connected.clear()
        self._connect_error = ""
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            self.client.loop_stop()
            reason = self._connect_error or "连接超时，请检查 IP、局域网模式和网络"
            raise ConnectionError(reason)

    def disconnect(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def wait_for_report(self, timeout: float = 10.0) -> bool:
        return self._got_report.wait(timeout)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self._connect_error = f"打印机拒绝连接：{reason_code}（检查序列号和访问码）"
            if self.on_connection:
                self.on_connection(False, self._connect_error)
            return
        client.subscribe(self.report_topic)
        self._connected.set()
        self.request_full_status()
        if self.on_connection:
            self.on_connection(True, "已连接")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected.clear()
        if self.on_connection:
            self.on_connection(False, f"连接断开：{reason_code}")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            return
        with self._lock:
            deep_merge(self.state, data)
            snapshot = PrinterStatus.from_print(dict(self.state.get("print", {})))
        if "print" in data:
            self._got_report.set()
            if self.on_status:
                self.on_status(snapshot)

    # ---- commands -----------------------------------------------------
    def _next_seq(self) -> str:
        self._seq += 1
        return str(self._seq)

    def publish(self, payload: dict) -> None:
        if not self.is_connected:
            raise ConnectionError("未连接打印机")
        self.client.publish(self.request_topic, json.dumps(payload), qos=1)

    def request_full_status(self) -> None:
        self.client.publish(
            self.request_topic,
            json.dumps(
                {
                    "pushing": {
                        "sequence_id": self._next_seq(),
                        "command": "pushall",
                        "version": 1,
                        "push_target": 1,
                    }
                }
            ),
        )

    def status(self) -> PrinterStatus:
        with self._lock:
            return PrinterStatus.from_print(dict(self.state.get("print", {})))

    def start_print(
        self,
        filename: str,
        plate: int = 1,
        url_prefix: str = "ftp://",
        use_ams: bool = False,
        bed_leveling: bool = True,
        timelapse: bool = False,
    ) -> None:
        """Start a .3mf that is already on the printer's storage.

        ``filename`` is the path on the printer, e.g. ``job.3mf``.
        ``url_prefix`` is ``ftp://`` (works on P1 series) or
        ``file:///sdcard/`` (X1 series); which one P2S expects still needs
        to be confirmed on the real machine, so it is configurable.
        """
        name = filename.lstrip("/")
        self.publish(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "project_file",
                    "param": f"Metadata/plate_{plate}.gcode",
                    "project_id": "0",
                    "profile_id": "0",
                    "task_id": "0",
                    "subtask_id": "0",
                    "subtask_name": name.rsplit("/", 1)[-1].removesuffix(".3mf"),
                    "url": f"{url_prefix}{name}",
                    "md5": "",
                    "timelapse": timelapse,
                    "bed_type": "auto",
                    "bed_levelling": bed_leveling,
                    "bed_leveling": bed_leveling,
                    "flow_cali": False,
                    "vibration_cali": False,
                    "layer_inspect": False,
                    "use_ams": use_ams,
                    "ams_mapping": [],
                }
            }
        )

    def _simple(self, command: str) -> None:
        self.publish({"print": {"sequence_id": self._next_seq(), "command": command, "param": ""}})

    def pause(self) -> None:
        self._simple("pause")

    def resume(self) -> None:
        self._simple("resume")

    def stop(self) -> None:
        self._simple("stop")

    def send_gcode(self, gcode: str) -> None:
        """Run raw G-code lines (used later for the plate-clearing tests)."""
        self.publish(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "gcode_line",
                    "param": gcode if gcode.endswith("\n") else gcode + "\n",
                }
            }
        )
