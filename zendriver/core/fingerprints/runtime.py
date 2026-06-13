"""Best-effort probe of the live browser's real UA-CH brands.

The brand list (Chromium / Google Chrome / a GREASE entry) is OS-independent, so
reusing the browser's real brands while spoofing only the platform fields keeps
the fingerprint coherent. The GREASE brand string changes per Chrome milestone
and must not be hardcoded — hence reading it from the running browser.

navigator.userAgentData is only populated on a real http(s) origin (it is
undefined on about:blank and data: URLs), so this probe succeeds only when the
given tab is already on such a page; otherwise it returns None and the resolver
falls back to version-derived brands.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple

from .profile import Brand

_PROBE_JS = """
(async function () {
  const uad = navigator.userAgentData;
  if (!uad) return null;
  try {
    const h = await uad.getHighEntropyValues(["fullVersionList"]);
    return JSON.stringify({ brands: uad.brands, full: h.fullVersionList });
  } catch (e) {
    return null;
  }
})()
"""


def parse_brands(payload: Any) -> Optional[Tuple[List[Brand], List[Brand]]]:
    """Parse a high-entropy payload into (brands, full_version_list), or None."""
    if not payload:
        return None
    data = json.loads(payload) if isinstance(payload, str) else payload
    brands = data.get("brands") or []
    full = data.get("full") or []
    if not brands:
        return None
    return (
        [Brand(b["brand"], b["version"]) for b in brands],
        [Brand(b["brand"], b["version"]) for b in full],
    )


async def probe_brands(tab: Any) -> Optional[Tuple[List[Brand], List[Brand]]]:
    """Read the live browser's real UA-CH brands from ``tab``.

    Returns None when userAgentData is unavailable (e.g. the tab is on
    about:blank / a data: URL), so the caller can fall back.
    """
    try:
        payload = await tab.evaluate(_PROBE_JS, await_promise=True)
        return parse_brands(payload)
    except Exception:
        return None
