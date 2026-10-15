const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
(async () => {
  const FPS = 30, DUR = 30, N = FPS * DUR;
  const out = process.argv[2] || 'aom-intro.mp4';
  const ff = spawn('ffmpeg', ['-y', '-f', 'image2pipe', '-vcodec', 'png', '-r', String(FPS), '-i', '-',
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-crf', '17', '-preset', 'medium', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  await page.goto('file://' + path.resolve(__dirname, 'anim.html'));
  await page.evaluate(() => document.fonts.ready);
  for (let i = 0; i < N; i++) {
    const t = i / FPS;
    const dataUrl = await page.evaluate((t) => { window.seek(t); return document.getElementById('c').toDataURL('image/png'); }, t);
    const buf = Buffer.from(dataUrl.slice(dataUrl.indexOf(',') + 1), 'base64');
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % 150 === 0) console.log('frame', i, '/', N);
  }
  ff.stdin.end();
  await new Promise(r => ff.on('close', r));
  await browser.close();
  console.log('wrote', out);
})();
