from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..stealth import (
    FontSpec,
    HardwareSpec,
    Platform,
    Seed,
    SurfaceCfg,
    WebglSpec,
    WebrtcSpec,
)


@dataclass
class Brand:
    brand: str
    version: str


@dataclass
class BrowserId:
    """Real Chrome engine identity. Version is truthful; ua_string is rebuilt for the target OS."""

    chrome_version: str
    chrome_major: int
    ua_string: str


@dataclass
class NavigatorSpec:
    platform: Platform
    hardware_concurrency: int
    device_memory_gb: int
    languages: List[str]
    locale: str


# Field set mirrors cdp.emulation.UserAgentMetadata exactly so this maps 1:1 when
# applied via Network.setUserAgentOverride. full_version and form_factors are the
# less-obvious members; form_factors is a list such as ["Desktop"].
@dataclass
class ClientHints:
    """Maps directly to CDP Network.setUserAgentOverride userAgentMetadata."""

    brands: List[Brand]
    full_version_list: List[Brand]
    platform: str
    platform_version: str
    architecture: str = "x86"
    bitness: str = "64"
    model: str = ""
    mobile: bool = False
    wow64: bool = False
    full_version: str = ""
    form_factors: List[str] = field(default_factory=lambda: ["Desktop"])


@dataclass
class ScreenSpec:
    width: int
    height: int
    avail_width: int
    avail_height: int
    color_depth: int = 24
    pixel_depth: int = 24
    device_pixel_ratio: float = 1.0


@dataclass
class TransportSpec:
    """Placeholder for sub-project D (TLS/JA3, header order, IP). Unused here."""

    pass


@dataclass
class ResolvedProfile:
    """The single coherent object the JS layer and the CDP layer both consume."""

    seed: Seed
    browser: BrowserId
    navigator: NavigatorSpec
    ua_ch: ClientHints
    screen: ScreenSpec
    webgl: WebglSpec
    fonts: FontSpec
    timezone: str
    canvas: SurfaceCfg = field(default_factory=SurfaceCfg)
    audio: SurfaceCfg = field(default_factory=SurfaceCfg)
    client_rects: SurfaceCfg = field(default_factory=SurfaceCfg)
    hardware: Optional[HardwareSpec] = None
    webrtc: Optional[WebrtcSpec] = None
    transport: Optional[TransportSpec] = None
