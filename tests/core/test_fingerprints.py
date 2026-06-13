"""Unit tests for the fingerprints package (ResolvedProfile model + source + resolver).

Unit tests run without a browser.
"""

from zendriver.core.stealth import FontSpec, Platform, Seed, SurfaceCfg, WebglSpec
from zendriver.core.fingerprints.profile import (
    Brand,
    BrowserId,
    ClientHints,
    NavigatorSpec,
    ResolvedProfile,
    ScreenSpec,
)


def _profile() -> ResolvedProfile:
    return ResolvedProfile(
        seed=Seed.from_int(7),
        browser=BrowserId(
            "124.0.6367.91",
            124,
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.6367.91",
        ),
        navigator=NavigatorSpec(Platform.WIN32, 8, 16, ["en-US", "en"], "en-US"),
        ua_ch=ClientHints(
            brands=[Brand("Chromium", "124")],
            full_version_list=[Brand("Chromium", "124.0.6367.91")],
            platform="Windows",
            platform_version="15.0.0",
            architecture="x86",
            bitness="64",
            model="",
            mobile=False,
            wow64=False,
        ),
        screen=ScreenSpec(1920, 1080, 1920, 1040, 24, 24, 1.0),
        webgl=WebglSpec(unmasked_vendor="Google Inc. (NVIDIA)", unmasked_renderer="ANGLE (NVIDIA)"),
        fonts=FontSpec(available=["Segoe UI"]),
        timezone="America/New_York",
        canvas=SurfaceCfg(),
        audio=SurfaceCfg(),
        client_rects=SurfaceCfg(),
        hardware=None,
        webrtc=None,
    )


def test_profile_constructs_with_transport_default_none() -> None:
    p = _profile()
    assert p.transport is None
    assert p.navigator.platform == Platform.WIN32
    assert p.ua_ch.brands[0].brand == "Chromium"


# ---------------------------------------------------------------------------
# Pool loader
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from zendriver.core.fingerprints.pool import (  # noqa: E402
    Archetype,
    FingerprintPoolError,
    load_pool,
)


@pytest.mark.parametrize("os_name", ["windows", "macos", "linux"])
def test_pool_loads_and_is_well_formed(os_name: str) -> None:
    entries = load_pool(os_name)
    assert len(entries) >= 1
    for e in entries:
        assert isinstance(e, Archetype)
        assert e.platform and e.ch_platform and e.ua_template
        assert "{version}" in e.ua_template
        assert e.webgl_vendor and e.webgl_renderer
        assert len(e.fonts) >= 3
        assert len(e.screens) >= 1
        assert len(e.hardware_concurrency) >= 1
        assert len(e.device_memory_gb) >= 1


def test_pool_unknown_os_raises() -> None:
    with pytest.raises(FingerprintPoolError):
        load_pool("solaris")


# ---------------------------------------------------------------------------
# Generative sampler
# ---------------------------------------------------------------------------

from zendriver.core.stealth import Strategy  # noqa: E402
from zendriver.core.fingerprints.generate import sample  # noqa: E402


def test_sample_is_deterministic_for_seed() -> None:
    a = sample("windows", Seed.from_int(42), "124.0.6367.91", 124)
    b = sample("windows", Seed.from_int(42), "124.0.6367.91", 124)
    assert a == b


def test_sample_builds_coherent_identity() -> None:
    p = sample("windows", Seed.from_int(1), "124.0.6367.91", 124)
    assert "124.0.6367.91" in p.browser.ua_string
    assert "Windows NT 10.0" in p.browser.ua_string
    assert p.navigator.platform == Platform.WIN32
    assert any(b.version == "124.0.6367.91" for b in p.ua_ch.full_version_list)
    assert p.ua_ch.platform == "Windows"
    assert p.navigator.device_memory_gb in (8, 16)
    assert p.navigator.hardware_concurrency in (8, 12, 16)
    # WebGL is carried but NATIVE by default (not emitted): a metadata-only
    # renderer spoof that doesn't match the real GPU is detectable.
    assert p.webgl.strategy == Strategy.NATIVE


def test_sample_varies_with_seed() -> None:
    mems = {
        sample("windows", Seed.from_int(s), "124.0.6367.91", 124).navigator.device_memory_gb
        for s in range(20)
    }
    assert len(mems) >= 2


# ---------------------------------------------------------------------------
# Resolver + coherence guard
# ---------------------------------------------------------------------------

from zendriver.core.stealth import Persona  # noqa: E402
from zendriver.core.fingerprints.resolve import (  # noqa: E402
    CoherenceError,
    assert_coherent,
    resolve_profile,
)

_INFO = {
    "Browser": "Chrome/124.0.6367.91",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0.6367.91",
}


def test_resolve_uses_persona_platform_over_host() -> None:
    persona = Persona(platform=Platform.WIN32, seed=Seed.from_int(5))
    profile = resolve_profile(persona, _INFO)
    assert profile.navigator.platform == Platform.WIN32
    assert "Windows NT 10.0" in profile.browser.ua_string
    assert profile.ua_ch.platform == "Windows"


def test_resolve_keeps_real_chrome_version() -> None:
    profile = resolve_profile(Persona(platform=Platform.WIN32, seed=Seed.from_int(5)), _INFO)
    assert profile.browser.chrome_version == "124.0.6367.91"
    assert profile.browser.chrome_major == 124


def test_resolve_applies_persona_webgl_override() -> None:
    persona = Persona(
        platform=Platform.WIN32,
        seed=Seed.from_int(5),
        webgl=WebglSpec(strategy=Strategy.VALUE, unmasked_vendor="ACME", unmasked_renderer="ACME GPU"),
    )
    profile = resolve_profile(persona, _INFO)
    assert profile.webgl.unmasked_vendor == "ACME"


def test_assert_coherent_rejects_platform_ua_mismatch() -> None:
    profile = resolve_profile(Persona(platform=Platform.WIN32, seed=Seed.from_int(5)), _INFO)
    profile.navigator.platform = Platform.MAC_INTEL
    with pytest.raises(CoherenceError):
        assert_coherent(profile, strict=True)


def test_resolve_unparseable_version_falls_back() -> None:
    profile = resolve_profile(Persona(platform=Platform.WIN32, seed=Seed.from_int(5)), {"Browser": "garbage"})
    assert profile.browser.chrome_major == 126


# ---------------------------------------------------------------------------
# Persona.sample + browserforge extension + screen override
# ---------------------------------------------------------------------------

from zendriver.core.stealth import parse_persona  # noqa: E402


def test_persona_sample_sets_platform_and_seed() -> None:
    p = Persona.sample(os="macos", seed=7)
    assert p.platform == Platform.MAC_INTEL
    assert p.seed is not None and p.seed.value == 7


def test_browserforge_dict_captures_timezone_and_screen() -> None:
    bf = {
        "navigator": {
            "platform": "Win32",
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
        },
        "screen": {
            "width": 2560,
            "height": 1440,
            "availWidth": 2560,
            "availHeight": 1400,
            "devicePixelRatio": 1.0,
        },
        "timezone": "Europe/Paris",
    }
    p = parse_persona(bf)
    assert p is not None
    assert p.timezone == "Europe/Paris"
    assert p.screen is not None and p.screen.width == 2560


def test_resolve_applies_persona_screen_override() -> None:
    persona = Persona(
        platform=Platform.WIN32,
        seed=Seed.from_int(5),
        screen=ScreenSpec(3440, 1440, 3440, 1400, 24, 24, 1.0),
    )
    profile = resolve_profile(persona, _INFO)
    assert profile.screen.width == 3440


# ---------------------------------------------------------------------------
# Runtime brand probe
# ---------------------------------------------------------------------------

from zendriver.core.fingerprints.runtime import parse_brands  # noqa: E402


def test_parse_brands_roundtrip() -> None:
    payload = {
        "brands": [{"brand": "Chromium", "version": "148"}],
        "full": [{"brand": "Chromium", "version": "148.0.7778.178"}],
    }
    result = parse_brands(payload)
    assert result is not None
    brands, full = result
    assert brands[0].brand == "Chromium" and brands[0].version == "148"
    assert full[0].version == "148.0.7778.178"


def test_parse_brands_none_on_empty() -> None:
    assert parse_brands(None) is None
    assert parse_brands({"brands": [], "full": []}) is None


def test_resolve_uses_supplied_real_brands() -> None:
    real = ([Brand("Chromium", "148")], [Brand("Chromium", "148.0.7778.178")])
    profile = resolve_profile(
        Persona(platform=Platform.WIN32, seed=Seed.from_int(5)), _INFO, brands=real
    )
    assert profile.ua_ch.brands == real[0]
    assert profile.ua_ch.full_version_list == real[1]


def test_resolve_falls_back_to_fabricated_brands_without_probe() -> None:
    profile = resolve_profile(Persona(platform=Platform.WIN32, seed=Seed.from_int(5)), _INFO)
    assert len(profile.ua_ch.brands) >= 1  # fabricated fallback present
