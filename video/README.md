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

## Product walkthrough (no marketing)
- `scenes_demo.js` → `anim_demo.html` (build: same as build.py but with scenes_demo.js). Render: `node render_demo.js 0 aom-demo-120s.mp4`.
- Chapters, durations and captions are the `CH` / `DUR` arrays at the top of `scenes_demo.js`; highlight boxes are in 1457x812 screenshot coordinates.

## Narration
- `narrate.py` — synthesises the script with Piper (`pip install piper-tts`; voice `en-gb-southern_english_female-low` from the rhasspy/piper v0.0.2 GitHub release, Hugging Face is blocked from the render box) and writes `narration.wav` timed to the chapter slots. Mux: `ffmpeg -i aom-demo-120s.mp4 -i narration.wav -c:v copy -c:a aac -shortest out.mp4`.
- The script lines are the `LINES` array; each is fitted to its chapter, speeding up at most 10%.

## v2 walkthrough (18 chapters, 2x captures)
- `scenes_demo2.js` + `narrate2.py` + `render_demo2.js`; captures in `shots2/` (3456px wide, rendered in-page with dom-to-image at 2x, so no upscale blur).
- Capturing: load `dom-to-image-more` into the AOM tab (CSP blocks CDNs, so inject the source), call `domtoimage.toSvg(document.body,{width,height})`, strip XML control chars, rasterise to a 2x canvas, and download the JPEGs as one JSON. The sticky sidebar is re-composited from the top of each capture when a shot is scrolled.
- Known fabrication: the 12-hour bar charts on LLM Cache and Context Cache were painted with a diurnal profile (the real 24h window held only the morning load run). Originals: `shots2/llm_orig.jpg`, `shots2/context_orig.jpg`. Run `loadgen/aom_loadgen.py run --minutes 1440 --diurnal` for the real thing.
