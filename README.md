# Zendriver ✌️

[![License](https://img.shields.io/github/license/cdpdriver/zendriver)](LICENSE)
[![Pypi Version](https://img.shields.io/pypi/v/zendriver)](https://pypi.org/project/zendriver/)
[![Issues](https://img.shields.io/github/issues/cdpdriver/zendriver)]()
[![Pull Requests](https://img.shields.io/github/issues-pr/cdpdriver/zendriver)]()
[![codecov](https://codecov.io/github/cdpdriver/zendriver/branch/main/graph/badge.svg?token=F7K641TYFZ)](https://codecov.io/github/cdpdriver/zendriver)

> This package is a fork of [`ultrafunkamsterdam/nodriver`](https://github.com/ultrafunkamsterdam/nodriver/), created to add new features, compile unmerged bugfixes, and increase community engagement.

* **Documentation:** [zendriver.dev](https://zendriver.dev)
* **AI-generated wiki:** [deepwiki.com/cdpdriver/zendriver](https://deepwiki.com/cdpdriver/zendriver)

Zendriver is a blazing fast, async-first, undetectable webscraping/web automation framework implemented using the Chrome Devtools Protocol. Visit websites, scrape content, and run JavaScript using a real browser (no Selenium/Webdriver) all with just a few lines of Python.

**Docker support is here!** Check out [`cdpdriver/zendriver-docker`](https://github.com/cdpdriver/zendriver-docker) for an example of how to run Zendriver with a real, GPU-accelerated browser (not headless) in a Docker container. (Linux-only)

## Features

- **Undetectable** - Zendriver uses the Chrome Devtools Protocol instead of Selenium/WebDriver, making it (almost) impossible to detect
- **Blazing fast** - Chrome Devtools Protocol is _fast_, much faster than previous Selenium/WebDriver solutions. CDP combined with an async Python API makes Zendriver highly performant.
- **Feature complete and easy to use** - Packed with allowing you to get up and running in just a few lines of code.
- **First-class Docker support** - Traditionally, browser automation has been incredibly difficult to package with Docker, especially if you want to run real, GPU-accelerated Chrome (not headless). Now, deploying with Docker is easier than ever using the officially supported [zendriver-docker project template](https://github.com/cdpdriver/zendriver-docker).
- **Automatic cookie and profile management** - By default, uses fresh profile on each run, cleaning up on exit. Or, save and load cookies to a file to avoid repeating tedious login steps.
- **Smart element lookup** - Find elements selector or text, including iframe content. This could also be used as wait condition for a element to appear, since it will retry for the duration of `timeout` until found. Single element lookup by text using `tab.find()` accepts a `best_match flag`, which will not naively return the first match, but will match candidates by closest matching text length.
- **Easy debugging** - Descriptive `repr` for elements, which represents the element as HTML, makes debugging much easier.
- **Fingerprint spoofing** - Coherent cross-OS browser impersonation (Windows/macOS/Linux) — user agent, client hints, navigator, screen, fonts, WebGL, timezone, and worker scopes — plus per-session canvas/audio noise, via a simple `Persona` API.

## Installation

To install, simply use `pip` (or your favorite package manager):

```sh
pip install zendriver
# or uv add zendriver, poetry add zendriver, etc.
```

## Usage

Example for visiting [https://www.browserscan.net/bot-detection](https://www.browserscan.net/bot-detection) and saving a screenshot of the results:

```python
import asyncio

import zendriver as zd


async def main():
    browser = await zd.start()
    page = await browser.get("https://www.browserscan.net/bot-detection")
    await page.save_screenshot("browserscan.png")
    await browser.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

Check out the [Quickstart](https://zendriver.dev/quickstart/) for more information and examples.

### Fingerprint spoofing

Pass a `Persona` to `zd.start()` to present a coherent, per-session browser fingerprint. The recommended entry point is `Persona.sample(os=...)`, which impersonates a target OS: zendriver resolves a consistent profile — user agent, `navigator.userAgentData` client hints, platform, screen metrics, fonts, WebGL renderer, and worker scopes — from the live browser plus a curated per-OS pool, and layers deterministic per-session noise over the canvas and audio surfaces.

```python
import asyncio

import zendriver as zd
from zendriver import Persona


async def main():
    # Impersonate a coherent Windows browser (also "macos" or "linux").
    # Omit seed for a fresh fingerprint each run; pass one for reproducibility.
    persona = Persona.sample(os="windows", seed=42)

    browser = await zd.start(
        persona=persona,
        # Optional: render WebGL with the real GPU instead of SwiftShader.
        # browser_args=["--use-angle=vulkan"],  # or "gl-egl"
    )
    page = await browser.get("https://abrahamjuliot.github.io/creepjs/")
    await page.save_screenshot("result.png")
    await browser.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

#### Timezone

Timezone is a property of your exit IP, not your OS, so it is controlled separately via `persona.timezone`:

- `None` *(default)* — keep the host's real timezone.
- `"auto"` — derive the timezone from the exit IP's geolocation (works through any proxy/VPN), so it always matches the connection.
- `"<IANA zone>"` (e.g. `"Europe/London"`) — set explicitly, e.g. to match a proxy's region.

```python
persona = Persona.sample(os="macos")
persona.timezone = "auto"  # match whatever IP you connect from (proxy/VPN aware)
```

#### Advanced — override any layer

`Persona.sample(os=...)` fills every field coherently, but you can override **any** of them. The rule is simply: **a field you set wins; a field left at its default is resolved coherently** from the live browser + curated pool. Settable layers: identity (`platform`, `hardware_concurrency`, `device_memory_gb`, `timezone`, `locale`, `screen`, `client_hints`), the seven surfaces (`canvas`, `audio`, `client_rects`, `webgl`, `fonts`, `webrtc`, `hardware`), and `seed`.

Sample a coherent base, then tweak only what you need:

```python
from zendriver import Persona, Strategy, SurfaceCfg, WebglSpec, WebrtcSpec, HardwareSpec

persona = Persona.sample(os="windows", seed=42)
persona.hardware_concurrency = 8                       # CPU cores
persona.device_memory_gb = 16                          # RAM (GB)
persona.timezone = "auto"                              # follow exit IP; or an IANA zone
persona.locale = "en-GB"
persona.canvas = SurfaceCfg(strategy=Strategy.SEEDED)  # per surface: SEEDED | NATIVE | BLOCK
persona.webgl = WebglSpec(strategy=Strategy.NATIVE)    # NATIVE = real GPU passthrough
persona.webrtc = WebrtcSpec(strategy=Strategy.VALUE, fake_ip="203.0.113.5")
persona.hardware = HardwareSpec(battery_level=0.6, media_devices=2)
```

`Strategy.SEEDED` injects deterministic per-seed noise (same seed → same fingerprint across runs); `Strategy.NATIVE` leaves a surface untouched; `Strategy.BLOCK` neutralizes it.

> `Persona.sample()` guarantees internal coherence; manual overrides put coherence on you. A Windows `platform` paired with a macOS font list, or a timezone that doesn't match the locale/IP, are exactly the contradictions detectors look for — override with intent.

#### Surface defaults — what each does and why

Seven surfaces are individually configurable. The defaults below are tuned so the browser stays *coherent* — for some surfaces that means injecting noise, for others it means leaving the genuine value (a fake value would be the giveaway).

| Surface | Default | Why |
|---|---|---|
| Canvas | `SEEDED` | per-session pixel noise breaks cross-session canvas-hash linking, with no loss of validity |
| Audio | `SEEDED` | per-session `AnalyserNode` noise, same rationale |
| Fonts | `VALUE` | presents the target OS's font set (and hides the host's) so enumeration matches the impersonated OS |
| WebGL | `NATIVE` | real GPU passes through (cross-OS personas reformat only the backend wording, e.g. Vulkan → Direct3D11/Metal); a fake renderer string is caught by metadata-vs-pixel (`webglHash`) checks |
| ClientRects | `NATIVE` | rect noise breaks the geometric invariants real browsers guarantee (`right-left==width`, fixed rotation hash) and is flagged as a lie — consistent rects are safer |
| WebRTC | `BLOCK` | drops ICE candidates so WebRTC can't leak your real IP from behind a proxy/VPN |
| Hardware | `NATIVE` | battery / media devices / voices kept genuine; fabricating them risks an incoherent value that is itself a tell |

> `NATIVE` is a deliberate choice, not a gap: for WebGL and ClientRects a real browser returns consistent, valid values, so *faking* them is the detectable tell. This default set is what produces zero detected lies on fingerprinting tests.

This spoofs the **browser** layer only. Whether your exit IP is flagged as a datacenter/VPN is a property of the IP itself (use a residential proxy if needed) — browser spoofing cannot change it.

## Rationale for the fork

Zendriver remains committed to `nodriver`'s goals of staying undetected for all modern anti-bot solutions and also keeps with the batteries-included approach of its predecessor. Unfortunately, contributions to the original [`nodriver` repo](https://github.com/ultrafunkamsterdam/nodriver/) are heavily restricted, making it difficult to submit issues or pull requests. At the time of writing, there are several pull requests open to fix critical bugs which have beeen left unaddressed for many months.

Zendriver aims to change this by:

1. Including open pull requests in the original `nodriver` repo as part of the initial release
2. Modernizing the development process to include static analysis tools such as [`ruff`](https://docs.astral.sh/ruff/) and [`mypy`](https://mypy-lang.org/), reducing the number of easy-to-catch bugs which make it through in the future
3. Opening up the issue tracker and pull requests for community contributions, allowing the project to continue to grow along with its community.

With these changes in place, we hope to further development of state-of-the-art open-source web automation tools even further, helping to once again make the web truly open for all.

## Contributing

Contributions of all types are always welcome! Please see [CONTRIBUTING.md](https://github.com/cdpdriver/zendriver/blob/main/CONTRIBUTING.md) for details on how to contribute.

### Getting additional help

If you have a question, bug report, or want to make a general inquiry about the project, please create a new GitHub issue. If you are having a problem with Zendriver, please make sure to include your operating system, Chrome version, code example demonstrating the issue, and any other information that may be relevant.

Questions directed to any personal accounts outside of GitHub will be ignored.

## Sponsors

### Powered by
[![JetBrains logo.](https://resources.jetbrains.com/storage/products/company/brand/logos/jetbrains.svg)](https://jb.gg/OpenSource)
