"""Verify fingerprint spoofing patches are active and probe bot-detection sites.

Usage:
    uv run python examples/verify_stealth.py
    uv run python examples/verify_stealth.py --headless
    uv run python examples/verify_stealth.py --no-sites
"""

import argparse
import asyncio
import json

import zendriver as zd
from zendriver import Persona, Seed, Strategy, SurfaceCfg, WebglSpec
from zendriver import cdp

_OK = "\033[92m✓\033[0m"
_FAIL = "\033[91m✗\033[0m"
_INFO = "\033[94m·\033[0m"

_PERSONA = Persona(
    # WebGL: NATIVE — VALUE only patches getParameter metadata strings, not actual
    # GPU pixel output. Sites that cross-check metadata against render output
    # (webglHash) will detect the mismatch. Only use VALUE if the claimed
    # renderer matches the real GPU.
    webgl=WebglSpec(strategy=Strategy.NATIVE),
    canvas=SurfaceCfg(strategy=Strategy.SEEDED),
    audio=SurfaceCfg(strategy=Strategy.SEEDED),
    client_rects=SurfaceCfg(strategy=Strategy.SEEDED),
    seed=Seed.from_int(42),
)

# Evaluates key fingerprint signals and returns JSON.
_PROBE_JS = """
(async function() {
    const r = {};
    r.webdriver   = navigator.webdriver;
    r.chrome      = typeof window.chrome !== 'undefined';
    r.prng        = typeof __zdRng;
    r.platform    = navigator.platform;
    r.concurrency = navigator.hardwareConcurrency;
    // Verify __zdNative shield: patched APIs must report [native code] to defeat
    // fingerprinters like pixelscan fptc.min.js that test /native/.test(fn.toString()).
    r.toDataURLNative  = /native/.test(HTMLCanvasElement.prototype.toDataURL.toString());
    r.measureTextNative = /native/.test(CanvasRenderingContext2D.prototype.measureText.toString());
    r.getBCRNative = /native/.test(Element.prototype.getBoundingClientRect.toString());
    try {
        const gl  = document.createElement('canvas').getContext('webgl');
        const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
        r.webglVendor   = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)   : null;
        r.webglRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
    } catch(e) {}
    try {
        const c = document.createElement('canvas');
        c.width = 200; c.height = 50;
        const ctx = c.getContext('2d');
        ctx.fillStyle = '#f60'; ctx.fillRect(0, 0, 200, 50);
        ctx.fillStyle = '#069'; ctx.font = '15px Arial';
        ctx.fillText('zendriver stealth', 4, 17);
        r.canvas = c.toDataURL().slice(200, 350);
    } catch(e) {}
    return JSON.stringify(r);
})()
"""

# Per-site JS that extracts a readable verdict after the page settles.
_VERDICTS: dict[str, str] = {
    "browserscan.net": "(function(){var e=document.querySelector('strong._1ikblmd');return e?e.innerText.trim():'';})() ",
    "nowsecure.nl": "(function(){var m=document.body.innerText.match(/(verified|not a bot|bot detected|human|passed|NOWSECURE)/i);return m?m[0]:'';})() ",
    "pixelscan.net": "(function(){var e=document.querySelector('[class*=\"result\"],[class*=\"score\"],h2');return e?e.innerText.trim().slice(0,80):'';})() ",
    "areyouheadless": "(function(){var e=document.querySelector('h2,p strong');return e?e.innerText.trim().slice(0,80):'';})() ",
}


def _verdict_js(url: str) -> str:
    return next((js for key, js in _VERDICTS.items() if key in url), "").strip()


async def _check_patches(headless: bool) -> None:
    """Probe about:blank with two seeds — verifies patches injected and canvas noise varies."""
    print("\n\033[1mPatch check\033[0m")

    browser_a = await zd.start(headless=headless, persona=_PERSONA)
    browser_b = await zd.start(
        headless=headless,
        persona=Persona(
            canvas=SurfaceCfg(strategy=Strategy.SEEDED), seed=Seed.from_int(9999)
        ),
    )
    tab_a = await browser_a.get("about:blank")
    tab_b = await browser_b.get("about:blank")
    await asyncio.gather(tab_a.wait(1), tab_b.wait(1))

    d: dict[str, object] = json.loads(
        await tab_a.evaluate(_PROBE_JS, await_promise=True)
    )
    canvas_b: str = json.loads(await tab_b.evaluate(_PROBE_JS, await_promise=True)).get(
        "canvas", ""
    )

    await asyncio.gather(browser_a.stop(), browser_b.stop())

    wd = d.get("webdriver")
    print(f"  {_OK if wd is None else _FAIL} navigator.webdriver: {wd!r}")
    print(f"  {_OK if d.get('chrome') else _FAIL} window.chrome: {d.get('chrome')}")
    print(
        f"  {_OK if d.get('prng') == 'function' else _FAIL} __zdRng injected: {d.get('prng')!r}"
    )
    print(
        f"  {_OK} platform: {d.get('platform')!r}  concurrency: {d.get('concurrency')}"
    )
    print(
        f"  {_OK if d.get('webglVendor') else _FAIL} webgl vendor:    {d.get('webglVendor')!r}"
    )
    print(
        f"  {_OK if d.get('webglRenderer') else _FAIL} webgl renderer:  {d.get('webglRenderer')!r}"
    )
    # These three check the native-shield: patched APIs must appear native to
    # defeat /native/.test(fn.toString()) probes used by pixelscan and others.
    print(
        f"  {_OK if d.get('toDataURLNative') else _FAIL} toDataURL.toString() looks native: {d.get('toDataURLNative')}"
    )
    print(
        f"  {_OK if d.get('measureTextNative') else _FAIL} measureText.toString() looks native: {d.get('measureTextNative')}"
    )
    print(
        f"  {_OK if d.get('getBCRNative') else _FAIL} getBoundingClientRect.toString() looks native: {d.get('getBCRNative')}"
    )
    differs = d.get("canvas", "") != canvas_b
    print(f"  {_OK if differs else _FAIL} canvas noise differs across seeds: {differs}")


async def _click_turnstile(tab: zd.Tab) -> bool:
    """Click the Cloudflare Turnstile checkbox via CDP coordinate dispatch.

    WHY NOT tab.find("Verify you are human"):
    The CF Turnstile widget (sitekey 3x00000000000000000000FF = force-interactive)
    is rendered by jsd/main.js inside an about:blank iframe via document.write().
    That iframe's src="" means it's same-origin, but the widget text is written
    asynchronously — so the text never appears in the main frame's accessibility
    tree and tab.find() returns nothing.

    WHY NOT JS dispatchEvent:
    CF Turnstile ignores synthetic JS mouse events (isTrusted=false). Only a
    real browser-level input event counts as trusted.

    SOLUTION — CDP Input.dispatchMouseEvent:
    CDP events are injected at the OS/browser level (isTrusted=true). We grab
    the .cf-turnstile div's bounding rect and aim ~27px from the left edge
    (where the checkbox appears), vertically centered.
    """
    try:
        coords = await tab.evaluate("""
            (function(){
                var ct = document.querySelector('.cf-turnstile');
                if (!ct) return null;
                ct.scrollIntoView({block: 'center', behavior: 'instant'});
                var r = ct.getBoundingClientRect();
                return {x: r.left + 27, y: r.top + r.height / 2};
            })()
        """)
        if not coords:
            return False
        pos = json.loads(coords) if isinstance(coords, str) else coords
        x, y = float(pos["x"]), float(pos["y"])
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mousePressed",
                x=x,
                y=y,
                button=cdp.input_.MouseButton.LEFT,
                click_count=1,
            )
        )
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseReleased",
                x=x,
                y=y,
                button=cdp.input_.MouseButton.LEFT,
                click_count=1,
            )
        )
        return True
    except Exception:
        return False


async def _probe_sites(headless: bool) -> None:
    """Visit each bot-detection site, screenshot, and print verdict."""
    print("\n\033[1mSite probes\033[0m")

    # (url, screenshot, initial_wait, click, result_wait)
    # click: "turnstile" → _click_turnstile(), any other string → tab.find(text)
    sites = [
        ("https://bot.sannysoft.com", "sannysoft.png", 4, None, 0),
        ("https://www.browserscan.net/bot-detection", "browserscan.png", 6, None, 0),
        # Score shows "..." — abs.incolumitas.com/lib.js (scoring backend) returns 502.
        # When server is back: complete the Bot Challenge to trigger behavioral scoring.
        # Flow: fill #formStuff (userName≠"not_a_bot", eMail, cookies dropdown, cat radio)
        # → POST to abs.incolumitas.com/mockData populates #tableStuff tbody → botQuestion()
        # fires a confirm() dialog (pre-override window.confirm=()=>true) → click
        # #updatePrice0 and #updatePrice1, wait for data-last-update attr → score updates.
        # Read results: JSON.parse(document.getElementById('new-tests').textContent).
        # ("https://bot.incolumitas.com", "incolumitas.png", 5, None, 15),
        (
            "https://arh.antoinevastel.com/bots/areyouheadless",
            "areyouheadless.png",
            5,
            None,
            0,
        ),
        ("https://pixelscan.net", "pixelscan.png", 3, "Scan My Browser Now", 14),
        # Sitekey 3x00000000000000000000FF = "force-interactive" (always shows checkbox).
        # Widget is painted by jsd/main.js via document.write() inside an about:blank
        # iframe — text never lands in main DOM so tab.find() can't locate it.
        # _click_turnstile() uses CDP Input.dispatchMouseEvent (isTrusted=true) aimed
        # at the .cf-turnstile BCR left+27px to hit the checkbox. JS dispatchEvent
        # (isTrusted=false) is ignored by CF. Result: "Success!" / "NOWSECURE BY NODRIVER".
        ("https://nowsecure.nl", "nowsecure.png", 5, "turnstile", 10),
        ("https://abrahamjuliot.github.io/creepjs/", "creepjs.png", 0, None, 15),
    ]

    for url, fname, wait, click, result_wait in sites:
        print(f"\n  {_INFO} {url}")
        browser = await zd.start(headless=headless, persona=_PERSONA)
        try:
            tab = await browser.get(url)
            if wait:
                await tab.wait(wait)

            if click == "turnstile":
                ok = await _click_turnstile(tab)
                print(f"    → turnstile: {'ok' if ok else 'not found'}")
            elif click:
                try:
                    el = await tab.find(click, best_match=True)
                    if el:
                        await el.click()
                        print(f"    → clicked: {click!r}")
                except Exception as e:
                    print(f"    → click failed: {e}")

            if result_wait:
                await tab.wait(result_wait)

            await tab.save_screenshot(fname)
            print(f"    → {fname}")

            js = _verdict_js(url)
            if js:
                try:
                    verdict = await tab.evaluate(js)
                    print(
                        f"    {_OK if verdict else ''} result: {verdict!r}"
                        if verdict
                        else "    (no verdict)"
                    )
                except Exception:
                    pass
        finally:
            await browser.stop()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify zendriver stealth patches")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-sites", action="store_true", help="skip site screenshots")
    args = parser.parse_args()

    await _check_patches(args.headless)
    if not args.no_sites:
        await _probe_sites(args.headless)
    print("\n\033[1mDone.\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
