# Third-party runtime assets

- Pyodide 0.29.3 — Mozilla Public License 2.0. Runtime assets are pinned by SHA-256.
- FontTools 4.56.0 — MIT License. The wheel is loaded only inside the browser Web Worker.
- FreeType 2.14.1 outline emboldening algorithm — FreeType License. The fixed-version
  `FT_Outline_EmboldenXY` algorithm is adapted in `font_processor.py`; no FreeType
  binary or font parser is bundled.
- Weixin JS SDK 1.6.0 — used only for the `wx.miniProgram` WebView bridge.

The manifest in `runtime-assets.json` is the source of truth for versions, URLs, sizes and hashes.
The fixed jsDelivr URLs are attempted first; the verified copies stored under `vendor/pyodide/`
remain available as an automatic fallback when the third-party CDN is unavailable.
Font files selected by users are never part of these assets and are never sent to these origins.
