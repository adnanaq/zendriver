"""Tests for fingerprint spoofing patches.

Unit tests run without a browser. Integration tests require a real Chrome
and are marked @pytest.mark.integration — opt in with:
    uv run pytest tests/core/test_stealth.py -m integration
"""

import pytest

import zendriver as zd
from zendriver.core.stealth import (
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
from zendriver.core.fingerprints.resolve import resolve_profile

_INFO = {
    "Browser": "Chrome/124.0.6367.91",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.6367.91",
}


def _script(persona: Persona) -> str:
    """Resolve a Windows profile from ``persona`` intent and return the JS bootstrap."""
    if persona.seed is None:
        persona.seed = Seed.from_int(123)
    if persona.platform is None:
        persona.platform = Platform.WIN32
    return bootstrap_script(resolve_profile(persona, _INFO))


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
    script = _script(Persona())
    assert "__zdNative" in script
    assert "__zdRng" in script


def test_canvas_strategy_script_output() -> None:
    # SEEDED: patch present, seed value substituted
    seeded = _script(
        Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(123))
    )
    assert "getImageData" in seeded
    assert "})(123)" in seeded

    # NATIVE: patch entirely absent
    native = _script(Persona(canvas=SurfaceCfg(strategy=Strategy.NATIVE)))
    assert "getImageData" not in native

    # RANDOM: Math.random re-seeds per page
    rnd = _script(Persona(canvas=SurfaceCfg(strategy=Strategy.RANDOM)))
    assert "Math.random()*4294967296" in rnd


def test_webgl_strategy_script_output() -> None:
    # VALUE: vendor/renderer substituted
    valued = _script(
        Persona(
            webgl=WebglSpec(
                strategy=Strategy.VALUE,
                unmasked_vendor="Google Inc.",
                unmasked_renderer="ANGLE (RTX 4090)",
            )
        )
    )
    assert "Google Inc." in valued
    assert "ANGLE (RTX 4090)" in valued

    # NATIVE: WebGL IIFE not emitted
    native = _script(Persona(webgl=WebglSpec(strategy=Strategy.NATIVE)))
    assert "0x9245" not in native


def test_script_determinism_across_seeds() -> None:
    p = Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(42))
    assert _script(p) == _script(p)

    p2 = Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(99))
    p3 = Persona(canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(42))
    assert _script(p3) != _script(p2)


def test_deviceMemory_and_platform_are_coherent() -> None:
    script = _script(Persona(platform=Platform.WIN32, seed=Seed.from_int(1)))
    assert "deviceMemory" in script  # navigator.deviceMemory must be spoofed
    assert "Win32" in script  # navigator.platform matches the Windows UA


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


# Detect font presence the way fingerprinters do: width of a probe string in
# "<font>, sans-serif" vs pure sans-serif fallback (differ => present).
_FONT_DETECT_JS = """
(async function(){
  var probe = "mmmmmmmmmmlli WWW 0123456789";
  var ctx = document.createElement('canvas').getContext('2d');
  function w(font){ ctx.font = '72px ' + font; return ctx.measureText(probe).width; }
  var base = w('sans-serif');
  function present(f){ ctx.font = '72px "' + f + '", sans-serif'; return Math.abs(ctx.measureText(probe).width - base) > 0.01; }
  async function ff(f){ try{ var x=new FontFace(f,'local("'+f+'")'); await x.load(); return x.status==='loaded'; }catch(e){ return false; } }
  return JSON.stringify({
    segoe: present('Segoe UI'),
    consolas: present('Consolas'),
    arial: present('Arial'),
    nope: present('NoSuchFontXYZ123'),
    checkSegoe: document.fonts.check('12px "Segoe UI"'),
    checkNope: document.fonts.check('12px "NoSuchFontXYZ123"'),
    ffSegoe: await ff('Segoe UI'),
    ffLiberation: await ff('Liberation Mono'),
  });
})()
"""


@pytest.mark.integration
async def test_cross_os_full_coherence(headless: bool) -> None:
    """End-to-end: a Windows persona on this (Linux) host presents a coherent
    identity across every JS-readable signal at once."""
    import json

    persona = Persona.sample(os="windows", seed=4242)
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("https://example.com")
        await tab.wait(1.0)
        d = json.loads(
            await tab.evaluate(
                """(async function(){
                    var uad = navigator.userAgentData;
                    var h = uad ? await uad.getHighEntropyValues(["platform"]) : {};
                    return JSON.stringify({
                        ua: navigator.userAgent,
                        platform: navigator.platform,
                        deviceMemory: navigator.deviceMemory,
                        chPlatform: h.platform,
                        shield: /native/.test(HTMLCanvasElement.prototype.toDataURL.toString()),
                    });
                })()""",
                await_promise=True,
            )
        )
        assert "Windows NT" in d["ua"]  # UA string
        assert d["platform"] == "Win32"  # navigator.platform
        assert d["chPlatform"] == "Windows"  # UA-CH platform
        assert d["deviceMemory"] in (8, 16)  # spoofed from the Windows pool
        assert d["shield"] is True  # patched APIs still look native
    finally:
        await browser.stop()


_WEBGL_PROBE = """(function(){
    var gl = document.createElement('canvas').getContext('webgl');
    var ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return JSON.stringify({
        vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : '',
        renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : '',
        maxTex: gl ? gl.getParameter(gl.MAX_TEXTURE_SIZE) : 0,
    });
})()"""


@pytest.mark.integration
async def test_webgl_works_with_persona(headless: bool) -> None:
    """A persona must NOT break WebGL: vendor + renderer must be non-empty (this is
    what bot.sannysoft.com checks). Guards against forcing a GPU backend the host
    can't provide."""
    import json

    persona = Persona.sample(os="windows", seed=3)
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("about:blank")
        d = json.loads(await tab.evaluate(_WEBGL_PROBE))
        assert d["vendor"], "WebGL vendor must be present"
        assert d["renderer"], "WebGL renderer must be present"
        assert d["maxTex"] >= 2048
    finally:
        await browser.stop()


@pytest.mark.integration
async def test_real_gpu_opt_in(headless: bool) -> None:
    """Opting into a real GPU backend exposes a non-SwiftShader renderer. Skipped on
    hosts where the backend is unavailable."""
    import json

    persona = Persona.sample(os="windows", seed=3)
    browser = await zd.start(
        headless=headless, persona=persona, browser_args=["--use-angle=vulkan"]
    )
    try:
        tab = await browser.get("about:blank")
        d = json.loads(await tab.evaluate(_WEBGL_PROBE))
        if not d["renderer"] or "SwiftShader" in d["renderer"]:
            pytest.skip("real GPU backend unavailable on this host")
        assert d["maxTex"] >= 16384
    finally:
        await browser.stop()


@pytest.mark.integration
async def test_webgl_renderer_os_coherent(headless: bool) -> None:
    """Cross-OS (Windows on Linux) with a real GPU: the renderer must use Windows
    Direct3D11 wording, not the Linux Vulkan/OpenGL backend. Skipped without a real
    GPU (SwiftShader has no OS backend to reformat meaningfully)."""
    import json

    persona = Persona.sample(os="windows", seed=2)
    browser = await zd.start(
        headless=headless, persona=persona, browser_args=["--use-angle=vulkan"]
    )
    try:
        tab = await browser.get("about:blank")
        d = json.loads(await tab.evaluate(_WEBGL_PROBE))
        if not d["renderer"] or "SwiftShader" in d["renderer"]:
            pytest.skip("real GPU backend unavailable on this host")
        assert "Vulkan" not in d["renderer"]
        assert "OpenGL" not in d["renderer"]
        assert "D3D11" in d["renderer"] or "Direct3D11" in d["renderer"]
        assert d["maxTex"] >= 16384  # real GPU caps preserved
    finally:
        await browser.stop()


@pytest.mark.integration
async def test_screen_not_headless_default(headless: bool) -> None:
    """screen.* must reflect the persona, not the 800x600 headless default."""
    import json

    persona = Persona.sample(os="windows", seed=88)
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("https://example.com")
        await tab.wait(0.6)
        d = json.loads(
            await tab.evaluate(
                "JSON.stringify({w:screen.width,h:screen.height,dpr:window.devicePixelRatio})"
            )
        )
        assert (d["w"], d["h"]) != (800, 600)  # not the headless default
        assert d["w"] >= 1280 and d["h"] >= 720  # a plausible desktop screen
    finally:
        await browser.stop()


_WORKER_LEAK_JS = """
(async function(){
  function snap(){return {platform:navigator.platform,hc:navigator.hardwareConcurrency,dm:navigator.deviceMemory};}
  var main = snap();
  var src = "self.onmessage=()=>postMessage(JSON.stringify("
          + "{platform:navigator.platform,hc:navigator.hardwareConcurrency,dm:navigator.deviceMemory}"
          + "));";
  var w = new Worker(URL.createObjectURL(new Blob([src],{type:'application/javascript'})));
  var worker = await new Promise(r=>{w.onmessage=e=>r(JSON.parse(e.data));w.postMessage(1);});
  return JSON.stringify({main: main, worker: worker});
})()
"""


@pytest.mark.integration
async def test_worker_scope_does_not_leak(headless: bool) -> None:
    """A dedicated worker's navigator must match the main thread — not leak the
    real host platform / cores / memory."""
    import json

    persona = Persona.sample(os="windows", seed=77)
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("https://example.com")
        await tab.wait(0.8)
        d = json.loads(await tab.evaluate(_WORKER_LEAK_JS, await_promise=True))
        assert d["worker"]["platform"] == d["main"]["platform"] == "Win32"
        assert d["worker"]["hc"] == d["main"]["hc"]
        assert d["worker"]["dm"] == d["main"]["dm"]
        assert d["worker"]["platform"] != "Linux x86_64"  # the real host
    finally:
        await browser.stop()


_UACH_JS = """
(async function(){
  var uad = navigator.userAgentData;
  var r = {hasUAD: !!uad, navPlatform: navigator.platform};
  if (uad) {
    r.native = (uad instanceof NavigatorUAData);
    r.platform = uad.platform;
    r.brands = uad.brands;
    var h = await uad.getHighEntropyValues(["platform","platformVersion","architecture"]);
    r.highPlatform = h.platform;
    r.highVersion = h.platformVersion;
  }
  r.ua = navigator.userAgent;
  return JSON.stringify(r);
})()
"""


@pytest.mark.integration
async def test_cdp_uach_native_and_coherent(headless: bool) -> None:
    """Windows persona on any host: userAgentData is a native object whose platform
    and high-entropy values match the profile, and the UA string carries no
    Headless token (requires a real https origin for userAgentData)."""
    import json

    persona = Persona.sample(os="windows", seed=7)
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("https://example.com")
        await tab.wait(1.0)
        d = json.loads(await tab.evaluate(_UACH_JS, await_promise=True))
        assert d["hasUAD"] is True
        assert d["native"] is True  # CDP yields a real NavigatorUAData, not a JS fake
        assert d["platform"] == "Windows"
        assert d["highPlatform"] == "Windows"
        assert len(d["brands"]) >= 1  # UA-CH not wiped
        assert d["navPlatform"] == "Win32"  # JS layer agrees
        assert "Headless" not in d["ua"]
    finally:
        await browser.stop()


_RECT_JS = """
(function(){
  var d = document.createElement('div');
  d.style.cssText = 'width:123.4px;height:57px;position:absolute;left:11px;top:9px';
  document.body.appendChild(d);
  var a = d.getBoundingClientRect().width;
  var b = d.getBoundingClientRect().width;
  return JSON.stringify({a: a, b: b});
})()
"""


@pytest.mark.integration
async def test_client_rects_stable_within_session(headless: bool) -> None:
    """Two getBoundingClientRect() calls on the same element return the SAME value
    (real layout stability); the value differs across seeds."""
    import json

    p1 = Persona(client_rects=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(1))
    b1 = await zd.start(headless=headless, persona=p1)
    try:
        d1 = json.loads(await (await b1.get("about:blank")).evaluate(_RECT_JS))
    finally:
        await b1.stop()
    assert d1["a"] == d1["b"]  # stable within a session, not per-call jitter

    p2 = Persona(client_rects=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(9999))
    b2 = await zd.start(headless=headless, persona=p2)
    try:
        d2 = json.loads(await (await b2.get("about:blank")).evaluate(_RECT_JS))
    finally:
        await b2.stop()
    assert d1["a"] != d2["a"]  # differs across seeds


@pytest.mark.integration
async def test_os_font_emulation(headless: bool) -> None:
    """Windows persona: target Windows fonts read present, others absent, and
    document.fonts.check agrees with measureText."""
    import json

    persona = Persona.sample(os="windows", seed=2024)
    browser = await zd.start(headless=headless, persona=persona)
    try:
        tab = await browser.get("about:blank")
        d = json.loads(await tab.evaluate(_FONT_DETECT_JS, await_promise=True))
        assert d["segoe"] is True  # multi-word family now parses + reads present
        assert d["consolas"] is True
        assert d["arial"] is True  # Arial is a real Windows font (in the pool)
        assert d["nope"] is False  # nonexistent font stays absent
        assert d["checkSegoe"] is True  # check() agrees with measureText
        assert d["checkNope"] is False
        assert d["ffSegoe"] is True  # FontFace local() probe: target font "loads"
        assert d["ffLiberation"] is False  # Linux-only font hidden from FontFace
    finally:
        await browser.stop()
