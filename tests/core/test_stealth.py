"""Tests for fingerprint spoofing patches.

Unit tests run without a browser. Integration tests require a real Chrome
and are marked @pytest.mark.integration — opt in with:
    uv run pytest tests/core/test_stealth.py -m integration
"""

import pytest

import zendriver as zd
from tests.conftest import CreateBrowser
from zendriver.core.stealth import (
    Fingerprint,
    Persona,
    Platform,
    Seed,
    Strategy,
    Surface,
    SurfaceCfg,
    SurfaceKind,
    WebglSpec,
)
from zendriver.core.stealth_patches import bootstrap_script


def _fp() -> Fingerprint:
    return Fingerprint(
        chrome_version="124.0.0.0",
        chrome_major=124,
        ua_string="Mozilla/5.0",
        platform=Platform.LINUX_X86_64,
        cpu_count=4,
        memory_gb=8,
        locale="en-US",
    )


# ===========================================================================
# Surface / Strategy resolution
# ===========================================================================


def test_surface_default_strategies() -> None:
    assert Surface.CANVAS.default_strategy() == Strategy.SEEDED
    assert Surface.AUDIO.default_strategy() == Strategy.SEEDED
    assert Surface.CLIENT_RECTS.default_strategy() == Strategy.SEEDED
    assert Surface.WEBGL.default_strategy() == Strategy.VALUE
    assert Surface.WEBRTC.default_strategy() == Strategy.BLOCK


def test_surface_kind_classification() -> None:
    assert Surface.CANVAS.kind == SurfaceKind.NOISE
    assert Surface.WEBGL.kind == SurfaceKind.VALUE
    assert Surface.WEBRTC.kind == SurfaceKind.POLICY


def test_resolve_strategy_native_and_invalid() -> None:
    # NATIVE is always valid
    for surface in Surface:
        assert surface.resolve_strategy(Strategy.NATIVE) == Strategy.NATIVE
    # SEEDED on a VALUE surface falls back to default
    assert Surface.WEBGL.resolve_strategy(Strategy.SEEDED) == Strategy.VALUE
    # None → default
    assert Surface.CANVAS.resolve_strategy(None) == Strategy.SEEDED


# ===========================================================================
# Seed
# ===========================================================================


def test_seed_construction() -> None:
    assert Seed.from_int(42).value == Seed.from_int(42).value
    assert Seed.from_system().value == Seed.from_system().value
    assert isinstance(Seed.random().value, int)


# ===========================================================================
# bootstrap_script — script generation
# ===========================================================================


def test_shield_and_prng_always_present() -> None:
    script = bootstrap_script(Persona(), _fp())
    assert "__zdNative" in script
    assert "__zdRng" in script


def test_canvas_strategy_script_output() -> None:
    fp = _fp()
    # SEEDED: patch present, seed value substituted
    seeded = bootstrap_script(
        Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(123)),
        fp,
    )
    assert "getImageData" in seeded
    assert "})(123)" in seeded

    # NATIVE: patch entirely absent
    native = bootstrap_script(Persona(canvas=SurfaceCfg(strategy=Strategy.NATIVE)), fp)
    assert "getImageData" not in native

    # RANDOM: Math.random re-seeds per page
    random = bootstrap_script(Persona(canvas=SurfaceCfg(strategy=Strategy.RANDOM)), fp)
    assert "Math.random()*4294967296" in random


def test_webgl_strategy_script_output() -> None:
    fp = _fp()
    # VALUE: vendor/renderer substituted
    valued = bootstrap_script(
        Persona(
            webgl=WebglSpec(
                strategy=Strategy.VALUE,
                unmasked_vendor="Google Inc.",
                unmasked_renderer="ANGLE (RTX 4090)",
            )
        ),
        fp,
    )
    assert "Google Inc." in valued
    assert "ANGLE (RTX 4090)" in valued

    # NATIVE: WebGL IIFE not emitted
    native = bootstrap_script(Persona(webgl=WebglSpec(strategy=Strategy.NATIVE)), fp)
    assert "0x9245" not in native


def test_script_determinism_across_seeds() -> None:
    fp = _fp()
    p = Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(42))
    assert bootstrap_script(p, fp) == bootstrap_script(p, fp)

    p2 = Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(99))
    assert bootstrap_script(p, fp) != bootstrap_script(p2, fp)


def test_persona_overlay_merges_fields() -> None:
    base = Persona(seed=Seed.from_int(1), canvas=SurfaceCfg(strategy=Strategy.NATIVE))
    merged = base.overlay(Persona(seed=Seed.from_int(99)))
    assert merged.seed is not None and merged.seed.value == 99
    assert merged.canvas is not None and merged.canvas.strategy == Strategy.NATIVE


# ===========================================================================
# Integration tests — real browser required
# ===========================================================================

_CANVAS_JS = """\
(function(){
    var c = document.createElement('canvas');
    c.width = 50; c.height = 20;
    var x = c.getContext('2d');
    x.fillStyle = '#f60'; x.fillRect(0, 0, 50, 20);
    x.fillStyle = '#069'; x.font = '12px Arial';
    x.fillText('zd', 2, 12);
    return c.toDataURL();
})()"""


@pytest.mark.integration
async def test_seeded_canvas_is_stable(headless: bool) -> None:
    persona = Persona(
        canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(42)
    )
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("about:blank")
        a = await tab.evaluate(_CANVAS_JS)
        b = await tab.evaluate(_CANVAS_JS)
        assert a == b
    finally:
        await browser.stop()


@pytest.mark.integration
async def test_seeds_differ_across_instances(headless: bool) -> None:
    p1 = Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(1))
    p2 = Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(9999))

    b1 = await zd.start(headless=headless, persona=p1)
    try:
        url1 = await (await b1.get("about:blank")).evaluate(_CANVAS_JS)
    finally:
        await b1.stop()

    b2 = await zd.start(headless=headless, persona=p2)
    try:
        url2 = await (await b2.get("about:blank")).evaluate(_CANVAS_JS)
    finally:
        await b2.stop()

    assert url1 != url2


@pytest.mark.integration
async def test_native_shield_active(headless: bool) -> None:
    persona = Persona(
        canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(42)
    )
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("about:blank")
        result = await tab.evaluate(
            "/native/.test(HTMLCanvasElement.prototype.toDataURL.toString())"
        )
        assert result is True
    finally:
        await browser.stop()


@pytest.mark.integration
async def test_persona_patches_injected(headless: bool) -> None:
    persona = Persona(
        canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(42)
    )
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("about:blank")
        assert await tab.evaluate("typeof __zdRng") == "function"
    finally:
        await browser.stop()
