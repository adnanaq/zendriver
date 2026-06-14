"""Verify fingerprint spoofing patches are active and probe bot-detection sites.

Usage:
    uv run python examples/verify_stealth.py
    uv run python examples/verify_stealth.py --os macos      # windows|macos|linux
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


def _persona(os_name: str, seed: int = 42) -> Persona:
    """Coherent persona for a target OS.

    WebGL stays NATIVE: VALUE only rewrites getParameter metadata strings, not the
    actual GPU pixel output, so sites cross-checking metadata against render output
    (webglHash) detect the mismatch. With NATIVE, the real GPU passes through and —
    when the target OS differs from the host — only the backend wording is reformatted
    (Vulkan/OpenGL -> Direct3D11/Metal) to match the claimed OS.
    """
    p = Persona.sample(os=os_name, seed=seed)
    p.webgl = WebglSpec(strategy=Strategy.NATIVE)
    p.canvas = SurfaceCfg(strategy=Strategy.SEEDED)
    p.audio = SurfaceCfg(strategy=Strategy.SEEDED)
    # NATIVE: rect noise reconstructs DOMRects from floats, breaking the exact
    # arithmetic identities + fixed rotation hash a real browser guarantees — which
    # fingerprinters flag as a lie. Real browsers return consistent rects.
    p.client_rects = SurfaceCfg(strategy=Strategy.NATIVE)
    p.seed = Seed.from_int(seed)
    return p


# Evaluates key fingerprint signals + a full lie-vector audit and returns JSON.
# The audit mirrors what modern fingerprinters (e.g. CreepJS) test on each patched
# API: a native method/getter has NO own `prototype`, reports "[native code]" via
# toString, carries the correct name, and (for getters) throws a TypeError when
# invoked on the wrong receiver ("illegal invocation"). Any deviation is a tell.
_PROBE_JS = """
(async function() {
    const r = {};
    r.webdriver   = navigator.webdriver;
    r.chrome      = typeof window.chrome !== 'undefined';
    r.platform    = navigator.platform;
    r.concurrency = navigator.hardwareConcurrency;
    r.deviceMemory = navigator.deviceMemory;
    r.languages   = navigator.languages;
    r.ua          = navigator.userAgent;
    r.uaDataPlatform = navigator.userAgentData ? navigator.userAgentData.platform : null;
    r.screen      = [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth];
    r.dpr         = window.devicePixelRatio;
    r.isSecure    = window.isSecureContext;
    try {
        const gl  = document.createElement('canvas').getContext('webgl2')
                  || document.createElement('canvas').getContext('webgl');
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

    // ---- lie-vector audit ----
    const nativeRe = /\\[native code\\]/;
    function illegalThrows(fn) {
        try { fn(); return false; } catch(e) { return e && e.constructor.name === 'TypeError'; }
    }
    function auditMethod(obj, key) {
        const fn = obj && obj[key];
        if (typeof fn !== 'function') return { missing: true };
        return {
            protoIn: ('prototype' in fn),
            native:  nativeRe.test(Function.prototype.toString.call(fn)),
            name:    fn.name,
            len:     fn.length,
        };
    }
    function auditGetter(proto, key) {
        const d = proto && Object.getOwnPropertyDescriptor(proto, key);
        if (!d || !d.get) return { missing: true };
        return {
            protoIn: ('prototype' in d.get),
            native:  nativeRe.test(d.get.toString()),
            name:    d.get.name,
            illegalThrows: illegalThrows(function(){ return proto[key]; }),
        };
    }
    const WGL = window.WebGL2RenderingContext && WebGL2RenderingContext.prototype;
    const RTF = window.Intl && Intl.RelativeTimeFormat && Intl.RelativeTimeFormat.prototype;
    r.audit = {
        methods: {
            toDataURL:             auditMethod(HTMLCanvasElement.prototype, 'toDataURL'),
            getImageData:          auditMethod(CanvasRenderingContext2D.prototype, 'getImageData'),
            measureText:           auditMethod(CanvasRenderingContext2D.prototype, 'measureText'),
            getBoundingClientRect: auditMethod(Element.prototype, 'getBoundingClientRect'),
            getParameter:          auditMethod(WGL, 'getParameter'),
            permissionsQuery:      auditMethod(window.Permissions && Permissions.prototype, 'query'),
            // Timezone is spoofed via CDP setTimezoneOverride (browser-level), NOT a
            // JS patch — so these stay native. Fingerprinters (e.g. CreepJS) key the
            // timezone "lie" precisely on tampering of these three; if they read
            // native here, the spoofed zone is not detectable as a lie.
            getTimezoneOffset:   auditMethod(Date.prototype, 'getTimezoneOffset'),
            dtfResolvedOptions:  auditMethod(Intl.DateTimeFormat.prototype, 'resolvedOptions'),
            rtfResolvedOptions:  auditMethod(RTF, 'resolvedOptions'),
        },
        getters: {
            platform:            auditGetter(Navigator.prototype, 'platform'),
            hardwareConcurrency: auditGetter(Navigator.prototype, 'hardwareConcurrency'),
            languages:           auditGetter(Navigator.prototype, 'languages'),
            webdriver:           auditGetter(Navigator.prototype, 'webdriver'),
        },
        fpToStringProtoIn: ('prototype' in Function.prototype.toString),
        fpToStringNative:  nativeRe.test(Function.prototype.toString.toString()),
    };
    // Timezone internal consistency: Intl zone, Date offset, and the DST-aware
    // offset for a fixed instant must all agree (a partial spoof would diverge).
    const jan = new Date('2026-01-15T12:00:00Z').getTimezoneOffset();
    const jul = new Date('2026-07-15T12:00:00Z').getTimezoneOffset();
    r.timezone = {
        intl: Intl.DateTimeFormat().resolvedOptions().timeZone,
        offsetNow: new Date().getTimezoneOffset(),
        janOffset: jan,
        julOffset: jul,
        observesDst: jan !== jul,
    };
    return JSON.stringify(r);
})()
"""

# Worker-scope probe. The page bootstrap never runs in a worker, so workers are
# patched separately; this confirms the worker's navigator is spoofed AND that the
# worker-scope shield closes the same lie vectors (no prototype, native toString,
# illegal-invocation TypeError).
_WORKER_PROBE_JS = r"""
(function(){
  var src = `
    function illegalThrows(fn){ try{ fn(); return false; }catch(e){ return e && e.constructor.name==='TypeError'; } }
    var p = Object.getPrototypeOf(navigator);
    var d = Object.getOwnPropertyDescriptor(p,'platform');
    postMessage(JSON.stringify({
      platform: navigator.platform,
      concurrency: navigator.hardwareConcurrency,
      getter_protoIn: d&&d.get?('prototype' in d.get):'n/a',
      getter_name: d&&d.get?d.get.name:'n/a',
      getter_native: d&&d.get?/\\[native code\\]/.test(d.get.toString()):'n/a',
      getter_illegalThrows: d&&d.get?illegalThrows(function(){ return p.platform; }):'n/a',
      fpToString_protoIn: ('prototype' in Function.prototype.toString),
    }));
  `;
  return new Promise(function(resolve){
    try {
      var w = new Worker(URL.createObjectURL(new Blob([src],{type:'application/javascript'})));
      w.onmessage = function(e){ resolve(e.data); };
      setTimeout(function(){ resolve('TIMEOUT'); }, 5000);
    } catch(e) { resolve('ERR:'+e); }
  });
})()
"""

# Per-site result extractors. Each returns JSON {overall, tests:[{name,pass,detail}]}
# scraped from the page's own reported results, so the output shows what each
# detector actually tested and whether each check passed — not just "did we load
# the page". `pass` reflects the site's verdict for that row (green/Normal/etc.).

# sannysoft: a results table where each scored row's last cell has class
# "...passed" / "...failed" (the rows below it are raw data dumps, no class).
_EXTRACT_SANNYSOFT = r"""
(function(){
  var tests=[];
  document.querySelectorAll('table tr').forEach(function(tr){
    var tds=tr.querySelectorAll('td'); if(tds.length<2) return;
    var rc=tds[tds.length-1], cls=(rc.className||'')+'';
    if(/passed|failed/.test(cls)){
      tests.push({name:(tds[0].textContent||'').replace(/\s+/g,' ').trim(),
        pass:/passed/.test(cls)&&!/failed/.test(cls),
        detail:(rc.textContent||'').replace(/\s+/g,' ').trim().slice(0,48)});
    }
  });
  var failed=tests.filter(function(t){return !t.pass;}).length;
  return JSON.stringify({overall:tests.length?(failed?'fail':'pass'):'unknown', tests:tests});
})()
"""

# areyouheadless: single verdict ("You are not Chrome headless" => pass).
_EXTRACT_AREYOUHEADLESS = r"""
(function(){
  var s=(document.body.innerText||'');
  var notHl=/not chrome headless/i.test(s);
  var hl=/you are chrome headless/i.test(s)&&!notHl;
  return JSON.stringify({overall:notHl?'pass':(hl?'fail':'unknown'),
    tests:[{name:'Chrome headless test', pass:notHl,
      detail:notHl?'not headless':(hl?'HEADLESS DETECTED':'unknown')}]});
})()
"""

# browserscan: results are computed client-side (fingerprintjs-V5) and rendered
# into the DOM — there is NO results API/global (the only POST is scroll/pageview
# analytics). All tabs render into the DOM once scrolled into view (no clicking
# needed). Each row is a div/li/tr with exactly two text children (label, value).
# An exact status word ("Normal"/"Detected") => a pass/fail bot test; anything else
# => a collected fingerprint value (informational, shown so you can see what it
# read). Caller must scroll to the bottom before running this.
_EXTRACT_BROWSERSCAN = r"""
(function(){
  function tc(el){return (el.textContent||'').replace(/\s+/g,' ').trim();}
  var STATUS=/^(Normal|Detected|Abnormal|Failed|Fail)$/i, FAIL=/^(Detected|Abnormal|Failed|Fail)$/i;
  var seen={}, tests=[], values=[];
  document.querySelectorAll('div,li,tr').forEach(function(el){
    var t=tc(el); if(!t||t.length>90) return;
    var kids=Array.prototype.filter.call(el.children,function(c){return tc(c).length>0;});
    if(kids.length!==2) return;
    var a=tc(kids[0]), b=tc(kids[1]);
    if(!a||!b||a===b||a.length>40||b.length>50||a.slice(-1)===':') return;
    if(/Normal|Detected/.test(b) && !STATUS.test(b)) return;   // skip merged-junk rows
    var key=a+'|'+b; if(seen[key]) return; seen[key]=1;
    if(STATUS.test(b)){ tests.push({name:a, pass:!FAIL.test(b), detail:b}); return; }
    // collected fingerprint data: keep meaningful scalar fields only. Drop native
    // method dumps, [object X] sub-object placeholders, inherited Object.prototype
    // members, and page chrome — they are noise, not signals browserscan reports.
    var JUNK=/^(constructor|hasOwnProperty|isPrototypeOf|propertyIsEnumerable|toString|valueOf|toLocaleString|__(define|lookup)(Getter|Setter)__)$/;
    if(/^function\b/.test(b) || /\[object /.test(b) || JUNK.test(a) || /release notes/i.test(a)) return;
    values.push({name:a, detail:b});
  });
  var failed=tests.filter(function(x){return !x.pass;}).length;
  return JSON.stringify({overall:tests.length?(failed?'fail':'pass'):'unknown',
    tests:tests, values:values});
})()
"""

# creepjs: read the FULL computed result from window.Fingerprint (CreepJS exposes
# everything there — no DOM scraping). Every headless/likeHeadless/stealth boolean
# is a test (true = a detected tell => FAIL); every section carries a `.lied` flag
# (lied => FAIL); the per-API lie detail and ratings are shown as values. Caller
# must wait until the fingerprint has computed (~30s).
_EXTRACT_CREEPJS = r"""
(function(){
  var F=window.Fingerprint;
  if(!F) return JSON.stringify({overall:'unknown', tests:[],
    values:[{name:'error', detail:'window.Fingerprint not ready (wait longer)'}]});
  var tests=[], values=[];
  function grp(o,p){ if(!o) return; Object.keys(o).forEach(function(k){
    var v=o[k]; if(typeof v==='boolean') tests.push({name:p+k, pass:v===false, detail:String(v)}); }); }
  var h=F.headless||{};
  grp(h.likeHeadless,'likeHeadless.'); grp(h.headless,'headless.'); grp(h.stealth,'stealth.');
  Object.keys(F).forEach(function(sec){ var s=F[sec];
    if(s && typeof s==='object' && ('lied' in s))
      tests.push({name:sec+'.lied', pass:!s.lied, detail:s.lied?'LIED':'ok'}); });
  var lieData=(F.lies&&F.lies.data)||{};
  Object.keys(lieData).forEach(function(api){
    try{ values.push({name:'lie: '+api, detail:JSON.stringify(lieData[api]).slice(0,90)}); }catch(e){} });
  values.push({name:'likeHeadlessRating', detail:(h.likeHeadlessRating!=null?h.likeHeadlessRating:'?')+'%'});
  values.push({name:'headlessRating', detail:(h.headlessRating!=null?h.headlessRating:'?')+'%'});
  if(F.lies) values.push({name:'totalLies', detail:String(F.lies.totalLies)});
  var failed=tests.filter(function(t){return !t.pass;}).length;
  return JSON.stringify({overall:failed?'fail':'pass', tests:tests, values:values});
})()
"""

# pixelscan / nowsecure: single overall verdict (SPA / Cloudflare). Best-effort.
_EXTRACT_PIXELSCAN = r"""
(function(){
  var s=(document.body.innerText||'').replace(/\s+/g,' ');
  var bad=/(bot|automation|detected|inconsistent|masked)/i.test(s);
  var good=/(you (do not|don't) look like a bot|no automation|consistent|not a bot)/i.test(s);
  var m=s.match(/[^.]*\b(bot|consistent|inconsistent|masked|automation)\b[^.]*/i);
  return JSON.stringify({overall:good&&!bad?'pass':(bad?'fail':'unknown'),
    tests:[{name:'pixelscan verdict', pass:good&&!bad, detail:(m?m[0].trim().slice(0,70):'?')}]});
})()
"""

_EXTRACT_NOWSECURE = r"""
(function(){
  var s=(document.body.innerText||'').replace(/\s+/g,' ');
  var ok=/(you are not a bot|not a bot|success|nowsecure)/i.test(s);
  var bad=/(you are a bot|bot detected|verify you are human|checking your browser)/i.test(s);
  return JSON.stringify({overall:ok&&!bad?'pass':(bad?'fail':'unknown'),
    tests:[{name:'Cloudflare / bot verdict', pass:ok&&!bad,
      detail:(s.match(/(you are[^.]{0,40}|success[^.]{0,20})/i)||['?'])[0].trim().slice(0,60)}]});
})()
"""


def _flag(ok: bool) -> str:
    return _OK if ok else _FAIL


def _print_audit(audit: dict) -> None:
    """Print the lie-vector audit. A native API has no own prototype, reports
    [native code], keeps its name, and (getters) throws TypeError on illegal use."""
    print("  lie audit (must look native — no prototype, [native code], correct name):")
    for name, m in audit.get("methods", {}).items():
        if m.get("missing"):
            print(f"    {_INFO} method {name}: not present")
            continue
        ok = (not m["protoIn"]) and m["native"]
        print(
            f"    {_flag(ok)} method {name}: protoIn={m['protoIn']} native={m['native']} "
            f"name={m['name']!r} len={m['len']}"
        )
    for name, g in audit.get("getters", {}).items():
        if g.get("missing"):
            print(f"    {_INFO} getter {name}: not present")
            continue
        ok = (not g["protoIn"]) and g["native"] and g["illegalThrows"]
        print(
            f"    {_flag(ok)} getter {name}: protoIn={g['protoIn']} native={g['native']} "
            f"name={g['name']!r} illegalThrows={g['illegalThrows']}"
        )
    fp_ok = (not audit["fpToStringProtoIn"]) and audit["fpToStringNative"]
    print(
        f"    {_flag(fp_ok)} Function.prototype.toString: protoIn={audit['fpToStringProtoIn']} "
        f"native={audit['fpToStringNative']}"
    )


async def _check_patches(headless: bool, os_name: str) -> None:
    """Probe about:blank — verify signals, the lie-vector audit, the worker scope,
    and (across two seeds) that canvas noise varies."""
    print(f"\n\033[1mPatch check (os={os_name})\033[0m")

    browser_a = await zd.start(headless=headless, persona=_persona(os_name, 42))
    browser_b = await zd.start(headless=headless, persona=_persona(os_name, 9999))
    tab_a = await browser_a.get("about:blank")
    tab_b = await browser_b.get("about:blank")
    await asyncio.gather(tab_a.wait(1), tab_b.wait(1))

    d: dict = json.loads(await tab_a.evaluate(_PROBE_JS, await_promise=True))
    worker_raw = await tab_a.evaluate(_WORKER_PROBE_JS, await_promise=True)
    canvas_b: str = json.loads(
        await tab_b.evaluate(_PROBE_JS, await_promise=True)
    ).get("canvas", "")

    await asyncio.gather(browser_a.stop(), browser_b.stop())

    wd = d.get("webdriver")
    print(f"  {_flag(wd is False)} navigator.webdriver: {wd!r}  (expect False, not None)")
    print(f"  {_flag(bool(d.get('chrome')))} window.chrome: {d.get('chrome')}")
    print(f"  {_INFO} userAgent: {d.get('ua')!r}")
    print(
        f"  {_INFO} platform: {d.get('platform')!r}  uaData.platform: {d.get('uaDataPlatform')!r}  "
        f"concurrency: {d.get('concurrency')}  deviceMemory: {d.get('deviceMemory')}"
    )
    print(f"  {_INFO} languages: {d.get('languages')}  screen: {d.get('screen')}  dpr: {d.get('dpr')}")
    print(f"  {_flag(bool(d.get('webglVendor')))} webgl vendor:   {d.get('webglVendor')!r}")
    print(f"  {_flag(bool(d.get('webglRenderer')))} webgl renderer: {d.get('webglRenderer')!r}")

    _print_audit(d.get("audit", {}))

    tz = d.get("timezone", {})
    if tz:
        print(
            f"  {_INFO} timezone: {tz.get('intl')!r}  offsetNow={tz.get('offsetNow')}  "
            f"jan={tz.get('janOffset')} jul={tz.get('julOffset')} observesDST={tz.get('observesDst')}"
        )

    print("  worker scope (patched separately from the page):")
    try:
        w = json.loads(worker_raw) if isinstance(worker_raw, str) else worker_raw
        w_ok = (
            w.get("getter_protoIn") is False
            and w.get("getter_native") is True
            and w.get("getter_illegalThrows") is True
            and w.get("fpToString_protoIn") is False
        )
        print(
            f"    {_flag(w_ok)} platform={w.get('platform')!r} concurrency={w.get('concurrency')} "
            f"protoIn={w.get('getter_protoIn')} native={w.get('getter_native')} "
            f"name={w.get('getter_name')!r} illegalThrows={w.get('getter_illegalThrows')}"
        )
    except Exception:
        print(f"    {_FAIL} worker probe failed: {worker_raw!r}")

    differs = d.get("canvas", "") != canvas_b
    print(f"  {_flag(differs)} canvas noise differs across seeds: {differs}")


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


async def _scroll_to_bottom(tab: zd.Tab, steps: int = 4) -> None:
    """Scroll down in steps so lazy-rendered sections mount into the DOM."""
    for _ in range(steps):
        await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await tab.wait(1.2)


def _print_site_result(data: dict) -> None:
    """Print a site's per-test pass/fail breakdown + any collected values."""
    overall = data.get("overall", "unknown")
    badge = _OK if overall == "pass" else (_FAIL if overall == "fail" else _INFO)
    tests = data.get("tests", [])
    values = data.get("values", [])
    npass = sum(1 for t in tests if t.get("pass"))
    print(f"    {badge} overall: {overall.upper()}  ({npass}/{len(tests)} checks passed)")
    for t in tests:
        print(f"      {_flag(bool(t.get('pass')))} {t.get('name')}: {t.get('detail')}")
    if values:
        print(f"    {_INFO} {len(values)} fingerprint values collected:")
        for v in values:
            print(f"        · {v.get('name')}: {v.get('detail')}")


async def _probe_sites(headless: bool, os_name: str) -> None:
    """Visit each bot-detection site and print its own per-test pass/fail results.

    No screenshots — each site's verdict is read from its reported results so you
    can see exactly what was tested and what passed/failed.
    """
    print(f"\n\033[1mSite probes (os={os_name})\033[0m")

    # (url, extractor, initial_wait, action, result_wait)
    # action: None | "scroll" | "turnstile" | "<text to click>"
    sites = [
        ("https://bot.sannysoft.com", _EXTRACT_SANNYSOFT, 5, None, 0),
        ("https://www.browserscan.net/bot-detection", _EXTRACT_BROWSERSCAN, 7, "scroll", 0),
        ("https://arh.antoinevastel.com/bots/areyouheadless", _EXTRACT_AREYOUHEADLESS, 5, None, 0),
        ("https://pixelscan.net", _EXTRACT_PIXELSCAN, 3, "Scan My Browser Now", 14),
        # Cloudflare Turnstile (force-interactive). _click_turnstile uses a trusted
        # CDP mouse event; the widget text never reaches the main DOM for tab.find().
        ("https://nowsecure.nl", _EXTRACT_NOWSECURE, 5, "turnstile", 10),
        ("https://abrahamjuliot.github.io/creepjs/", _EXTRACT_CREEPJS, 33, None, 0),
    ]

    for url, extractor, wait, action, result_wait in sites:
        print(f"\n  {_INFO} {url}")
        browser = await zd.start(headless=headless, persona=_persona(os_name, 42))
        try:
            tab = await browser.get(url)
            if wait:
                await tab.wait(wait)

            if action == "scroll":
                await _scroll_to_bottom(tab)
            elif action == "turnstile":
                ok = await _click_turnstile(tab)
                print(f"    → turnstile: {'clicked' if ok else 'not found'}")
            elif action:
                try:
                    el = await tab.find(action, best_match=True)
                    if el:
                        await el.click()
                        print(f"    → clicked: {action!r}")
                except Exception as e:
                    print(f"    → click failed: {e}")

            if result_wait:
                await tab.wait(result_wait)

            try:
                data = json.loads(await tab.evaluate(extractor))
                _print_site_result(data)
            except Exception as e:
                print(f"    {_FAIL} could not extract results: {e!r}")
        finally:
            await browser.stop()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify zendriver stealth patches")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-sites", action="store_true", help="skip live site probes")
    parser.add_argument(
        "--os",
        choices=("windows", "macos", "linux"),
        default="windows",
        help="target OS to impersonate (default: windows)",
    )
    args = parser.parse_args()

    await _check_patches(args.headless, args.os)
    if not args.no_sites:
        await _probe_sites(args.headless, args.os)
    print("\n\033[1mDone.\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
