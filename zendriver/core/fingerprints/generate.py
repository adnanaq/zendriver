from __future__ import annotations

import random
from typing import List, Tuple

from ..stealth import FontSpec, Platform, Seed, Strategy, SurfaceCfg, WebglSpec
from .pool import Archetype, load_pool
from .profile import (
    Brand,
    BrowserId,
    ClientHints,
    NavigatorSpec,
    ResolvedProfile,
    ScreenSpec,
)

_PLATFORM_BY_JS = {
    "Win32": Platform.WIN32,
    "MacIntel": Platform.MAC_INTEL,
    "Linux x86_64": Platform.LINUX_X86_64,
}


def _build_brands(
    chrome_major: int, full_version: str
) -> Tuple[List[Brand], List[Brand]]:
    """Last-resort offline fallback only.

    WARNING: the GREASE brand string and its position change every Chrome
    milestone by design — never rely on a hardcoded value. The resolver prefers
    brands derived from the real running browser (see runtime.py); this stub is
    used only when no live browser is available.
    """
    brands = [
        Brand("Chromium", str(chrome_major)),
        Brand("Google Chrome", str(chrome_major)),
        Brand("Not)A;Brand", "99"),
    ]
    full = [
        Brand("Chromium", full_version),
        Brand("Google Chrome", full_version),
        Brand("Not)A;Brand", "99.0.0.0"),
    ]
    return brands, full


def sample(
    os_name: str, seed: Seed, chrome_version: str, chrome_major: int
) -> ResolvedProfile:
    """Build a ResolvedProfile for ``os_name`` deterministically from ``seed``.

    The Chrome version is truthful (passed in from the live browser); only the
    OS-specific bits come from the curated archetype.
    """
    rng = random.Random(seed.value)
    arch: Archetype = rng.choice(load_pool(os_name))

    screen_raw = rng.choice(arch.screens)
    screen = ScreenSpec(
        width=int(screen_raw["width"]),
        height=int(screen_raw["height"]),
        avail_width=int(screen_raw["avail_width"]),
        avail_height=int(screen_raw["avail_height"]),
        device_pixel_ratio=float(screen_raw.get("device_pixel_ratio", 1.0)),
    )
    cpu = rng.choice(arch.hardware_concurrency)
    mem = rng.choice(arch.device_memory_gb)

    ua_string = arch.ua_template.format(version=chrome_version)
    brands, full = _build_brands(chrome_major, chrome_version)

    return ResolvedProfile(
        seed=seed,
        browser=BrowserId(
            chrome_version=chrome_version,
            chrome_major=chrome_major,
            ua_string=ua_string,
        ),
        navigator=NavigatorSpec(
            platform=_PLATFORM_BY_JS.get(arch.platform, Platform.LINUX_X86_64),
            hardware_concurrency=cpu,
            device_memory_gb=mem,
            languages=[arch.locale, arch.locale.split("-")[0]],
            locale=arch.locale,
        ),
        ua_ch=ClientHints(
            brands=brands,
            full_version_list=full,
            platform=arch.ch_platform,
            platform_version=arch.platform_version,
            architecture=arch.architecture,
            bitness=arch.bitness,
            full_version=chrome_version,
        ),
        screen=screen,
        # WebGL strings are carried for reference, but NATIVE by default: a
        # metadata-only renderer spoof that doesn't match the real GPU is
        # detectable, because MAX_TEXTURE_SIZE and the extension list still report
        # the real GPU. Cross-OS WebGL is handled via the real-GPU launch flag +
        # renderer reformatting, not by emitting this string.
        webgl=WebglSpec(
            strategy=Strategy.NATIVE,
            unmasked_vendor=arch.webgl_vendor,
            unmasked_renderer=arch.webgl_renderer,
        ),
        fonts=FontSpec(available=list(arch.fonts)),
        timezone=arch.timezone,
        canvas=SurfaceCfg(),
        audio=SurfaceCfg(),
        # Client rects default to NATIVE: any per-rect noise reconstructs DOMRects
        # from floats, which breaks the exact arithmetic identities a real browser
        # guarantees (right-left===width, etc.) and changes the fixed rotated-element
        # hash — both of which fingerprinters flag as a lie. A real browser returns
        # consistent rects, so NATIVE is the coherent (non-detectable) behavior; the
        # font/canvas/audio surfaces still carry cross-session entropy.
        client_rects=SurfaceCfg(strategy=Strategy.NATIVE),
    )
