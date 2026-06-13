from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Dict, List


class FingerprintPoolError(Exception):
    """Raised when a pool file is missing, unknown, or malformed."""


_VALID_OS = {"windows", "macos", "linux"}
_REQUIRED_KEYS = {
    "platform",
    "ch_platform",
    "platform_version",
    "architecture",
    "bitness",
    "ua_template",
    "webgl_vendor",
    "webgl_renderer",
    "fonts",
    "screens",
    "hardware_concurrency",
    "device_memory_gb",
    "timezone",
    "locale",
}


@dataclass
class Archetype:
    os: str
    platform: str
    ch_platform: str
    platform_version: str
    architecture: str
    bitness: str
    ua_template: str
    webgl_vendor: str
    webgl_renderer: str
    fonts: List[str]
    screens: List[Dict[str, float]]
    hardware_concurrency: List[int]
    device_memory_gb: List[int]
    timezone: str
    locale: str


_CACHE: Dict[str, List[Archetype]] = {}


def load_pool(os_name: str) -> List[Archetype]:
    if os_name not in _VALID_OS:
        raise FingerprintPoolError(f"No fingerprint pool for OS {os_name!r}")
    if os_name in _CACHE:
        return _CACHE[os_name]
    try:
        text = (resources.files(__package__) / "pool" / f"{os_name}.json").read_text(
            "utf-8"
        )
        raw = json.loads(text)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise FingerprintPoolError(f"Failed to load pool {os_name!r}: {exc}") from exc

    entries: List[Archetype] = []
    for i, item in enumerate(raw):
        missing = _REQUIRED_KEYS - item.keys()
        if missing:
            raise FingerprintPoolError(
                f"{os_name}[{i}] missing keys: {sorted(missing)}"
            )
        entries.append(Archetype(os=os_name, **{k: item[k] for k in _REQUIRED_KEYS}))
    if not entries:
        raise FingerprintPoolError(f"Pool {os_name!r} is empty")
    _CACHE[os_name] = entries
    return entries
