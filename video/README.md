# AOM intro video sources

Everything here renders deterministically: an HTML canvas animation is seeked frame-by-frame in headless Chromium and piped to ffmpeg.

- `anim.html` — 30s motion-graphics intro (self-contained). Render: `node render.js aom-intro.mp4`
- `scenes2.js` + `build.py` → `anim2.html` — 60s / 120s editions with live app screens. Render: `node render2.js 60 aom-60s.mp4` or `node render2.js 120 aom-120s.mp4`
- `shots/` — app screenshots at 1564x784 (Chrome, window 1920x1130). Filenames are referenced in `SHOTS` at the top of `scenes2.js`.
- Contact sheet for a quick check: `node render2.js 60 x 1.5,10,20,30,45` writes `p_60_<t>.png`.

Needs: Node + `playwright` (Chromium), `ffmpeg`, Inter / Inter Display fonts. Output is 1920x1080 @ 30fps, H.264, no audio.

## Recapture checklist (after the 24h demo run)
1. Pages: /memory (+ click View on a user), /topology, /context-cache, /llm-caching, /, /servers, /catalog, /threat-detection, /audit-log — full-res screenshots into `shots/`.
2. If the in-app charts are populated, delete the overlay chart calls in `H_` and `I_` (barChart/lineChart/donut) and keep only the counters.
3. Update real figures in `H_`, `I_`, `F` (hit rates, req/min, tokens saved) from the pages.
4. `python3 build.py && node render2.js 60 && node render2.js 120`.
