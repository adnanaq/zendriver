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
    Seed,
    Strategy,
    Surface,
    SurfaceCfg,
    WebglSpec,
    WebrtcSpec,
)

if TYPE_CHECKING:
    from .stealth import Fingerprint, Persona

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
# ---------------------------------------------------------------------------
_NATIVE_SHIELD = """\
(function(){
  const _fns=new WeakSet();
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
  function n(v) { return v + (rng() - 0.5) * 1e-3; }
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

# Only emitted when strategy is not NATIVE and vendor/renderer are provided.
# WEBGL_VENDOR / WEBGL_RENDERER substituted with JSON strings.
_WEBGL = """\
(function(vendor,renderer){
  function patch(p){const o=p.getParameter;p.getParameter=__zdNative(function getParameter(q){if(vendor&&q===0x9245)return vendor;if(renderer&&q===0x9246)return renderer;return o.call(this,q);});}
  if(window.WebGLRenderingContext)patch(WebGLRenderingContext.prototype);
  if(window.WebGL2RenderingContext)patch(WebGL2RenderingContext.prototype);
})(WEBGL_VENDOR,WEBGL_RENDERER);"""

_FONTS = """\
(function(allow,seed){
  const rng=__zdRng(seed);
  const orig=CanvasRenderingContext2D.prototype.measureText;
  CanvasRenderingContext2D.prototype.measureText=__zdNative(function measureText(t){
    const m=orig.call(this,t);
    try{Object.defineProperty(m,'width',{value:m.width+(rng()-0.5)*1e-3});}catch(e){}
    return m;
  });
  if(Array.isArray(allow)&&document.fonts&&document.fonts.check){
    const oc=document.fonts.check.bind(document.fonts);
    document.fonts.check=__zdNative(function check(font,text){
      const fam=(font||'').split(/[ \t]+/).pop();
      if(fam&&allow.indexOf(fam.replace(/["']/g,''))===-1)return false;
      return oc(font,text);
    });
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
_WEBDRIVER = """\
try{
  const _wd=Object.getOwnPropertyDescriptor(Navigator.prototype,'webdriver');
  if(_wd){delete Navigator.prototype.webdriver;}
}catch(e){}"""

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
  const _nl=_nativeVal(Object.getOwnPropertyDescriptor(Navigator.prototype,'languages'));
  if(_plat!==undefined&&_plat!==fp.platformJs)_defProp('platform',fp.platformJs);
  if(_hw!==undefined&&_hw!==fp.cpuCount)_defProp('hardwareConcurrency',fp.cpuCount);
  if(_nl!==undefined){const v=Array.from(_nl||[]);if(JSON.stringify(v)!==JSON.stringify(fp.languages))_defProp('languages',fp.languages);}
})();"""

# userAgentData is intentionally not overridden — Chrome's real NavigatorUAData
# object is already correct for the installed browser, and replacing it with a
# plain JS object makes constructor/instanceof checks detectable.

_IDENTITY_BODY = "\n".join(
    [
        _WEBDRIVER,
        _CHROME_OBJECT,
        _PERMISSIONS,
        _NAVIGATOR_PROPS,
    ]
)

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


def bootstrap_script(persona: "Persona", fingerprint: "Fingerprint") -> str:
    """Assemble the full CDP stealth bootstrap script.

    Runs the native-shield first so __zdNative() is available to all subsequent
    patches. Then wraps identity patches in a single fp-parameterized IIFE,
    then appends the PRNG and each surface patch when its resolved strategy
    is not Native.
    """
    cpu = persona.hardware_concurrency or fingerprint.cpu_count
    locale = persona.locale or fingerprint.locale or "en-US"
    plat = persona.platform or fingerprint.platform
    seed = persona.seed.value if persona.seed else Seed.random().value

    languages = [locale]
    # Always include the base language tag (e.g. "en-US" → also append "en")
    lang_base = locale.split("-")[0] if "-" in locale else None
    if lang_base and lang_base not in languages:
        languages.append(lang_base)

    fp_json = json.dumps(
        {
            "platformJs": plat.js_string(),
            "cpuCount": cpu,
            "languages": languages,
        }
    )

    parts = [
        _NATIVE_SHIELD,
        f"(function(fp){{\n{_IDENTITY_BODY}\n}})({fp_json});",
        _PRNG,
    ]

    _push_noise(parts, Surface.CANVAS, persona.canvas, _CANVAS, seed)
    _push_noise(parts, Surface.AUDIO, persona.audio, _AUDIO, seed)
    _push_noise(parts, Surface.CLIENT_RECTS, persona.client_rects, _CLIENT_RECTS, seed)
    _push_webgl(parts, persona.webgl)
    _push_fonts(parts, persona.fonts, seed)
    _push_hardware(parts, persona.hardware)
    _push_webrtc(parts, persona.webrtc)

    return "\n".join(parts)
