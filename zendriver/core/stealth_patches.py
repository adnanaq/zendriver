"""Bootstrap script assembly for fingerprint spoofing.

Combines identity patches (parameterized by Fingerprint) with per-surface
farbling patches (driven by Persona strategy config) into a single
Page.addScriptToEvaluateOnNewDocument payload.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .stealth import (
    FontSpec,
    HardwareSpec,
    Persona,
    Platform,
    Seed,
    Strategy,
    Surface,
    SurfaceCfg,
    WebglSpec,
    WebrtcSpec,
)

_OS_BY_PLATFORM = {
    Platform.WIN32: "windows",
    Platform.MAC_INTEL: "macos",
    Platform.LINUX_X86_64: "linux",
}

if TYPE_CHECKING:
    from .fingerprints.profile import ResolvedProfile

# ---------------------------------------------------------------------------
# Native-code toString facade — must run first, before any patching.
#
# WHY: Many bot-detection fingerprinters (e.g. pixelscan fptc.min.js) check
# whether browser APIs have been replaced by testing:
#   /native/i.test(HTMLCanvasElement.prototype.toDataURL.toString())
# A patched function returns its JS source → detection.
#
# FIX: Override Function.prototype.toString. Patched functions are registered
# in a WeakSet via window.__zdNative(fn). For registered functions the override
# returns the standard "[native code]" string; all other functions fall through
# to the original toString. The override itself is also registered so that
# toString.toString() also looks native.
#
# CROSS-REALM: the registry WeakSet is stored on the TOP window under a shared
# Symbol.for key, so every same-origin realm (the page + each iframe, which all
# run this bootstrap) shares ONE registry. Without this, a fingerprinter calls a
# fresh iframe's pristine Function.prototype.toString on a MAIN-realm patched
# function — the iframe's per-realm WeakSet wouldn't contain it, leaking the real
# source. (Cross-origin iframes can't reach window.top and fall back to a local
# registry — an inherent limit, but they also can't read main-realm functions.)
# ---------------------------------------------------------------------------
_NATIVE_SHIELD = """\
(function(){
  var TOP; try{ TOP=window.top; if(TOP===null) TOP=window; }catch(e){ TOP=window; }
  var KEY=Symbol.for('zd.fns');
  var _fns=TOP[KEY]; if(!_fns){ try{ _fns=TOP[KEY]=new WeakSet(); }catch(e){ _fns=new WeakSet(); } }
  const _orig=Function.prototype.toString;
  Object.defineProperty(Function.prototype,'toString',{
    value:function toString(){
      return _fns.has(this)?'function '+(this.name||'')+'() { [native code] }':_orig.call(this);
    },
    writable:true,configurable:true,enumerable:false,
  });
  _fns.add(Function.prototype.toString);
  window.__zdNative=function zdNative(fn){_fns.add(fn);return fn;};
  __zdNative(window.__zdNative);
})();"""

# ---------------------------------------------------------------------------
# Mulberry32 PRNG — shared by all noise-surface patches.
# ---------------------------------------------------------------------------
_PRNG = """\
function __zdRng(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}"""

# ---------------------------------------------------------------------------
# Surface farbling patches (ported from zendriver-rs, Apache-2.0).
# Token substitution: SEED → u32 integer | strategy expression.
# All prototype replacements are wrapped with __zdNative() so that
# fn.toString() returns "[native code]" instead of the JS source.
# ---------------------------------------------------------------------------
_CANVAS = """\
(function (seed) {
  const seed32 = seed >>> 0;
  function farble(data, width) {
    let s = seed32;
    for (let i = 0; i < 16 && i < data.length; i++) s = (Math.imul(s, 33) ^ data[i]) >>> 0;
    const r = __zdRng(s);
    const n = (data.length / 4) | 0;
    for (let k = 0; k < 8; k++) {
      const px = (r() * n) | 0;
      const idx = px * 4;
      if (idx + 3 >= data.length) { r(); r(); r(); continue; }
      const rv = data[idx], gv = data[idx+1], bv = data[idx+2], av = data[idx+3];
      const col = px % width;
      let uniform = true;
      const up = idx - width * 4;
      if (uniform && up >= 0 && (data[up]!==rv||data[up+1]!==gv||data[up+2]!==bv||data[up+3]!==av)) uniform=false;
      const dn = idx + width * 4;
      if (uniform && dn+3<data.length && (data[dn]!==rv||data[dn+1]!==gv||data[dn+2]!==bv||data[dn+3]!==av)) uniform=false;
      if (uniform && col>0 && (data[idx-4]!==rv||data[idx-3]!==gv||data[idx-2]!==bv||data[idx-1]!==av)) uniform=false;
      if (uniform && col<width-1 && (data[idx+4]!==rv||data[idx+5]!==gv||data[idx+6]!==bv||data[idx+7]!==av)) uniform=false;
      if (uniform) { r(); r(); r(); continue; }
      data[idx]   = Math.max(0, Math.min(255, rv + (r() < 0.5 ? -1 : 1)));
      data[idx+1] = Math.max(0, Math.min(255, gv + (r() < 0.5 ? -1 : 1)));
      data[idx+2] = Math.max(0, Math.min(255, bv + (r() < 0.5 ? -1 : 1)));
    }
  }
  const origGet = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = __zdNative(function getImageData(...args) {
    const img = origGet.apply(this, args); farble(img.data, img.width); return img;
  });
  const origURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = __zdNative(function toDataURL(...args) {
    const ctx = this.getContext('2d');
    if (ctx && this.width > 0 && this.height > 0) {
      const orig = origGet.call(ctx, 0, 0, this.width, this.height);
      const copy = new ImageData(new Uint8ClampedArray(orig.data), this.width, this.height);
      farble(copy.data, this.width); ctx.putImageData(copy, 0, 0);
      const url = origURL.apply(this, args); ctx.putImageData(orig, 0, 0); return url;
    }
    return origURL.apply(this, args);
  });
})(SEED);"""

_AUDIO = """\
(function (seed) {
  if (typeof AnalyserNode === 'undefined') return;
  const rng = __zdRng(seed);
  const origFreq = AnalyserNode.prototype.getFloatFrequencyData;
  AnalyserNode.prototype.getFloatFrequencyData = __zdNative(function getFloatFrequencyData(a) {
    origFreq.call(this, a);
    for (let i = 0; i < a.length; i++) a[i] += (rng() - 0.5) * 1e-4;
  });
  const origTime = AnalyserNode.prototype.getByteTimeDomainData;
  if (origTime) {
    AnalyserNode.prototype.getByteTimeDomainData = __zdNative(function getByteTimeDomainData(a) {
      origTime.call(this, a);
      for (let i = 0; i < a.length; i++)
        a[i] = Math.max(0, Math.min(255, a[i] + (rng() < 0.5 ? -1 : 1)));
    });
  }
})(SEED);"""

_CLIENT_RECTS = """\
(function (seed) {
  const rng = __zdRng(seed);
  // A single per-session scale factor (drawn once, not per call). The same
  // element returns identical rects on every call within a session — matching a
  // real browser's layout stability — while the factor differs across seeds, so
  // the values can't be linked across sessions. Per-call jitter, by contrast,
  // is itself a tell: a real rect does not change between two reads.
  const factor = 1 + (rng() - 0.5) * 2e-5;
  function n(v) { return v * factor; }
  const origR = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = __zdNative(function getBoundingClientRect() {
    const r = origR.call(this);
    return new DOMRect(n(r.x), n(r.y), n(r.width), n(r.height));
  });
  const origRs = Element.prototype.getClientRects;
  Element.prototype.getClientRects = __zdNative(function getClientRects() {
    return Array.from(origRs.call(this)).map(r => new DOMRect(n(r.x), n(r.y), n(r.width), n(r.height)));
  });
})(SEED);"""

# Cross-OS WebGL renderer reformatting. When impersonating a different OS while
# keeping the real GPU (no fixed-string substitution), the renderer otherwise
# leaks the host's backend wording (e.g. "Vulkan …" on Linux) under a foreign UA.
# This rewrites only the backend wording to the target OS (Direct3D11 for Windows,
# Metal for macOS) while preserving the real vendor, GPU model, device id and
# capabilities (MAX_TEXTURE_SIZE etc.) — so it stays internally consistent for the
# real GPU's class. WEBGL_OS substituted with a JSON OS string.
_WEBGL_REFORMAT = """\
(function(targetOs){
  function reformat(real){
    if(!real) return real;
    var m = real.match(/^ANGLE \\(([^,]+), (.+), ([^,)]+)\\)$/);
    if(!m) return real;
    var vendor = m[1], backend = m[2], model = backend;
    var inner = backend.match(/^(?:Vulkan|OpenGL)[^(]*\\((.+)\\)$/);
    if(inner) model = inner[1];
    model = model.replace(/^(\\S+)\\s+\\1\\b/, '$1').replace(/\\bMesa\\s+/, '');
    if(targetOs === 'windows') return 'ANGLE (' + vendor + ', ' + model + ' Direct3D11 vs_5_0 ps_5_0, D3D11)';
    if(targetOs === 'macos')   return 'ANGLE (' + vendor + ', ANGLE Metal Renderer: ' + model + ', Unspecified Version)';
    return real;
  }
  function patch(p){
    if(!p) return;
    var o = p.getParameter;
    p.getParameter = __zdNative(function getParameter(q){
      if(q === 0x9246) return reformat(o.call(this, q));
      return o.call(this, q);
    });
  }
  if(window.WebGLRenderingContext) patch(WebGLRenderingContext.prototype);
  if(window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
})(WEBGL_OS);"""

# Only emitted when strategy is not NATIVE and vendor/renderer are provided.
# WEBGL_VENDOR / WEBGL_RENDERER substituted with JSON strings.
_WEBGL = """\
(function(vendor,renderer){
  function patch(p){const o=p.getParameter;p.getParameter=__zdNative(function getParameter(q){if(vendor&&q===0x9245)return vendor;if(renderer&&q===0x9246)return renderer;return o.call(this,q);});}
  if(window.WebGLRenderingContext)patch(WebGLRenderingContext.prototype);
  if(window.WebGL2RenderingContext)patch(WebGL2RenderingContext.prototype);
})(WEBGL_VENDOR,WEBGL_RENDERER);"""

# OS-font emulation.
# Font *presence* is detected by comparing measureText width of a string in a
# font against the fallback; differ => "present". We:
#   (a) parse the CSS font shorthand correctly — first family, strip quotes
#       (so multi-word families like "Segoe UI" are matched, not just "UI");
#   (b) give each target-OS font a deterministic per-font width delta (present);
#   (c) force every other *named* (non-generic) family to the fallback width
#       (hides host fonts that would otherwise leak);
#   (d) make document.fonts.check AGREE with measureText (true for the set).
_FONTS = """\
(function(allow,seed){
  if(!Array.isArray(allow)) return;
  const SET=new Set(allow);
  const GENERIC=new Set(['sans-serif','serif','monospace','cursive','fantasy',
    'system-ui','ui-sans-serif','ui-serif','ui-monospace','math','emoji',
    '-apple-system','blinkmacsystemfont','inherit','initial','unset','revert']);
  function fam(fontStr){
    const m=(fontStr||'').match(/(?:\\d*\\.?\\d+(?:px|pt|em|rem|%|ex|ch|vh|vw)\\s+)(.+)$/);
    let f=m?m[1]:(fontStr||'');
    return f.split(',')[0].trim().replace(/^["']|["']$/g,'');
  }
  function sizeOf(fontStr){
    const m=(fontStr||'').match(/^(.*?\\d*\\.?\\d+(?:px|pt|em|rem|%|ex|ch|vh|vw))/);
    return m?m[1]:'10px';
  }
  function h(s){let x=2166136261;for(let i=0;i<s.length;i++){x=Math.imul(x^s.charCodeAt(i),16777619)>>>0;}return x;}
  const orig=CanvasRenderingContext2D.prototype.measureText;
  CanvasRenderingContext2D.prototype.measureText=__zdNative(function measureText(t){
    const m=orig.call(this,t);
    const f=fam(this.font);
    if(!f||GENERIC.has(f.toLowerCase())) return m;
    if(SET.has(f)){
      const d=0.5+(h(f)%150)/100;
      try{Object.defineProperty(m,'width',{value:m.width+d,configurable:true});}catch(e){}
    } else {
      const saved=this.font;
      let fb=m.width;
      try{this.font=sizeOf(saved)+' sans-serif';fb=orig.call(this,t).width;}catch(e){}
      finally{this.font=saved;}
      try{Object.defineProperty(m,'width',{value:fb,configurable:true});}catch(e){}
    }
    return m;
  });
  if(document.fonts&&document.fonts.check){
    document.fonts.check=__zdNative(function check(font,text){
      return SET.has(fam(font));
    });
  }
  // FontFace presence probe: new FontFace(name,'local("name")').load() resolves
  // iff the font is installed. Shim the constructor so a local()-only probe
  // resolves for the target set and rejects otherwise. Real url() web fonts are
  // left untouched (their .load actually fetches), so site fonts keep working.
  if(typeof FontFace!=='undefined'){
    const RealFF=FontFace;
    function FontFaceShim(family,source,desc){
      const ff=new RealFF(family,source,desc);
      const localOnly = typeof source==='string' && /local\\(/.test(source) && !/url\\(/.test(source);
      if(localOnly){
        const f=fam(family);
        ff.load=__zdNative(function load(){
          if(SET.has(f)){ try{Object.defineProperty(ff,'status',{value:'loaded',configurable:true});}catch(e){} return Promise.resolve(ff); }
          return Promise.reject(new DOMException('A network error occurred.','NetworkError'));
        });
      }
      return ff;
    }
    FontFaceShim.prototype=RealFF.prototype;
    try{ self.FontFace=__zdNative(FontFaceShim); }catch(e){}
  }
})(FONT_ALLOW,SEED);"""

_HARDWARE = """\
(function(battery,mediaDevices,voices){
  if(typeof battery==='number'&&navigator.getBattery)
    navigator.getBattery=__zdNative(function getBattery(){return Promise.resolve({level:battery,charging:true,chargingTime:0,dischargingTime:Infinity,addEventListener(){},removeEventListener(){}});});
  if(typeof mediaDevices==='number'&&navigator.mediaDevices&&navigator.mediaDevices.enumerateDevices)
    navigator.mediaDevices.enumerateDevices=__zdNative(function enumerateDevices(){return Promise.resolve(Array.from({length:mediaDevices},(_,i)=>({deviceId:'dev'+i,kind:'audioinput',label:'',groupId:'g'+i})));});
  if(Array.isArray(voices)&&window.speechSynthesis)
    speechSynthesis.getVoices=__zdNative(function getVoices(){return voices.map(n=>({name:n,lang:'en-US',default:false,localService:true,voiceURI:n}));});
})(HW_BATTERY,HW_MEDIA_DEVICES,HW_VOICES);"""

_WEBRTC = """\
(function(policy,fakeIp){
  if(policy==='native')return;
  const RTC=window.RTCPeerConnection||window.webkitRTCPeerConnection;
  if(!RTC)return;
  window.RTCPeerConnection=__zdNative(function RTCPeerConnection(cfg,...rest){
    const pc=new RTC(cfg,...rest),origAdd=pc.addEventListener.bind(pc);
    pc.addEventListener=function(type,cb,...a){
      if(type==='icecandidate'){
        return origAdd(type,function(e){
          if(policy==='block'&&e&&e.candidate)return;
          if(policy==='value'&&fakeIp&&e&&e.candidate)try{Object.defineProperty(e.candidate,'address',{value:fakeIp});}catch(x){}
          return cb.apply(this,arguments);
        },...a);
      }
      return origAdd(type,cb,...a);
    };
    return pc;
  });
  window.RTCPeerConnection.prototype=RTC.prototype;
})(WEBRTC_POLICY,WEBRTC_FAKE_IP);"""

# ---------------------------------------------------------------------------
# Identity patches — run inside a single (function(fp){...})(fpJson) IIFE.
# fp fields: platformJs, chPlatform, platformVersion, cpuCount, memoryGb,
#            languages, chromeVersion, chromeFullVersion, brands,
#            fullVersionList, architecture, bitness.
# ---------------------------------------------------------------------------
# navigator.webdriver should be false (real Chrome), never undefined (deleting
# it makes CreepJS flag `webdriver === undefined`). The launch flag
# --disable-blink-features=AutomationControlled makes it natively false; this is
# a fallback that only acts (with a native-looking getter) if the flag is absent.
_WEBDRIVER = """\
try{ if(navigator.webdriver!==false){ Object.defineProperty(Navigator.prototype,'webdriver',{get:__zdNative(function(){return false;}),configurable:true,enumerable:true}); } }catch(e){}"""

_CHROME_OBJECT = """\
if(!window.chrome){window.chrome={app:{isInstalled:false,getDetails:function(){return null;},getIsInstalled:function(){return false;},runningState:function(){return 'cannot_run';}},runtime:{},loadTimes:function(){return null;},csi:function(){return null;}};}"""

_PERMISSIONS = """\
try{const _pq=window.Permissions&&Permissions.prototype.query;if(_pq){Permissions.prototype.query=__zdNative(function query(p){return p.name==='notifications'?Promise.resolve({state:'default',onchange:null}):_pq.call(this,p);});}}catch(e){}"""

_NAVIGATOR_PROPS = """\
(function(){
  function _isNative(fn){return typeof fn==='function'&&fn.toString().includes('[native code]');}
  function _nativeVal(pd){if(!pd||!_isNative(pd.get))return undefined;try{return pd.get.call(navigator);}catch(e){return undefined;}}
  function _defProp(key,val){Object.defineProperty(Navigator.prototype,key,{get:__zdNative(function(){return val;}),configurable:true,enumerable:true});}
  const _plat=_nativeVal(Object.getOwnPropertyDescriptor(Navigator.prototype,'platform'));
  const _hw=_nativeVal(Object.getOwnPropertyDescriptor(Navigator.prototype,'hardwareConcurrency'));
  const _dm=_nativeVal(Object.getOwnPropertyDescriptor(Navigator.prototype,'deviceMemory'));
  const _nl=_nativeVal(Object.getOwnPropertyDescriptor(Navigator.prototype,'languages'));
  if(_plat!==undefined&&_plat!==fp.platformJs)_defProp('platform',fp.platformJs);
  if(_hw!==undefined&&_hw!==fp.cpuCount)_defProp('hardwareConcurrency',fp.cpuCount);
  if(_dm!==undefined&&typeof fp.deviceMemory==='number'&&_dm!==fp.deviceMemory)_defProp('deviceMemory',fp.deviceMemory);
  if(_nl!==undefined){const v=Array.from(_nl||[]);if(JSON.stringify(v)!==JSON.stringify(fp.languages))_defProp('languages',fp.languages);}
})();"""

# userAgentData is intentionally not overridden — Chrome's real NavigatorUAData
# object is already correct for the installed browser, and replacing it with a
# plain JS object makes constructor/instanceof checks detectable.

# Screen spoof via JS (NOT CDP setDeviceMetricsOverride, which reflows the page
# layout — visibly moving elements even when the window isn't resized). Defining
# screen.* properties has no layout effect. availHeight is kept < height so the
# "no taskbar" headless tell (screen.height === availHeight) does not fire, and
# the real window.innerWidth stays below screen.width so the "viewport == screen"
# tell does not fire either. SCREEN_* tokens substituted with integers/float.
_SCREEN = """\
(function(){
  function dp(k,v){try{Object.defineProperty(screen,k,{get:__zdNative(function(){return v;}),configurable:true});}catch(e){}}
  if(typeof screen!=='undefined'){
    dp('width',SCREEN_W); dp('height',SCREEN_H);
    dp('availWidth',SCREEN_AW); dp('availHeight',SCREEN_AH);
    dp('colorDepth',SCREEN_CD); dp('pixelDepth',SCREEN_PD);
  }
  try{Object.defineProperty(window,'devicePixelRatio',{get:__zdNative(function(){return SCREEN_DPR;}),configurable:true});}catch(e){}
})();"""

# Fill in desktop-Chrome APIs that headless omits (CreepJS headless tells).
# navigator.share / canShare and NetworkInformation.downlinkMax are present on a
# real desktop Chrome but missing in headless. (ContactsManager is intentionally
# NOT added — desktop Chrome lacks it too; adding it would look mobile.)
_HEADLESS_HINTS = """\
(function(){
  try{ if(!('share' in Navigator.prototype)) Navigator.prototype.share=__zdNative(function share(){return Promise.resolve();}); }catch(e){}
  try{ if(!('canShare' in Navigator.prototype)) Navigator.prototype.canShare=__zdNative(function canShare(){return true;}); }catch(e){}
  try{
    if(navigator.connection){
      var cp=Object.getPrototypeOf(navigator.connection);
      if(!('downlinkMax' in cp)) Object.defineProperty(cp,'downlinkMax',{get:__zdNative(function(){return Infinity;}),configurable:true,enumerable:true});
    }
  }catch(e){}
})();"""

_IDENTITY_BODY = "\n".join(
    [
        _WEBDRIVER,
        _CHROME_OBJECT,
        _PERMISSIONS,
        _NAVIGATOR_PROPS,
        _HEADLESS_HINTS,
    ]
)

# Web Worker identity patch. The page's addScriptToEvaluateOnNewDocument never
# runs in WorkerGlobalScope, so a worker's navigator otherwise exposes the real
# host (platform / hardwareConcurrency / deviceMemory). CDP overrides don't reach
# dedicated workers for these either (only the UA string does). Fix: shim the
# Worker / SharedWorker constructors so the patch is prepended to each worker's
# source (read synchronously for blob:/same-origin scripts). The patch is
# self-referential — it re-installs the same shim inside the worker — so nested
# workers are covered too. The literal values are substituted in (not passed as a
# closure arg) so the function reproduces them via toString() when re-injected.
# Token substitution: WORKER_PLATFORM / WORKER_HC / WORKER_DM / WORKER_LANGS.
_WORKER = """\
(function zdWorker(){
  function dp(o,k,v){try{Object.defineProperty(o,k,{get:function(){return v;},configurable:true,enumerable:true});}catch(e){}}
  // Only patch navigator inside a worker — the page's navigator is already
  // patched (native-shielded) by the identity body; re-patching there would
  // replace a native-looking getter with a plain one.
  if (typeof document === 'undefined' && typeof navigator !== 'undefined') {
    var p = Object.getPrototypeOf(navigator);
    dp(p,'platform',WORKER_PLATFORM);
    dp(p,'hardwareConcurrency',WORKER_HC);
    dp(p,'deviceMemory',WORKER_DM);
    dp(p,'languages',WORKER_LANGS);
    // Reformat OffscreenCanvas WebGL renderer to the target OS so it matches the
    // main thread (else a main-vs-worker renderer mismatch is itself a tell).
    var WGOS=WORKER_REFORMAT_OS;
    if(WGOS){
      var rf=function(real){ if(!real) return real; var m=real.match(/^ANGLE \\(([^,]+), (.+), ([^,)]+)\\)$/); if(!m) return real; var vendor=m[1],backend=m[2],model=backend; var inner=backend.match(/^(?:Vulkan|OpenGL)[^(]*\\((.+)\\)$/); if(inner) model=inner[1]; model=model.replace(/^(\\S+)\\s+\\1\\b/,'$1').replace(/\\bMesa\\s+/,''); if(WGOS==='windows') return 'ANGLE ('+vendor+', '+model+' Direct3D11 vs_5_0 ps_5_0, D3D11)'; if(WGOS==='macos') return 'ANGLE ('+vendor+', ANGLE Metal Renderer: '+model+', Unspecified Version)'; return real; };
      var wgp=function(pr){ if(!pr) return; var o=pr.getParameter; pr.getParameter=function(q){ if(q===0x9246) return rf(o.call(this,q)); return o.call(this,q); }; };
      if(typeof WebGLRenderingContext!=='undefined') wgp(WebGLRenderingContext.prototype);
      if(typeof WebGL2RenderingContext!=='undefined') wgp(WebGL2RenderingContext.prototype);
    }
  }
  var SELF='('+zdWorker.toString()+')();';
  var reg=(typeof __zdNative!=='undefined')?__zdNative:function(f){return f;};
  function wrap(Real){
    if(!Real) return Real;
    function shim(url,opts){
      try{
        var x=new XMLHttpRequest(); x.open('GET',url,false); x.send();
        var blob=new Blob([SELF+'\\n'+x.responseText],{type:'application/javascript'});
        return new Real(URL.createObjectURL(blob),opts);
      }catch(e){ return new Real(url,opts); }
    }
    shim.prototype=Real.prototype;
    return reg(shim);
  }
  try{ self.Worker = wrap(self.Worker); }catch(e){}
  try{ self.SharedWorker = wrap(self.SharedWorker); }catch(e){}
})();"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _seed_token(strat: Strategy, seed: int) -> str | None:
    """Return the JS seed expression for a noise surface, or None for Native."""
    if strat == Strategy.NATIVE:
        return None
    if strat == Strategy.BLOCK:
        return "0/*BLOCK*/"
    if strat == Strategy.RANDOM:
        return "(Math.random()*4294967296)>>>0"
    return str(seed & 0xFFFFFFFF)  # truncate to u32 for mulberry32


def _push_noise(
    parts: list[str], surface: Surface, cfg: SurfaceCfg | None, js: str, seed: int
) -> None:
    strat = surface.resolve_strategy(cfg.strategy if cfg else None)
    tok = _seed_token(strat, seed)
    if tok is not None:
        parts.append(js.replace("SEED", tok))


def _push_webgl(parts: list[str], spec: WebglSpec | None) -> None:
    strat = Surface.WEBGL.resolve_strategy(spec.strategy if spec else None)
    if strat == Strategy.NATIVE:
        return
    vendor = (
        json.dumps(spec.unmasked_vendor) if spec and spec.unmasked_vendor else "null"
    )
    renderer = (
        json.dumps(spec.unmasked_renderer)
        if spec and spec.unmasked_renderer
        else "null"
    )
    if vendor == "null" and renderer == "null":
        return
    parts.append(
        _WEBGL.replace("WEBGL_VENDOR", vendor).replace("WEBGL_RENDERER", renderer)
    )


def _push_fonts(parts: list[str], spec: FontSpec | None, seed: int) -> None:
    strat = Surface.FONTS.resolve_strategy(spec.strategy if spec else None)
    tok = _seed_token(strat, seed)
    if tok is None:
        return
    allow = (
        "[]"
        if strat == Strategy.BLOCK
        else (json.dumps(spec.available) if spec and spec.available else "null")
    )
    parts.append(_FONTS.replace("FONT_ALLOW", allow).replace("SEED", tok))


def _push_hardware(parts: list[str], spec: HardwareSpec | None) -> None:
    strat = Surface.HARDWARE.resolve_strategy(spec.strategy if spec else None)
    if strat == Strategy.NATIVE:
        return
    if strat == Strategy.BLOCK:
        battery, media, voices = "1", "0", "[]"
    else:
        battery = (
            str(spec.battery_level)
            if spec and spec.battery_level is not None
            else "null"
        )
        media = (
            str(spec.media_devices)
            if spec and spec.media_devices is not None
            else "null"
        )
        voices = (
            json.dumps(spec.speech_voices) if spec and spec.speech_voices else "null"
        )
    parts.append(
        _HARDWARE.replace("HW_BATTERY", battery)
        .replace("HW_MEDIA_DEVICES", media)
        .replace("HW_VOICES", voices)
    )


def _push_webrtc(parts: list[str], spec: WebrtcSpec | None) -> None:
    strat = Surface.WEBRTC.resolve_strategy(spec.strategy if spec else None)
    policy_map = {Strategy.NATIVE: "native", Strategy.VALUE: "value"}
    policy = policy_map.get(strat, "block")
    fake_ip = json.dumps(spec.fake_ip) if spec and spec.fake_ip else "null"
    parts.append(
        _WEBRTC.replace("WEBRTC_POLICY", f'"{policy}"').replace(
            "WEBRTC_FAKE_IP", fake_ip
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Identity patch injected into worker targets over CDP (Runtime.evaluate on the
# worker session). Used for service workers, which the page-level Worker shim
# cannot reach (they load from a real same-origin URL, not a blob). Overrides the
# worker's WorkerNavigator and its native userAgentData. Token substitution below.
_SW_INJECT = """\
(function(){
  var p = Object.getPrototypeOf(navigator);
  function dp(k,v){try{Object.defineProperty(p,k,{get:function(){return v;},configurable:true,enumerable:true});}catch(e){}}
  dp('platform',SW_PLATFORM);
  dp('hardwareConcurrency',SW_HC);
  dp('deviceMemory',SW_DM);
  dp('userAgent',SW_UA);
  dp('appVersion',SW_APPVERSION);
  dp('languages',SW_LANGS);
  try{
    var uad = navigator.userAgentData;
    if (uad) {
      var up = Object.getPrototypeOf(uad);
      Object.defineProperty(up,'platform',{get:function(){return SW_CH_PLATFORM;},configurable:true});
      var he = up.getHighEntropyValues;
      up.getHighEntropyValues = function(h){return he.call(this,h).then(function(v){v.platform=SW_CH_PLATFORM;v.platformVersion=SW_CH_VERSION;return v;});};
    }
  }catch(e){}
  var WGOS=SW_REFORMAT_OS;
  if(WGOS){
    var rf=function(real){ if(!real) return real; var m=real.match(/^ANGLE \\(([^,]+), (.+), ([^,)]+)\\)$/); if(!m) return real; var vendor=m[1],backend=m[2],model=backend; var inner=backend.match(/^(?:Vulkan|OpenGL)[^(]*\\((.+)\\)$/); if(inner) model=inner[1]; model=model.replace(/^(\\S+)\\s+\\1\\b/,'$1').replace(/\\bMesa\\s+/,''); if(WGOS==='windows') return 'ANGLE ('+vendor+', '+model+' Direct3D11 vs_5_0 ps_5_0, D3D11)'; if(WGOS==='macos') return 'ANGLE ('+vendor+', ANGLE Metal Renderer: '+model+', Unspecified Version)'; return real; };
    var wgp=function(pr){ if(!pr) return; var o=pr.getParameter; pr.getParameter=function(q){ if(q===0x9246) return rf(o.call(this,q)); return o.call(this,q); }; };
    if(typeof WebGLRenderingContext!=='undefined') wgp(WebGLRenderingContext.prototype);
    if(typeof WebGL2RenderingContext!=='undefined') wgp(WebGL2RenderingContext.prototype);
  }
})();"""


def _reformat_os(profile: "ResolvedProfile") -> str:
    """Target OS for cross-OS WebGL renderer reformatting, or '' when not applicable.

    Only when WebGL is NATIVE (real GPU passes through) and the target OS differs
    from the host OS — then both the main thread and workers rewrite the renderer
    backend wording so they stay consistent (Windows/macOS only).
    """
    strat = Surface.WEBGL.resolve_strategy(profile.webgl.strategy if profile.webgl else None)
    if strat != Strategy.NATIVE:
        return ""
    host_os = _OS_BY_PLATFORM.get(Persona.system().platform, "linux")
    target_os = _OS_BY_PLATFORM.get(profile.navigator.platform, "linux")
    return target_os if (target_os != host_os and target_os in ("windows", "macos")) else ""


def service_worker_inject_script(profile: "ResolvedProfile") -> str:
    """Build the worker-scope identity patch for CDP injection into worker targets."""
    nav = profile.navigator
    ua = profile.browser.ua_string
    app_version = ua[len("Mozilla/") :] if ua.startswith("Mozilla/") else ua
    languages = nav.languages or ["en-US"]
    return (
        _SW_INJECT.replace("SW_PLATFORM", json.dumps(nav.platform.js_string()))
        .replace("SW_HC", str(nav.hardware_concurrency))
        .replace("SW_DM", str(nav.device_memory_gb))
        .replace("SW_UA", json.dumps(ua))
        .replace("SW_APPVERSION", json.dumps(app_version))
        .replace("SW_LANGS", json.dumps(languages))
        .replace("SW_CH_PLATFORM", json.dumps(profile.ua_ch.platform))
        .replace("SW_CH_VERSION", json.dumps(profile.ua_ch.platform_version))
        .replace("SW_REFORMAT_OS", json.dumps(_reformat_os(profile)))
    )


def bootstrap_script(profile: "ResolvedProfile") -> str:
    """Assemble the full CDP stealth bootstrap script from a resolved profile.

    Runs the native-shield first so __zdNative() is available to all subsequent
    patches. Then wraps identity patches in a single fp-parameterized IIFE,
    then appends the PRNG and each surface patch when its resolved strategy
    is not Native.
    """
    seed = profile.seed.value if profile.seed else Seed.random().value
    nav = profile.navigator

    languages = list(nav.languages) if nav.languages else ["en-US"]

    fp_json = json.dumps(
        {
            "platformJs": nav.platform.js_string(),
            "cpuCount": nav.hardware_concurrency,
            "deviceMemory": nav.device_memory_gb,
            "languages": languages,
        }
    )

    reformat_os = _reformat_os(profile)
    worker_js = (
        _WORKER.replace("WORKER_PLATFORM", json.dumps(nav.platform.js_string()))
        .replace("WORKER_HC", str(nav.hardware_concurrency))
        .replace("WORKER_DM", str(nav.device_memory_gb))
        .replace("WORKER_LANGS", json.dumps(languages))
        .replace("WORKER_REFORMAT_OS", json.dumps(reformat_os))
    )

    scr = profile.screen
    screen_js = (
        _SCREEN.replace("SCREEN_W", str(scr.width))
        .replace("SCREEN_H", str(scr.height))
        .replace("SCREEN_AW", str(scr.avail_width))
        .replace("SCREEN_AH", str(scr.avail_height))
        .replace("SCREEN_CD", str(scr.color_depth))
        .replace("SCREEN_PD", str(scr.pixel_depth))
        .replace("SCREEN_DPR", repr(float(scr.device_pixel_ratio)))
    )

    parts = [
        _NATIVE_SHIELD,
        f"(function(fp){{\n{_IDENTITY_BODY}\n}})({fp_json});",
        worker_js,
        screen_js,
        _PRNG,
    ]

    _push_noise(parts, Surface.CANVAS, profile.canvas, _CANVAS, seed)
    _push_noise(parts, Surface.AUDIO, profile.audio, _AUDIO, seed)
    _push_noise(parts, Surface.CLIENT_RECTS, profile.client_rects, _CLIENT_RECTS, seed)
    _push_webgl(parts, profile.webgl)

    # Cross-OS WebGL renderer reformatting (main thread): rewrite the real GPU's
    # backend wording to the target OS so it doesn't leak the host (e.g. "Vulkan"
    # on Linux under a Windows UA). The worker patch above does the same in worker
    # scope so the two stay consistent. _reformat_os() gates both.
    if reformat_os:
        parts.append(_WEBGL_REFORMAT.replace("WEBGL_OS", json.dumps(reformat_os)))

    _push_fonts(parts, profile.fonts, seed)
    _push_hardware(parts, profile.hardware)
    _push_webrtc(parts, profile.webrtc)

    return "\n".join(parts)
