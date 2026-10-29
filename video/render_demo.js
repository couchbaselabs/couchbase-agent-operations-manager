const { chromium } = require('playwright');const { spawn } = require('child_process');const path = require('path');
(async () => {
  const FPS=30, ED=parseInt(process.argv[2]||'60'), out=process.argv[3]||`aom-${ED}s.mp4`, probe=process.argv[4];
  const browser=await chromium.launch({args:['--allow-file-access-from-files']});
  const page=await browser.newPage({viewport:{width:1920,height:1080}});
  page.on('pageerror',e=>console.log('PAGEERR',e.message));
  await page.goto('file://'+path.resolve(__dirname,'anim_demo.html'));
  await page.evaluate(()=>Promise.all([document.fonts.ready,window.assetsReady]));
  const total=await page.evaluate(ed=>window.setEdition(ed),ED);
  if(probe){ // contact sheet at given times
    const ts=probe.split(',').map(Number);
    for(const t of ts){await page.evaluate(t=>window.seek(t),t);await page.screenshot({path:`p_${ED}_${t}.png`})}
    await browser.close();return;
  }
  const N=Math.round(FPS*total);
  const ff=spawn('ffmpeg',['-y','-f','image2pipe','-vcodec','png','-r',String(FPS),'-i','-','-c:v','libx264','-pix_fmt','yuv420p','-profile:v','high','-crf','17','-preset','medium','-movflags','+faststart',out],{stdio:['pipe','ignore','inherit']});
  for(let i=0;i<N;i++){const t=i/FPS;const d=await page.evaluate(t=>{window.seek(t);return document.getElementById('c').toDataURL('image/png')},t);
    const buf=Buffer.from(d.slice(d.indexOf(',')+1),'base64');if(!ff.stdin.write(buf))await new Promise(r=>ff.stdin.once('drain',r));if(i%300===0)console.log('frame',i,'/',N)}
  ff.stdin.end();await new Promise(r=>ff.on('close',r));await browser.close();console.log('wrote',out,total+'s');
})();
