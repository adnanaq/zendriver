from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from ..stealth import Persona, Platform, Seed
from . import generate
from .profile import Brand, ResolvedProfile

logger = logging.getLogger(__name__)

_FALLBACK_MAJOR = 126
_FALLBACK_VERSION = "126.0.0.0"

_OS_BY_PLATFORM = {
    Platform.WIN32: "windows",
    Platform.MAC_INTEL: "macos",
    Platform.LINUX_X86_64: "linux",
}
_UA_OS_TOKEN = {
    "windows": "Windows NT",
    "macos": "Mac OS X",
    "linux": "Linux",
}
_CH_PLATFORM = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}


class CoherenceError(Exception):
    """Raised by assert_coherent(strict=True) when signals disagree."""


def _parse_chrome(browser_info: Dict) -> Tuple[str, int]:
    m = re.search(r"Chrome/(\d+)\.([\d.]+)", browser_info.get("Browser", ""))
    if m:
        return f"{m.group(1)}.{m.group(2)}", int(m.group(1))
    logger.warning(
        "Could not parse Chrome version from browser info; using fallback %s",
        _FALLBACK_VERSION,
    )
    return _FALLBACK_VERSION, _FALLBACK_MAJOR


def _target_os(persona: Persona) -> str:
    if persona.platform is not None:
        return _OS_BY_PLATFORM.get(persona.platform, "linux")
    sys_plat = Persona.system().platform or Platform.LINUX_X86_64
    return _OS_BY_PLATFORM.get(sys_plat, "linux")


def resolve_profile(
    persona: Persona,
    browser_info: Dict,
    strict: bool = False,
    brands: Optional[Tuple[List[Brand], List[Brand]]] = None,
) -> ResolvedProfile:
    """Merge persona intent + the live browser's real Chrome version + a pool
    archetype into a coherent ResolvedProfile.

    ``brands`` (optional) supplies real UA-CH brands probed from the live browser
    — preferred over the fabricated fallback, since the GREASE brand string is
    version-specific and must not be invented.
    """
    os_name = _target_os(persona)
    version, major = _parse_chrome(browser_info)
    seed = persona.seed or Seed.random()

    profile = generate.sample(os_name, seed, version, major)

    # Prefer real browser-derived brands — the brand list is OS-independent, so
    # reusing it while spoofing only the platform fields stays coherent.
    if brands is not None:
        profile.ua_ch.brands, profile.ua_ch.full_version_list = brands

    # Overlay Persona intent (explicit overrides win where coherent).
    if persona.webgl is not None:
        if persona.webgl.unmasked_vendor:
            profile.webgl.unmasked_vendor = persona.webgl.unmasked_vendor
        if persona.webgl.unmasked_renderer:
            profile.webgl.unmasked_renderer = persona.webgl.unmasked_renderer
        profile.webgl.strategy = persona.webgl.strategy
    if persona.fonts is not None and persona.fonts.available:
        profile.fonts.available = persona.fonts.available
    if persona.timezone:
        profile.timezone = persona.timezone
    if persona.locale:
        profile.navigator.locale = persona.locale
        profile.navigator.languages = [persona.locale, persona.locale.split("-")[0]]
    if persona.hardware_concurrency is not None:
        profile.navigator.hardware_concurrency = persona.hardware_concurrency
    if persona.device_memory_gb is not None:
        profile.navigator.device_memory_gb = persona.device_memory_gb
    if persona.canvas is not None:
        profile.canvas = persona.canvas
    if persona.audio is not None:
        profile.audio = persona.audio
    if persona.client_rects is not None:
        profile.client_rects = persona.client_rects
    if persona.hardware is not None:
        profile.hardware = persona.hardware
    if persona.webrtc is not None:
        profile.webrtc = persona.webrtc

    # Screen override — read defensively in case a Persona has no screen attribute.
    screen_override = getattr(persona, "screen", None)
    if screen_override is not None:
        profile.screen = screen_override

    assert_coherent(profile, strict=strict)
    return profile


def assert_coherent(profile: ResolvedProfile, strict: bool = False) -> None:
    problems = []

    os_of_platform = _OS_BY_PLATFORM.get(profile.navigator.platform)
    if os_of_platform is None:
        problems.append(f"unknown navigator.platform {profile.navigator.platform}")
    else:
        token = _UA_OS_TOKEN[os_of_platform]
        if token not in profile.browser.ua_string:
            problems.append(
                f"UA string {profile.browser.ua_string!r} missing OS token {token!r} for {os_of_platform}"
            )
        ch_expected = _CH_PLATFORM[os_of_platform]
        if profile.ua_ch.platform != ch_expected:
            problems.append(
                f"ua_ch.platform {profile.ua_ch.platform!r} != {ch_expected!r}"
            )

    if not any(
        b.version == profile.browser.chrome_version
        for b in profile.ua_ch.full_version_list
    ):
        problems.append(
            "ua_ch.full_version_list does not contain the real chrome_version"
        )

    s = profile.screen
    if s.avail_width > s.width or s.avail_height > s.height:
        problems.append("screen avail dimensions exceed screen dimensions")

    if problems:
        msg = "Incoherent profile: " + "; ".join(problems)
        if strict:
            raise CoherenceError(msg)
        logger.warning(msg)
