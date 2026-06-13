import enum
import hashlib
import platform
import random
import socket
import sys
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Dict, List, Optional


class Strategy(enum.Enum):
    NATIVE = "Native"
    SEEDED = "Seeded"
    RANDOM = "Random"
    BLOCK = "Block"
    VALUE = "Value"


class Surface(enum.Enum):
    CANVAS = "Canvas"
    WEBGL = "Webgl"
    AUDIO = "Audio"
    FONTS = "Fonts"
    CLIENT_RECTS = "ClientRects"
    WEBRTC = "Webrtc"
    HARDWARE = "Hardware"

    @property
    def kind(self) -> "SurfaceKind":
        if self in (Surface.CANVAS, Surface.AUDIO, Surface.CLIENT_RECTS):
            return SurfaceKind.NOISE
        elif self in (Surface.WEBGL, Surface.FONTS, Surface.HARDWARE):
            return SurfaceKind.VALUE
        else:
            return SurfaceKind.POLICY

    def default_strategy(self) -> Strategy:
        if self.kind == SurfaceKind.NOISE:
            return Strategy.SEEDED
        elif self.kind == SurfaceKind.VALUE:
            return Strategy.VALUE
        else:
            return Strategy.BLOCK

    def resolve_strategy(self, requested: Optional[Strategy]) -> Strategy:
        if requested is None:
            return self.default_strategy()

        # Validate strategy against surface kind
        kind = self.kind
        ok = (
            requested == Strategy.NATIVE
            or requested == Strategy.BLOCK
            or (
                kind == SurfaceKind.NOISE
                and requested in (Strategy.SEEDED, Strategy.RANDOM)
            )
            or (kind == SurfaceKind.VALUE and requested == Strategy.VALUE)
            or (kind == SurfaceKind.POLICY and requested == Strategy.VALUE)
        )
        if ok:
            return requested
        else:
            import logging

            logging.getLogger(__name__).warning(
                f"Strategy {requested.name} not meaningful for surface {self.name}; using default {self.default_strategy().name}"
            )
            return self.default_strategy()


class SurfaceKind(enum.Enum):
    NOISE = "Noise"
    VALUE = "Value"
    POLICY = "Policy"


class Platform(enum.Enum):
    WIN32 = "Win32"
    MAC_INTEL = "MacIntel"
    LINUX_X86_64 = "Linux x86_64"

    def js_string(self) -> str:
        return self.value

    def ch_platform(self) -> str:
        if self == Platform.WIN32:
            return "Windows"
        elif self == Platform.MAC_INTEL:
            return "macOS"
        else:
            return "Linux"


@dataclass
class Seed:
    value: int

    @classmethod
    def random(cls) -> "Seed":
        # Generate a random 64-bit unsigned integer
        return cls(random.randint(0, (1 << 64) - 1))

    @classmethod
    def from_system(cls) -> "Seed":
        return cls(_get_system_seed())

    @classmethod
    def from_int(cls, val: int) -> "Seed":
        return cls(val)


def _get_system_seed() -> int:
    h = hashlib.sha256()
    # 1. Stable machine ID
    h.update(_get_machine_id().encode("utf-8"))
    # 2. Hostname
    h.update(socket.gethostname().encode("utf-8"))
    # Convert first 8 bytes of hash to 64-bit int
    return int.from_bytes(h.digest()[:8], byteorder="big")


def _get_machine_id() -> str:
    # Best-effort stable machine ID
    if sys.platform.startswith("linux"):
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path, "r") as f:
                    return f.read().strip()
            except Exception:
                pass
    elif sys.platform == "darwin":
        import subprocess

        try:
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]
            )
            for line in out.decode("utf-8").splitlines():
                if "IOPlatformUUID" in line:
                    return line.strip()
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                val, _ = winreg.QueryValueEx(key, "MachineGuid")
                return str(val)
        except Exception:
            pass
    return ""


@dataclass
class SurfaceCfg:
    strategy: Optional[Strategy] = None


@dataclass
class UaSpec:
    ua_string: Optional[str] = None
    platform: Optional[str] = None


@dataclass
class WebglSpec:
    strategy: Optional[Strategy] = None
    unmasked_vendor: Optional[str] = None
    unmasked_renderer: Optional[str] = None


@dataclass
class FontSpec:
    strategy: Optional[Strategy] = None
    available: Optional[List[str]] = None


@dataclass
class WebrtcSpec:
    strategy: Optional[Strategy] = None
    fake_ip: Optional[str] = None


@dataclass
class HardwareSpec:
    strategy: Optional[Strategy] = None
    battery_level: Optional[float] = None
    media_devices: Optional[int] = None
    speech_voices: Optional[List[str]] = None


@dataclass
class Persona:
    _cached_system: ClassVar[Optional["Persona"]] = None

    platform: Optional[Platform] = None
    ua: Optional[UaSpec] = None
    hardware_concurrency: Optional[int] = None
    device_memory_gb: Optional[int] = None
    timezone: Optional[str] = None
    locale: Optional[str] = None
    webgl: Optional[WebglSpec] = None
    canvas: Optional[SurfaceCfg] = None
    audio: Optional[SurfaceCfg] = None
    fonts: Optional[FontSpec] = None
    client_rects: Optional[SurfaceCfg] = None
    webrtc: Optional[WebrtcSpec] = None
    hardware: Optional[HardwareSpec] = None
    seed: Optional[Seed] = None
    screen: Optional[Any] = None  # ScreenSpec override (from fingerprints.profile)
    client_hints: Optional[Any] = None  # ClientHints override (from fingerprints.profile)

    def overlay(self, over: "Persona") -> "Persona":
        # Perform field-wise overlay (over overrides self)
        merged = Persona()
        for field_name in self.__dataclass_fields__:
            # __dataclass_fields__ also lists ClassVars (e.g. the _cached_system
            # cache). Skip private/cache fields — overlaying them would recurse
            # (the cache holds a Persona, which itself has .overlay).
            if field_name.startswith("_"):
                continue
            val_self = getattr(self, field_name)
            val_over = getattr(over, field_name)

            if val_over is not None:
                if (
                    val_self is not None
                    and hasattr(val_self, "overlay")
                    and hasattr(val_over, "overlay")
                ):
                    # Recursive overlay for spec fields
                    setattr(merged, field_name, val_self.overlay(val_over))
                else:
                    setattr(merged, field_name, val_over)
            else:
                setattr(merged, field_name, val_self)
        return merged

    def apply_surface_override(self, surface: Surface, strategy: Strategy) -> None:
        if surface == Surface.CANVAS:
            if self.canvas is None:
                self.canvas = SurfaceCfg()
            self.canvas.strategy = strategy
        elif surface == Surface.AUDIO:
            if self.audio is None:
                self.audio = SurfaceCfg()
            self.audio.strategy = strategy
        elif surface == Surface.CLIENT_RECTS:
            if self.client_rects is None:
                self.client_rects = SurfaceCfg()
            self.client_rects.strategy = strategy
        elif surface == Surface.WEBGL:
            if self.webgl is None:
                self.webgl = WebglSpec()
            self.webgl.strategy = strategy
        elif surface == Surface.FONTS:
            if self.fonts is None:
                self.fonts = FontSpec()
            self.fonts.strategy = strategy
        elif surface == Surface.WEBRTC:
            if self.webrtc is None:
                self.webrtc = WebrtcSpec()
            self.webrtc.strategy = strategy
        elif surface == Surface.HARDWARE:
            if self.hardware is None:
                self.hardware = HardwareSpec()
            self.hardware.strategy = strategy

    def resolved_platform_js(self) -> str:
        plat = self.platform
        if plat is None:
            plat = Persona.system().platform or Platform.LINUX_X86_64
        return plat.js_string()

    @classmethod
    def sample(
        cls,
        os: str = "windows",
        seed: Optional[int] = None,
        chrome_major: Optional[int] = None,
    ) -> "Persona":
        """Intent helper: request a coherent profile for a target OS.

        The resolver (fingerprints.resolve) fills the rest from the live browser
        + pool. ``chrome_major`` is accepted for forward-compatibility with pool
        filtering and is currently unused (the resolver reads the real version
        from the live browser).
        """
        plat = {
            "windows": Platform.WIN32,
            "macos": Platform.MAC_INTEL,
            "linux": Platform.LINUX_X86_64,
        }.get(os.lower(), Platform.LINUX_X86_64)
        return cls(
            platform=plat,
            seed=Seed.from_int(seed) if seed is not None else None,
        )

    @classmethod
    def system(cls) -> "Persona":
        if cls._cached_system is not None:
            return cls._cached_system
        import os

        plat_str = platform.system().lower()
        if "win" in plat_str:
            plat = Platform.WIN32
        elif "mac" in plat_str or "darwin" in plat_str:
            plat = Platform.MAC_INTEL
        else:
            plat = Platform.LINUX_X86_64

        cpu_count = max(2, min(32, os.cpu_count() or 4))
        try:
            import psutil

            total_bytes = psutil.virtual_memory().total
            gb = max(4, min(8, int(total_bytes / (1024**3))))
        except Exception:
            gb = 4

        cls._cached_system = cls(
            platform=plat,
            hardware_concurrency=cpu_count,
            device_memory_gb=gb,
            seed=Seed.random(),
        )
        return cls._cached_system

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Persona":
        # Robust dictionary parser
        p = cls()
        if "platform" in d and d["platform"]:
            p.platform = Platform(d["platform"])
        if "ua" in d and d["ua"]:
            p.ua = UaSpec(**d["ua"])
        if "hardware_concurrency" in d:
            p.hardware_concurrency = d["hardware_concurrency"]
        if "device_memory_gb" in d:
            p.device_memory_gb = d["device_memory_gb"]
        if "timezone" in d:
            p.timezone = d["timezone"]
        if "locale" in d:
            p.locale = d["locale"]
        if "webgl" in d and d["webgl"]:
            p.webgl = WebglSpec(**d["webgl"])
        if "canvas" in d and d["canvas"]:
            p.canvas = SurfaceCfg(**d["canvas"])
        if "audio" in d and d["audio"]:
            p.audio = SurfaceCfg(**d["audio"])
        if "fonts" in d and d["fonts"]:
            p.fonts = FontSpec(**d["fonts"])
        if "client_rects" in d and d["client_rects"]:
            p.client_rects = SurfaceCfg(**d["client_rects"])
        if "webrtc" in d and d["webrtc"]:
            p.webrtc = WebrtcSpec(**d["webrtc"])
        if "hardware" in d and d["hardware"]:
            p.hardware = HardwareSpec(**d["hardware"])
        if "seed" in d and d["seed"]:
            if isinstance(d["seed"], dict):
                p.seed = Seed(d["seed"]["value"])
            else:
                p.seed = Seed(int(d["seed"]))
        return p


def parse_persona(val: Any) -> Optional[Persona]:
    if val is None:
        return None
    if isinstance(val, Persona):
        return val
    if isinstance(val, str):
        # Could be JSON
        import json

        try:
            val = json.loads(val)
        except Exception:
            pass
    if isinstance(val, dict):
        # Check if it's a browserforge fingerprint
        if "fingerprint" in val or "userAgent" in val or "navigator" in val:
            return _persona_from_browserforge_dict(val)
        return Persona.from_dict(val)
    return None


def _persona_from_browserforge_dict(val: Dict[str, Any]) -> Persona:
    fp = val.get("fingerprint", val)
    p = Persona()

    # 1. Navigator
    nav = fp.get("navigator", {})
    if "platform" in nav:
        plat_str = nav["platform"]
        if plat_str == "Win32":
            p.platform = Platform.WIN32
        elif plat_str == "MacIntel":
            p.platform = Platform.MAC_INTEL
        else:
            p.platform = Platform.LINUX_X86_64

    if "deviceMemory" in nav:
        p.device_memory_gb = nav["deviceMemory"]
    if "hardwareConcurrency" in nav:
        p.hardware_concurrency = nav["hardwareConcurrency"]
    if "language" in nav:
        p.locale = nav["language"]

    # 2. User Agent
    ua_str = (
        nav.get("userAgent")
        or fp.get("userAgent")
        or fp.get("headers", {}).get("User-Agent")
    )
    if ua_str:
        p.ua = UaSpec(ua_string=ua_str, platform=nav.get("platform"))

    # 3. Video Card (WebGL)
    vc = fp.get("videoCard", {})
    vendor = vc.get("vendor")
    renderer = vc.get("renderer")
    if vendor or renderer:
        p.webgl = WebglSpec(
            strategy=Strategy.VALUE, unmasked_vendor=vendor, unmasked_renderer=renderer
        )

    # 4. Fonts
    fonts = fp.get("fonts")
    if fonts:
        p.fonts = FontSpec(strategy=Strategy.VALUE, available=fonts)

    # 5. Hardware (Battery)
    bat = fp.get("battery", {})
    if bat:
        level = bat.get("level") if isinstance(bat, dict) else bat
        if isinstance(level, (int, float)):
            p.hardware = HardwareSpec(
                strategy=Strategy.VALUE, battery_level=float(level)
            )

    # 6. Screen
    scr = fp.get("screen")
    if isinstance(scr, dict) and "width" in scr and "height" in scr:
        from .fingerprints.profile import ScreenSpec

        p.screen = ScreenSpec(
            width=int(scr["width"]),
            height=int(scr["height"]),
            avail_width=int(scr.get("availWidth", scr["width"])),
            avail_height=int(scr.get("availHeight", scr["height"])),
            color_depth=int(scr.get("colorDepth", 24)),
            pixel_depth=int(scr.get("pixelDepth", 24)),
            device_pixel_ratio=float(scr.get("devicePixelRatio", 1.0)),
        )

    # 7. Timezone
    tz = fp.get("timezone") or val.get("timezone")
    if isinstance(tz, str):
        p.timezone = tz

    return p


