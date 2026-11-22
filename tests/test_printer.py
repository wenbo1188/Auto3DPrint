"""Tests against a local MQTT broker and FTP server standing in for the printer."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from autoprint.config import Settings
from autoprint.ftps import list_files, upload_file
from autoprint.printer import BambuPrinter, deep_merge
from autoprint.threemf import sliced_plates

SERIAL = "01P00TEST0000001"
CODE = "12345678"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- MQTT broker ---------------------------------------------------------
def _run_broker(config: dict, ready: threading.Event, stop: threading.Event):
    from amqtt.broker import Broker

    async def main():
        broker = Broker(config)
        await broker.start()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.05)
        await broker.shutdown()

    asyncio.run(main())


@pytest.fixture(params=["plain", "tls"])
def broker(request, tmp_path):
    port = free_port()
    listener = {"type": "tcp", "bind": f"127.0.0.1:{port}"}
    if request.param == "tls":
        cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=printer", "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
        listener.update({"ssl": True, "certfile": str(cert), "keyfile": str(key)})
    config = {
        "listeners": {"default": listener},
        "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
    }
    ready, stop = threading.Event(), threading.Event()
    t = threading.Thread(target=_run_broker, args=(config, ready, stop), daemon=True)
    t.start()
    assert ready.wait(10)
    yield port, request.param == "tls"
    stop.set()
    t.join(5)


class FakePrinter:
    """Answers pushall with a full report and records commands."""

    def __init__(self, port: int):
        self.commands: list[dict] = []
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.c.on_message = self._on_message
        self.c.connect("127.0.0.1", port)
        self.c.subscribe(f"device/{SERIAL}/request")
        self.c.loop_start()
        time.sleep(0.3)

    def report(self, data: dict):
        self.c.publish(f"device/{SERIAL}/report", json.dumps({"print": data}))

    def _on_message(self, client, userdata, msg):
        data = json.loads(msg.payload)
        self.commands.append(data)
        if data.get("pushing", {}).get("command") == "pushall":
            self.report({
                "command": "push_status", "gcode_state": "IDLE", "mc_percent": 0,
                "nozzle_temper": 25.0, "bed_temper": 24.5, "bed_target_temper": 0,
            })

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


def test_connect_status_and_commands(broker):
    port, tls = broker
    fake = FakePrinter(port) if not tls else None
    if tls:
        # The fake printer talks plain MQTT, so for the TLS run only check the
        # TLS handshake + subscribe path works.
        p = BambuPrinter("127.0.0.1", SERIAL, CODE, port=port, use_tls=True)
        p.connect(timeout=10)
        assert p.is_connected
        p.disconnect()
        return

    seen = []
    p = BambuPrinter("127.0.0.1", SERIAL, CODE, port=port, use_tls=False)
    p.on_status = seen.append
    p.connect(timeout=10)
    assert p.wait_for_report(5), "no report after pushall"
    st = p.status()
    assert st.gcode_state == "IDLE" and st.state_label == "空闲"
    assert st.bed_temp == 24.5

    # Partial updates are merged into the existing state.
    fake.report({"gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 17})
    deadline = time.time() + 5
    while time.time() < deadline and p.status().percent != 42:
        time.sleep(0.05)
    st = p.status()
    assert (st.gcode_state, st.percent, st.remaining_min, st.bed_temp) == ("RUNNING", 42, 17, 24.5)

    p.start_print("job.3mf", plate=2)
    p.pause()
    p.send_gcode("G28")
    deadline = time.time() + 5
    while time.time() < deadline and len(fake.commands) < 4:
        time.sleep(0.05)
    start = fake.commands[1]["print"]
    assert start["command"] == "project_file"
    assert start["param"] == "Metadata/plate_2.gcode"
    assert start["url"] == "ftp://job.3mf"
    assert start["subtask_name"] == "job"
    assert fake.commands[2]["print"]["command"] == "pause"
    assert fake.commands[3]["print"] == {"sequence_id": fake.commands[3]["print"]["sequence_id"],
                                          "command": "gcode_line", "param": "G28\n"}
    p.disconnect()
    fake.close()


def test_connect_timeout():
    p = BambuPrinter("127.0.0.1", SERIAL, CODE, port=free_port(), use_tls=False)
    with pytest.raises(ConnectionError):
        p.connect(timeout=1)


def test_deep_merge():
    base = {"print": {"a": 1, "ams": {"x": 1}}}
    deep_merge(base, {"print": {"b": 2, "ams": {"y": 2}}})
    assert base == {"print": {"a": 1, "b": 2, "ams": {"x": 1, "y": 2}}}


# ---- FTP ------------------------------------------------------------------
@pytest.fixture
def ftp_server(tmp_path):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "sdcard"
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_user("bblp", CODE, str(root), perm="elradfmw")
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    port = free_port()
    server = FTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    t.start()
    yield port, root
    server.close_all()


def test_upload_and_list(ftp_server, tmp_path):
    port, root = ftp_server
    src = tmp_path / "part.3mf"
    src.write_bytes(b"x" * 100_000)
    seen = []
    name = upload_file("127.0.0.1", CODE, str(src), port=port, use_tls=False,
                       progress=lambda a, b: seen.append((a, b)))
    assert name == "part.3mf"
    assert (root / "part.3mf").read_bytes() == src.read_bytes()
    assert seen[-1] == (100_000, 100_000)
    assert "part.3mf" in list_files("127.0.0.1", CODE, port=port, use_tls=False)


# ---- misc -----------------------------------------------------------------
def test_sliced_plates(tmp_path):
    f = tmp_path / "a.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28")
        z.writestr("Metadata/plate_3.gcode", "G28")
        z.writestr("Metadata/plate_1.png", "")
    assert sliced_plates(str(f)) == [1, 3]
    g = tmp_path / "b.3mf"
    with zipfile.ZipFile(g, "w") as z:
        z.writestr("3D/3dmodel.model", "")
    assert sliced_plates(str(g)) == []
    assert sliced_plates(str(tmp_path / "missing.3mf")) == []


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "cfg.json"
    Settings(host="192.168.1.50", serial=SERIAL, access_code=CODE).save(path)
    s = Settings.load(path)
    assert (s.host, s.serial, s.access_code, s.url_prefix) == ("192.168.1.50", SERIAL, CODE, "ftp://")
    path.write_text("{bad json")
    assert Settings.load(path).host == ""
