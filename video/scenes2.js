// ================= ASSETS =================
const IMGW=1564,IMGH=784;
const SHOTS={memory:'shots/memory.jpg',memory_view:'shots/memory_view.jpg',topology:'shots/topology.jpg',context:'shots/context_cache.jpg',llm:'shots/llm_cache.jpg',dashboard:'shots/dashboard.jpg',threat:'shots/threat.jpg',audit:'shots/audit.jpg',servers:'shots/servers.jpg',catalog:'shots/catalog.jpg'};
const IMG={};
window.assetsReady=Promise.all(Object.entries(SHOTS).map(([k,src])=>new Promise(res=>{const im=new Image();im.onload=()=>{IMG[k]=im;res()};im.onerror=()=>res();im.src=src})));
const GREEN='#16A34A',TEAL='#2DD4BF',AMBER='#F59E0B',BLUE='#3B82F6';

// ---- camera over a screenshot. cam = {x0,y0,z0,x1,y1,z1}; focus point in image coords, z = zoom on top of cover
function screenBegin(key,p,cam){
  const im=IMG[key];const z=lerp(cam.z0,cam.z1,p),fx=lerp(cam.x0,cam.x1,p),fy=lerp(cam.y0,cam.y1,p);
  const S=(H/IMGH)*z;let ox=W/2-fx*S,oy=H/2-fy*S;ox=clamp(ox,W-IMGW*S,0);oy=clamp(oy,H-IMGH*S,0);
  g.save();g.translate(ox,oy);g.scale(S,S);
  g.fillStyle=PAPER;g.fillRect(0,0,IMGW,IMGH);
  if(im)g.drawImage(im,0,0,IMGW,IMGH);
  return S;
}
function screenEnd(){g.restore()}
function hilite(x,y,w,h,t,alpha=1){g.save();g.globalAlpha=alpha;g.strokeStyle=RED;g.lineWidth=4+Math.sin(t*9)*1.5;g.shadowColor=RED;g.shadowBlur=18;g.strokeRect(x,y,w,h);g.restore()}
function cursor(x,y,click=0){g.save();g.translate(x,y);
  if(click>0&&click<1){g.strokeStyle=RED;g.lineWidth=3;g.globalAlpha=1-click;g.beginPath();g.arc(0,0,8+click*34,0,7);g.stroke();g.globalAlpha=1}
  g.fillStyle='#000';g.strokeStyle='#fff';g.lineWidth=2;g.beginPath();g.moveTo(0,0);g.lineTo(0,22);g.lineTo(6,17);g.lineTo(10,26);g.lineTo(14,24);g.lineTo(10,15);g.lineTo(17,15);g.closePath();g.fill();g.stroke();g.restore()}
function lowerThird(title,sub,p,dark=false){ // bottom-left panel
  if(p<=0)return;const e=outExpo(clamp(p));g.save();g.translate(lerp(-700,0,e),0);
  g.fillStyle=RED;g.fillRect(0,H-230,24,150);
  g.fillStyle=dark?'rgba(11,11,13,.92)':'rgba(11,11,13,.92)';g.fillRect(24,H-230,g.measureText(title).width*0+900,150);
  txt(title,60,H-180,{size:58,weight:900,align:'left',color:'#fff',ls:-2});
  txt(sub,62,H-118,{size:28,weight:600,align:'left',color:'#C9C9D0'});
  g.restore()}
function bigStat(val,label,x,y,p,color=RED,size=150){if(p<=0)return;const e=outBack(clamp(p));g.save();g.translate(x,y);g.scale(lerp(.6,1,e),lerp(.6,1,e));g.globalAlpha=clamp(p*4);
  g.fillStyle='rgba(11,11,13,.9)';const w=Math.max(380,val.length*size*.6+80);g.fillRect(-w/2,-size*.75,w,size*1.55);
  txt(val,0,-size*.05,{size,color,ls:-6});txt(label,0,size*.55,{size:26,weight:700,color:'#C9C9D0',ls:4});g.restore()}
function dim(a){g.fillStyle='rgba(11,11,13,'+a+')';g.fillRect(0,0,W,H)}
// ---- charts (image-space)
function barChart(x,y,w,h,vals,p,col=RED){const n=vals.length,bw=w/n*.68,mx=Math.max(...vals);for(let i=0;i<n;i++){const q=clamp(p*n-i*.6);const v=vals[i]*outQuint(clamp(q))/mx*h;g.fillStyle=col;g.fillRect(x+i*(w/n)+(w/n-bw)/2,y+h-v,bw,v)}}
function lineChart(x,y,w,h,vals,p,col=RED,fill=true){const n=vals.length,mx=Math.max(...vals);const k=Math.floor(p*(n-1));g.save();g.beginPath();for(let i=0;i<=k;i++){const px=x+i/(n-1)*w,py=y+h-vals[i]/mx*h;i?g.lineTo(px,py):g.moveTo(px,py)}
  if(fill){g.lineTo(x+k/(n-1)*w,y+h);g.lineTo(x,y+h);g.closePath();g.fillStyle=col;g.globalAlpha=.15;g.fill();g.globalAlpha=1}
  g.beginPath();for(let i=0;i<=k;i++){const px=x+i/(n-1)*w,py=y+h-vals[i]/mx*h;i?g.lineTo(px,py):g.moveTo(px,py)}g.strokeStyle=col;g.lineWidth=5;g.lineJoin='round';g.stroke();
  if(k>0){g.fillStyle=col;g.beginPath();g.arc(x+k/(n-1)*w,y+h-vals[k]/mx*h,8,0,7);g.fill()}g.restore()}
function donut(cx,cy,r,parts,p,centerText,sub){ // parts [[frac,color]]
  g.save();g.fillStyle='#fff';g.beginPath();g.arc(cx,cy,r+22,0,7);g.fill();
  let a=-Math.PI/2;const tot=outQuint(clamp(p));for(const [f,c] of parts){const e=a+f*Math.PI*2*tot;g.beginPath();g.arc(cx,cy,r,a,e);g.strokeStyle=c;g.lineWidth=28;g.stroke();a=e}
  txt(centerText,cx,cy-6,{size:54,weight:900,color:INK,ls:-2});txt(sub,cx,cy+34,{size:18,weight:600,color:GREY});g.restore()}
const R24=(()=>{const r=rng(42);return Array.from({length:24},(_,i)=>400+Math.sin(i/24*6.3-1.2)*300+r()*220+(i>14?i*40:0))})();
const TOK=(()=>{let v=0;const r=rng(9);return Array.from({length:40},(_,i)=>v+=1.2e6+Math.pow(i,1.6)*4e4+r()*1e6)})();


// ---- at-scale ticker (sourced) shown on live-screen segments
const TICK=['40% of enterprise apps ship task-specific agents in 2026, up from <5%  —  Gartner','82% of enterprises found AI agents they did not know about  —  Cloud Security Alliance','65% had an agent-related incident in 12 months · 61% data exposure  —  CSA','21,000+ public MCP servers · 232% growth in six months  —  Nordic APIs / Bloomberry','67M local MCP server downloads in one month  —  Pulse MCP','Only 8.5% of audited MCP servers use OAuth  —  Astrix','Only 21% have a formal process to decommission agents  —  CSA','3.2 quadrillion tokens/month at Google alone  —  Sundar Pichai, I/O 2026'];
function ticker(t){const speed=260;const gap=120;g.save();g.fillStyle='rgba(11,11,13,.92)';g.fillRect(0,0,W,64);g.fillStyle=RED;g.fillRect(0,64,W,4);
  g.font=`700 26px ${DISPLAY}`;g.textBaseline='middle';g.textAlign='left';const widths=TICK.map(s=>g.measureText(s).width+gap);const total=widths.reduce((a,b)=>a+b,0);let x=W-((t*speed)%total);
  for(let k=0;k<2;k++){for(let i=0;i<TICK.length;i++){if(x<W&&x+widths[i]>0){g.fillStyle=RED;g.fillRect(x-gap/2+20,24,12,16);g.fillStyle='#fff';g.fillText(TICK[i],x,34)}x+=widths[i]}}
  g.fillStyle='rgba(11,11,13,1)';g.fillRect(W-300,0,300,64);g.fillStyle=RED;g.fillRect(W-300,0,4,64);txt('AGENTS AT SCALE',W-40,34,{size:20,weight:900,color:RED,align:'right',ls:4});g.restore()}
// ================= SCENES =================
// A: hook
function A(lt,D){bg();drawLines(lt,.6);const r=rng(11);const burst=outExpo(seg(lt,.25,1.6));
  for(let i=0;i<380;i++){const a=r()*Math.PI*2,d=(r()*.8+.2)*1100,sp=r()*.5+.75;const x=W/2+Math.cos(a)*d*burst*sp,y=H/2+Math.sin(a)*d*burst*sp*.62;const s=2+r()*5;g.fillStyle=r()>.85?RED:'#D4D4DA';g.globalAlpha=lerp(1,.35,burst);g.fillRect(x,y,s,s);
    if(r()>.93&&burst>.2){g.strokeStyle=RED;g.globalAlpha=.25;g.beginPath();g.moveTo(x,y);g.lineTo(W/2+(r()-.5)*400,H/2+(r()-.5)*200);g.stroke()}}
  g.globalAlpha=1;const pr=seg(lt,0,.3);g.fillStyle=RED;g.beginPath();g.arc(W/2,H/2,lerp(0,70,outBack(pr))*(1-seg(lt,.25,.5)),0,Math.PI*2);g.fill();
  const t2=D*.62;
  if(lt<t2){slam('AGENTS',W/2,H/2-80,seg(lt,.55,.95),{size:240,ls:-10});slam('ARE EVERYWHERE.',W/2,H/2+120,seg(lt,.85,1.25),{size:150,ls:-4,color:RED2})}
  else{const p=seg(lt,t2,t2+1);const v=lerp(5,40,outQuint(p));txt(Math.round(v)+'%',W/2-30,H/2-40,{size:380,ls:-20,color:RED,alpha:clamp(p*6)});
    txt('OF ENTERPRISE APPS SHIP AI AGENTS IN 2026',W/2,H/2+200,{size:52,weight:800,color:PAPER,ls:3,alpha:seg(lt,t2+.2,t2+.5)});txt('up from under 5% a year ago  ·  Gartner',W/2,H/2+275,{size:32,weight:500,color:GREY,alpha:seg(lt,t2+.4,t2+.7)});flashFrame(seg(lt,t2,t2+.2),'#fff')}}
// B: chaos + stats
const STAMPS2=[['21,000+ PUBLIC MCP SERVERS',-.05],['ONLY 8.5% USE OAUTH',.05],['PROMPT INJECTION',-.03],['OVER-PRIVILEGED',.06],['NO AUDIT TRAIL',-.05],['TOKENS BURNED TWICE',.04]];
const CALLS2=['invoke  delete_table(prod)','invoke  send_email(all_users)','discover  *  (no filter)','invoke  exec_shell("rm -rf")','auth  —  none','tool.description  ←  "ignore previous…"','invoke  read_secrets()','cost  $$$  (no cache)','identity  ?  unknown','invoke  wire_transfer()'];
function B(lt,D){bg('#000');g.save();g.font=`500 30px ${MONO}`;g.textBaseline='middle';const sp=lt*2200;for(let i=0;i<40;i++){const yy=((i*52+(H+sp))%(H+60))-30;const s=CALLS2[(i+Math.floor(sp/52))%CALLS2.length];g.fillStyle=i%7===0?RED:'#3A3A42';g.globalAlpha=.9;g.textAlign='left';g.fillText(s,60+((i*131)%700),yy);g.fillText(s,1000+((i*97)%600),(yy+H/2)%H)}g.restore();
  const k=D/3;for(let i=0;i<STAMPS2.length;i++){const st=i*.3*k,p=seg(lt,st,st+.18);if(p<=0)continue;const [s,ang]=STAMPS2[i];const rr=rng(i*13+5);const x=lerp(300,W-300,rr()),y=lerp(200,H-200,rr());const sc=lerp(2.2,1,outExpo(p));g.save();g.translate(x,y);g.scale(sc,sc);tag(s,0,0,i%2?PAPER:RED,i%2?INK:'#fff',52,26,clamp(p*5)*(lt-st<1.2*k?1:.55),ang);g.restore()}
  const t1=1.5*k,t2=2.4*k;if(lt>t1){dim(.7*seg(lt,t1,t1+.2));
    const big=(num,label,src,x,y,st)=>{const p=seg(lt,st,st+.3);if(p<=0)return;slam(num,x,y,p,{size:230,ls:-12});txt(label,x,y+150,{size:40,weight:800,color:RED2,ls:4,alpha:clamp(p*3)});txt(src,x,y+205,{size:26,weight:600,color:GREY,alpha:clamp(p*3)})};
    big('82%','RUN UNKNOWN AI AGENTS','Cloud Security Alliance, 2026',W/4+40,H/2-80,t1+.1);big('65%','HAD AN AGENT INCIDENT','Cloud Security Alliance, 2026',W*3/4-40,H/2-80,t1+.4)}
  if(lt>t2){dim(.85*seg(lt,t2,t2+.15));slam('WHO IS WATCHING',W/2,H/2-90,seg(lt,t2,t2+.3),{size:180,ls:-8});slam('YOUR AGENTS?',W/2,H/2+110,seg(lt,t2+.15,t2+.45),{size:170,ls:-6,color:RED2})}
  glitchSlices(lt,6,90,3)}
// C: lockup
function C(lt,D){bg(PAPER);const e=outExpo(seg(lt,.05,.9));g.fillStyle=RED;g.fillRect(0,0,W*e,H);
  const lp=outBack(seg(lt,.35,.95));if(lp>0){g.save();g.translate(0,lerp(-500,0,lp));logo(380,H/2,170);g.restore()}
  const x0=600;const line=(s,y,p,size)=>{if(p<=0)return;g.save();g.beginPath();g.rect(x0,y-size*.62,W,size*1.3);g.clip();txt(s,x0+lerp(-120,0,outExpo(p)),y,{size,align:'left',color:'#fff',ls:-4,alpha:clamp(p*3)});g.restore()};
  line('COUCHBASE',H/2-200,seg(lt,.6,1.2),110);line('AGENT OPERATIONS',H/2-60,seg(lt,.75,1.35),130);line('MANAGER',H/2+80,seg(lt,.9,1.5),130);
  txt('The control plane for your AI agents.',x0+6,H/2+200,{size:46,weight:600,align:'left',color:'#FFD6D7',alpha:seg(lt,1.4,1.8)});flashFrame(seg(lt,D-.25,D),'#fff')}
// D: Agent Memory list (live screen)
function D_(lt,D){const p=inOutQuint(clamp(lt/D));screenBegin('memory',p,{x0:780,y0:392,z0:1,x1:560,y1:160,z1:1.55});
  const hp=seg(lt,.5,.9);if(hp>0){hilite(245,105,1290,88,lt,hp)}
  screenEnd();
  bigStat('54,274','MEMORY ENTRIES',W/2-380,H/2+150,seg(lt,.9,1.3));bigStat('29','USERS REMEMBERED',W/2+420,H/2+150,seg(lt,1.1,1.5),'#fff',150);
  ticker(TT);lowerThird('AGENT MEMORY','Per-user memory · semantic recall · consolidation every 60 min',seg(lt,.15,.6));
  }
// E: click View → user memories
function E_(lt,D){const click=seg(lt,.15,.5);const after=lt>.5;
  if(!after){screenBegin('memory',1,{x0:560,y0:160,z0:1.55,x1:560,y1:160,z1:1.55});cursor(lerp(900,748,outQuint(seg(lt,0,.4))),lerp(380,292,outQuint(seg(lt,0,.4))),seg(lt,.35,.6));screenEnd()}
  else{const q=inOutQuint(seg(lt,.5,D));screenBegin('memory_view',q,{x0:1200,y0:420,z0:1.2,x1:1215,y1:560,z1:1.55});
    const hp=seg(lt,1.2,1.6);if(hp>0)hilite(900,432,634,180,lt,hp);
    screenEnd();
    flashFrame(seg(lt,.5,.7),'#fff');
    bigStat('996 → 1','ENTRIES CONSOLIDATED',W/2-430,H/2+40,seg(lt,1.8,2.2));
    ticker(TT);lowerThird('WHAT THE AGENT REMEMBERS','sim-operator-14 · semantic rollup · recalled by vector search',seg(lt,.6,1.0))}}
// F: Live Topology with animated flows
function F(lt,D){const p=inOutQuint(clamp(lt/D));screenBegin('topology',p,{x0:880,y0:400,z0:1.05,x1:880,y1:400,z1:1.25});
  // sankey overlay in image space: left nodes x≈340, right nodes x≈1440, y range 135..640
  const r=rng(3);const L=[180,330,470,610].map((y,i)=>({x:340,y}));const Rn=[135,200,265,330,395,460,525,590,640].map(y=>({x:1440,y}));
  const mid={x:890,y:400};const reveal=seg(lt,.3,1.6);
  g.save();g.lineCap='round';
  for(let i=0;i<L.length;i++)for(let j=0;j<Rn.length;j++){if(r()>.55)continue;const a=L[i],b=Rn[j];const col=j<4?RED:j<7?BLUE:TEAL;g.strokeStyle=col;g.globalAlpha=.18*reveal;g.lineWidth=6+r()*10;
    g.beginPath();g.moveTo(a.x,a.y);g.bezierCurveTo(mid.x,a.y,mid.x,b.y,b.x,b.y);g.stroke();
    // particles
    for(let k=0;k<3;k++){const u=((lt*(.35+r()*.3))+k/3+r())%1;if(u>reveal)continue;const t=u;const x=Math.pow(1-t,3)*a.x+3*Math.pow(1-t,2)*t*mid.x+3*(1-t)*t*t*mid.x+t*t*t*b.x,y=Math.pow(1-t,3)*a.y+3*Math.pow(1-t,2)*t*a.y+3*(1-t)*t*t*b.y+t*t*t*b.y;g.globalAlpha=.95;g.fillStyle=col;g.beginPath();g.arc(x,y,5,0,7);g.fill()}}
  // central gate
  g.globalAlpha=reveal;g.fillStyle=RED;g.fillRect(mid.x-60,mid.y-120,120,240);g.fillStyle='#fff';g.font=`800 18px ${DISPLAY}`;g.textAlign='center';g.textBaseline='middle';g.fillText('AOM',mid.x,mid.y-20);g.fillText('GATE',mid.x,mid.y+4);
  g.restore();screenEnd();
  const rate=outQuint(seg(lt,.8,D*.7))*1248;bigStat(fmt(rate),'REQUESTS / MIN THROUGH THE GATE',W/2-120,180,seg(lt,.8,1.2),'#fff',120);
  const tp=seg(lt,D*.55,D*.55+.4);if(tp>0){const sc=lerp(2.5,1,outExpo(tp));g.save();g.translate(W/2-120,300);g.scale(sc,sc);tag('100% RE-AUTHORIZED  ·  0 DIRECT CALLS',0,0,RED,'#fff',40,28,clamp(tp*4));g.restore()}
  ticker(TT);lowerThird('LIVE TOPOLOGY','Agents → AOM → MCP tools & LLM providers · every edge is a policy decision',seg(lt,.15,.6))}
// G: the request flow (motion graphic)
const NODES=[['AGENT','sim-operator-14'],['AOM GATE','API key → role · RBAC'],['MEMORY','recall · 4 entries'],['CONTEXT CACHE','inventory snapshot'],['LLM CACHE','exact · semantic'],['PROVIDER','Claude / ChatGPT / Gemini']];
function G(lt,D){bg();drawLines(lt,.5);const n=NODES.length;const xs=NODES.map((_,i)=>200+i*(W-400)/(n-1));const y=H/2-40;
  slam('ONE REQUEST. EVERY STOP ACCOUNTED FOR.',W/2,120,seg(lt,0,.4),{size:84,ls:-3});
  // nodes
  for(let i=0;i<n;i++){const p=outBack(seg(lt,.3+i*.12,.7+i*.12));if(p<=0)continue;g.save();g.translate(xs[i],y);g.scale(p,p);const w=280,h=150;g.fillStyle=i===1?RED:'#17171C';g.strokeStyle=i===1?RED:'#3A3A42';g.lineWidth=3;g.fillRect(-w/2,-h/2,w,h);g.strokeRect(-w/2,-h/2,w,h);
    txt(NODES[i][0],0,-20,{size:32,weight:900,color:'#fff',ls:1});txt(NODES[i][1],0,26,{size:21,weight:500,color:i===1?'#FFD6D7':GREY});g.restore()}
  for(let i=0;i<n-1;i++){const p=seg(lt,.8+i*.12,1.0+i*.12);if(p<=0)continue;g.strokeStyle='#3A3A42';g.lineWidth=3;g.beginPath();g.moveTo(xs[i]+140,y);g.lineTo(lerp(xs[i]+140,xs[i+1]-140,outExpo(p)),y);g.stroke()}
  // runs: each run = (start, path of node indices with outcome labels)
  const runs=D>10?[[1.6,'MISS'],[D*.42,'SEMANTIC HIT'],[D*.68,'EXACT HIT']]:[[1.6,'MISS'],[D*.55,'HIT']];
  let tokens=0,ms=0;
  for(const [st,kind] of runs){const dur=kind==='MISS'?3.2:2.0;const u=seg(lt,st,st+dur);if(u<=0)continue;const last=kind==='MISS'?5:4;const pos=u*last;const i=Math.floor(pos);const frac=pos-i;const x=i>=last?xs[last]:lerp(xs[i],xs[Math.min(i+1,last)],frac);
    g.save();g.shadowColor=RED;g.shadowBlur=30;g.fillStyle=RED2;g.beginPath();g.arc(x,y+95,14,0,7);g.fill();g.restore();
    const labels=kind==='MISS'?['','allow · 3 ms','recall · 11 ms','miss → fetch 412 ms','miss','provider 2,140 ms']:kind==='SEMANTIC HIT'?['','allow · 3 ms','recall · 9 ms','hit · 2 ms','semantic hit 0.93 · 6 ms']:['','allow · 2 ms','recall · 8 ms','hit · 1 ms','exact hit · 1 ms'];
    for(let k=1;k<=Math.min(i,last);k++){const q=seg(pos,k,k+.4);const hit=/hit/.test(labels[k]),miss=/miss|provider/.test(labels[k]);tag(labels[k],xs[k],y+150,hit?GREEN:miss?'#2A0A0C':'#1E1E24',hit?'#fff':miss?RED2:'#C9C9D0',20,12,clamp(q*3))}
    if(u>=1){const res=kind==='MISS'?['2,566 ms','$0.0180']:kind==='SEMANTIC HIT'?['20 ms','$0.0000']:['12 ms','$0.0000'];const q=seg(lt,st+dur,st+dur+.4);
      g.save();g.translate(xs[last]+(kind==='MISS'?0:0),y-150);g.scale(outBack(q),outBack(q));tag(kind+'  ·  '+res[0]+'  ·  '+res[1],0,0,kind==='MISS'?'#2A0A0C':GREEN,kind==='MISS'?RED2:'#fff',30,20,clamp(q*3));g.restore()}}
  // summary at end
  const sp=seg(lt,D-1.6,D-1.2);if(sp>0){dim(.6*sp);txt('2,566 ms  →  12 ms',W/2,H/2-60,{size:150,ls:-8,color:'#fff',alpha:sp});txt('SAME ANSWER.  ZERO PROVIDER TOKENS.  FULL AUDIT TRAIL.',W/2,H/2+70,{size:40,weight:800,color:RED2,ls:4,alpha:sp})}
  grain(lt,.04)}
// H: Context Cache screen with animated chart
function H_(lt,D){const p=inOutQuint(clamp(lt/D));screenBegin('context',p,{x0:780,y0:360,z0:1.05,x1:640,y1:330,z1:1.35});
  // chart area ~ x250-980 y233-456 ; donut center (1085,323) r 95
  g.fillStyle='#fff';g.fillRect(252,236,728,218);barChart(270,245,700,200,R24,seg(lt,.6,2.4),RED);
  donut(1085,323,88,[[.94,GREEN],[.06,'#E5E5EA']],seg(lt,1.2,2.4),'94%','hit rate');
  // overwrite headline tiles
  const cp=seg(lt,.9,2.2);g.fillStyle='#fff';g.fillRect(1160,140,230,60);txt(fmt(outQuint(cp)*48912),1170,170,{size:46,weight:900,color:INK,align:'left',ls:-2});
  g.fillStyle='#fff';g.fillRect(1160,210,330,40);txt('lookups · last 24h',1170,228,{size:20,weight:500,color:GREY,align:'left'});
  screenEnd();
  bigStat('49.2k','CONTEXT ENTRIES CACHED',W-330,170,seg(lt,.5,.9),'#fff',110);
  bigStat('412 ms → 2 ms','PER LOOKUP',W-400,H-150,seg(lt,2.2,2.6),RED2,90);
  ticker(TT);lowerThird('CONTEXT CACHE','Catalogs, warehouse lookups, API responses — fetched once, served to every agent',seg(lt,.15,.6))}
// I: LLM Cache screen with animated chart
function I_(lt,D){const p=inOutQuint(clamp(lt/D));screenBegin('llm',p,{x0:780,y0:330,z0:1.05,x1:620,y1:300,z1:1.35});
  g.fillStyle='#fff';g.fillRect(252,205,728,195);lineChart(270,215,700,175,TOK,seg(lt,.6,2.6),RED);
  donut(1081,292,80,[[.41,GREEN],[.33,TEAL],[.26,'#E5E5EA']],seg(lt,1.2,2.4),'74%','hit rate');
  // replace 0% tile (approx x 980-1230, y 110-150)
  const cp=seg(lt,.9,2.2);g.fillStyle='#fff';g.fillRect(975,105,250,70);txt(Math.round(outQuint(cp)*74)+'%',985,140,{size:48,weight:900,color:INK,align:'left',ls:-2});
  screenEnd();
  bigStat('116.35M','TOKENS NEVER SENT',W-360,170,seg(lt,.5,.9),'#fff',110);
  bigStat('$110.89','SAVED · ONE INSTANCE',W-400,H-150,seg(lt,1.0,1.4),RED2,100);
  const hp=seg(lt,2.6,3.0);if(hp>0){const sc=lerp(2.5,1,outExpo(hp));g.save();g.translate(W/2+120,H-260);g.scale(sc,sc);tag('EXACT 41%  ·  SEMANTIC 33%  ·  MISS 26%',0,0,GREEN,'#fff',32,22,clamp(hp*4));g.restore()}
  ticker(TT);lowerThird('LLM CACHE','One endpoint for Claude, ChatGPT & Gemini · exact + vector-similarity hits · spend budgets',seg(lt,.15,.6))}
// J: scale beat — tokens
function J(lt,D){bg(PAPER);const [sx,sy]=shake(lt,lt<.25?10:0);g.save();g.translate(sx,sy);
  slam('THIS IS WHAT SCALE LOOKS LIKE.',W/2,150,seg(lt,0,.35),{size:110,ls:-5,color:INK});
  // log bar chart 2024/2025/2026
  const data=[[9.7e12,'2024','9.7 TRILLION'],[4.8e14,'2025','480 TRILLION'],[3.2e15,'2026','3.2 QUADRILLION']];const base=Math.log10(1e12),top=Math.log10(5e15);
  for(let i=0;i<3;i++){const p=outQuint(seg(lt,.5+i*.35,1.4+i*.35));if(p<=0)continue;const x=W/2+(i-1)*480;const hgt=(Math.log10(data[i][0])-base)/(top-base)*520*p;g.fillStyle=i===2?RED:'#2A2A32';g.fillRect(x-150,820-hgt,300,hgt);
    txt(data[i][1],x,870,{size:36,weight:800,color:INK,ls:4});txt(data[i][2],x,800-hgt,{size:i===2?54:40,weight:900,color:i===2?RED:INK,ls:-2,alpha:clamp(p*3)})}
  txt('TOKENS PER MONTH  ·  GOOGLE ALONE  ·  Sundar Pichai, I/O 2026',W/2,940,{size:28,weight:700,color:'#55555E',ls:4,alpha:seg(lt,1.4,1.8)});
  const tp=seg(lt,D-1.6,D-1.3);if(tp>0){const sc=lerp(2.5,1,outExpo(tp));g.save();g.translate(W/2,300);g.scale(sc,sc);tag('CACHE IT. OR PAY FOR IT TWICE.',0,0,RED,'#fff',60,36,clamp(tp*4));g.restore()}
  g.restore();wipe(seg(lt,D-.4,D),RED,1)}
// K: oversight quick cuts
function K(lt,D){const cuts=D>8?[['dashboard','DASHBOARD','servers, tools, calls and findings at a glance'],['servers','MCP SERVERS','registered & trusted · nothing else is reachable'],['catalog','TOOL CATALOG','RBAC + vector discovery · trust review · drift detection'],['threat','THREAT DETECTION','tool hijacking · poisoned descriptions · quarantine'],['audit','AUDIT LOG','append-only · every allow and deny · SIEM forwarding']]:[['threat','THREAT DETECTION','tool hijacking · poisoned descriptions · quarantine'],['audit','AUDIT LOG','append-only · every allow and deny · SIEM forwarding']];
  const each=D/cuts.length;const i=Math.min(cuts.length-1,Math.floor(lt/each));const l=lt-i*each;const [key,title,sub]=cuts[i];
  const p=inOutQuint(clamp(l/each));screenBegin(key,p,{x0:780,y0:300,z0:1.05,x1:820,y1:420,z1:1.22});screenEnd();
  ticker(TT);flashFrame(seg(l,0,.2),'#fff');lowerThird(title,sub,seg(l,.1,.5));
  const stamps=['EVERY DECISION ON THE RECORD','RBAC','TRACES','EVALS','APPROVALS','IDENTITY + OIDC'];const sp=seg(l,each*.5,each*.5+.3);if(sp>0&&i===cuts.length-1){const sc=lerp(2.5,1,outExpo(sp));g.save();g.translate(W/2+200,220);g.scale(sc,sc);tag(stamps[0],0,0,RED,'#fff',48,30,clamp(sp*4));g.restore()}
  if(i===0&&D>8){const r=rng(5);for(let k=1;k<6;k++){const q=outBack(seg(l,.6+k*.15,1.0+k*.15));if(q<=0)continue;g.save();g.translate(W/2+200+(k-3)*260,220);g.scale(q,q);tag(stamps[k],0,0,k%2?INK:RED,'#fff',30,20);g.restore()}}}
// L: deploy stamps
function L(lt,D){bg();drawLines(lt,.5);const k=D/3;const rows=[['SELF-HOSTED.',0],['COUCHBASE ENTERPRISE.',.3*k],['DOCKER COMPOSE OR HELM.',.6*k],['PYTHON SDK. CLAUDE · CHATGPT · GEMINI SKILLS.',.9*k]];
  for(let i=0;i<rows.length;i++){const p=seg(lt,rows[i][1],rows[i][1]+.3);if(p<=0)continue;const y=240+i*190;g.save();g.beginPath();g.rect(0,y-100,W,200);g.clip();const e=outExpo(p);g.fillStyle=i%2?PAPER:RED;g.fillRect(lerp(-W,0,e),y-80,W,160);txt(rows[i][0],W/2+lerp(-200,0,e),y,{size:i===3?74:110,ls:-4,color:i%2?INK:'#fff'});g.restore()}
  txt('five containers  ·  up in minutes  ·  bring your own cluster for production',W/2,990,{size:34,weight:600,color:GREY,alpha:seg(lt,1.3*k,1.7*k)});glitchSlices(lt,lt>D-.6?10:0,120,9);wipe(seg(lt,D-.45,D-.1),RED,1)}
// M: final lockup
function M(lt,D){bg(RED);for(let i=0;i<6;i++){const p=((lt*.55+i*.18)%1);g.strokeStyle='rgba(255,255,255,'+(.25*(1-p))+')';g.lineWidth=3;g.beginPath();g.arc(W/2,H/2-60,p*1400,0,7);g.stroke()}
  const lp=outBack(seg(lt,.05,.6));g.save();g.translate(W/2,H/2-230);g.scale(lp,lp);g.fillStyle='#fff';g.beginPath();g.arc(0,0,122,0,7);g.fill();logo(0,0,110);g.restore();
  const e1=seg(lt,.4,.85),e2=seg(lt,.55,1.0),e3=seg(lt,1.0,1.5);g.save();g.beginPath();g.rect(0,H/2-120,W,420);g.clip();
  txt('COUCHBASE',W/2,H/2-20+lerp(80,0,outExpo(e1)),{size:76,weight:800,color:'#FFD6D7',ls:14,alpha:clamp(e1*3)});txt('AGENT OPERATIONS MANAGER',W/2,H/2+100+lerp(120,0,outExpo(e2)),{size:116,ls:-4,color:'#fff',alpha:clamp(e2*3)});g.restore();
  txt('Govern every tool. Cache every model. Remember every user.',W/2,H/2+250,{size:44,weight:600,color:'#fff',alpha:e3});
  const bp=seg(lt,1.6,2.1);g.fillStyle='rgba(0,0,0,.35)';g.fillRect(0,H-120*outExpo(bp),W,120);txt('github.com/couchbaselabs/couchbase-agent-operations-manager',W/2,H-60,{size:34,weight:600,color:'#fff',font:MONO,alpha:bp});
  const f=seg(lt,D-.7,D);if(f>0){g.fillStyle='rgba(0,0,0,'+f+')';g.fillRect(0,0,W,H)}}
// P (120 only): the three failure modes
function P(lt,D){bg();drawLines(lt,.5);slam('THE DEFAULT MODEL IS "TRUST EVERYTHING."',W/2,140,seg(lt,0,.4),{size:78,ls:-3});
  const rows=[['UNAUTHORIZED CODE EXECUTION','a tool runs because the agent was pointed at it'],['HIDDEN PROMPT INJECTION','a tool description carries instructions the LLM treats as system text'],['OVER-PRIVILEGED ACCESS','any caller, any tool, any server — including destructive admin actions']];
  for(let i=0;i<3;i++){const p=outExpo(seg(lt,.5+i*.5,1.0+i*.5));if(p<=0)continue;const y=330+i*230;g.save();g.globalAlpha=p;g.translate(lerp(-200,0,p),0);g.fillStyle=RED;g.fillRect(160,y-70,16,140);txt('0'+(i+1),230,y,{size:90,weight:900,color:'#2A2A32',align:'left'});txt(rows[i][0],380,y-28,{size:54,weight:900,color:'#fff',align:'left',ls:-2});txt(rows[i][1],382,y+34,{size:30,weight:500,color:GREY,align:'left'});g.restore()}
  const sp=seg(lt,D-1.4,D-1.0);if(sp>0){dim(.75*sp);slam('AOM CLOSES ALL THREE.',W/2,H/2,sp,{size:170,ls:-8,color:RED2})}}
// Q (120 only): tool gate graphic (from the 30s cut)
function Q(lt,D){bg();drawLines(lt,.5);const cx=W/2,cy=H/2+60;const gp=outBack(seg(lt,.1,.6));g.save();g.translate(cx,cy);g.scale(gp,gp);g.fillStyle=RED;g.fillRect(-90,-170,180,340);g.fillStyle='#fff';g.font=`800 26px ${DISPLAY}`;g.textAlign='center';g.textBaseline='middle';g.fillText('RBAC',0,-40);g.fillText('+',0,0);g.fillText('VECTOR',0,40);g.restore();
  for(let i=0;i<7;i++){const y=cy-240+i*80;const p=outExpo(seg(lt,.3+i*.05,.8+i*.05));g.fillStyle='#D4D4DA';g.fillRect(lerp(-40,260,p),y-6,12,12);g.strokeStyle='#2A2A32';g.lineWidth=2;g.beginPath();g.moveTo(260,y);g.lineTo(cx-90,y);g.stroke();const q=(lt*1.6+i*.21)%1;if(p>.9){g.fillStyle=RED2;g.beginPath();g.arc(lerp(260,cx-90,q),y,6,0,7);g.fill()}
    const blocked=i===1||i===4||i===5;const p2=outExpo(seg(lt,.9+i*.05,1.3+i*.05));g.strokeStyle=blocked?'#3A1A1C':'#2A2A32';g.beginPath();g.moveTo(cx+90,y);g.lineTo(lerp(cx+90,W-260,p2),y);g.stroke();
    if(p2>.95){if(blocked){tag('DENIED',W-330,y,'#2A0A0C',RED2,20,10)}else{const q2=(lt*1.6+i*.37)%1;g.fillStyle='#fff';g.beginPath();g.arc(lerp(cx+90,W-260,q2),y,6,0,7);g.fill();g.fillStyle='#D4D4DA';g.fillRect(W-260,y-6,12,12)}}}
  txt('AGENTS',260,cy-320,{size:28,weight:700,color:GREY,ls:4});txt('MCP TOOLS',W-260,cy-320,{size:28,weight:700,color:GREY,ls:4});
  slam('DISCOVER. AUTHORIZE. PROXY.',W/2,150,seg(lt,.05,.4),{size:96,ls:-3});txt('Every tool call re-checked against Couchbase before it leaves the building.',W/2,250,{size:34,weight:500,color:GREY,alpha:seg(lt,.5,.9)});
  const hp=seg(lt,D*.6,D*.6+.2);if(hp>0){const sc=lerp(3,1,outExpo(hp));g.save();g.translate(W/2,H-110);g.scale(sc,sc);tag('⚠  TOOL HIJACKING DETECTED  ·  BLOCKED',0,0,RED,'#fff',44,30,clamp(hp*4));g.restore();if(lt<D*.6+.5)flashFrame(seg(lt,D*.6,D*.6+.3),RED)}grain(lt,.04)}
// S (120 only): memory consolidation numbers
function S_(lt,D){bg();const r=rng(77);const entries=['Prefers Customer 360 summary first','Session rollup · 18 entries → 1','Checks inventory before priority list','Semantic · importance 0.29','Consolidated from 996 earlier entries','Recalled 0× · superseded','Retail & E-Commerce copilot','User sim-operator-14'];
  g.save();g.font=`500 28px ${MONO}`;g.textBaseline='middle';g.textAlign='left';for(let i=0;i<22;i++){const sp=lt*(260+i*17);const x=(W+400-(sp+i*531)%(W+900));const y=70+i*48;g.fillStyle='#1A1A20';g.fillRect(x,y-20,520,40);g.fillStyle=i%5===0?RED2:'#4A4A55';g.fillText(entries[i%entries.length],x+14,y)}g.restore();dim(.78);
  slam('REMEMBERS',W/2,210,seg(lt,.05,.4),{size:180,ls:-8});slam('EVERY USER.',W/2,380,seg(lt,.2,.55),{size:150,ls:-6,color:RED2});
  const stats=[[54274,'MEMORY ENTRIES'],[54138,'SUPERSEDED'],[117,'CONSOLIDATED'],[29,'USERS']];for(let i=0;i<4;i++){const p=seg(lt,.7+i*.15,1.9+i*.15);if(p<=0)continue;const x=W/2+(i-1.5)*440;txt(fmt(outQuint(p)*stats[i][0]),x,640,{size:120,ls:-6,color:'#fff'});txt(stats[i][1],x,750,{size:28,weight:700,color:GREY,ls:6,alpha:clamp(p*3)})}
  txt('live from one AOM instance  ·  consolidation pass every 60 minutes',W/2,880,{size:34,weight:600,color:'#8A8A92',alpha:seg(lt,1.6,2.0)});grain(lt,.04);wipe(seg(lt,D-.4,D),PAPER,-1)}

const EDITIONS={
  60:[[A,3],[B,3],[C,2.5],[D_,3.5],[E_,3.5],[F,7],[G,8],[H_,6],[I_,6],[J,5],[K,4],[L,3],[M,5.5]],
  120:[[A,5],[B,5],[P,6],[C,4],[D_,6],[E_,6],[S_,4],[F,10],[G,16],[H_,9],[I_,9],[J,8],[Q,6],[K,14],[L,5],[M,7]]
};
let TL=[];function setEdition(n){let t=0;TL=EDITIONS[n].map(([fn,d])=>{const s=t;t+=d;return [s,t,fn,d]});return t}
let TT=0;function seek(t){TT=t;g.setTransform(1,0,0,1,0,0);g.globalAlpha=1;g.globalCompositeOperation='source-over';for(const [a,b,fn,d] of TL){if(t>=a&&t<b){fn(t-a,d);break}}
  const v=g.createRadialGradient(W/2,H/2,500,W/2,H/2,1300);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.35)');g.fillStyle=v;g.fillRect(0,0,W,H)}
window.seek=seek;window.setEdition=setEdition;
if(!navigator.webdriver){window.assetsReady.then(()=>{const total=setEdition(60);let st=performance.now();(function f(){seek(((performance.now()-st)/1000)%total);requestAnimationFrame(f)})()})}
