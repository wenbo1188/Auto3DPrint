"""Settings stored as JSON in the user's app-data folder."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def config_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    return Path(base) / "Auto3DPrint" / "config.json"


@dataclass
class Settings:
    host: str = ""
    serial: str = ""
    access_code: str = ""
    mqtt_port: int = 8883
    ftp_port: int = 990
    # "ftp://" works on P1 series; try "file:///sdcard/" if P2S refuses it.
    url_prefix: str = "ftp://"
    use_ams: bool = False
    bed_leveling: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
