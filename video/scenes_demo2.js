// ================= DEMO v2 — 18 chapters, 2x captures (shots2/*.jpg, 3456px wide) =================
const SHOTS={memory:'shots2/memory.jpg',memory_view:'shots2/memory_view.jpg',identities:'shots2/identities.jpg',servers:'shots2/servers.jpg',catalog:'shots2/catalog.jpg',knowledge:'shots2/knowledge.jpg',knowledge_models:'shots2/knowledge_models.jpg',policy:'shots2/policy.jpg',limits:'shots2/limits.jpg',llm:'shots2/llm.jpg',context:'shots2/context.jpg',traces:'shots2/traces.jpg',topology:'shots2/topology.jpg',audit:'shots2/audit.jpg',audit_forwarding:'shots2/audit_forwarding.jpg',sdk:'shots2/sdk.jpg',sdk_full:'shots2/sdk_full.jpg',agent_code:'shots2/agent_code.jpg',roles:'shots2/roles.jpg',threat:'shots2/threat.jpg',approvals:'shots2/approvals.jpg',evals:'shots2/evals.jpg',guardrails:'shots2/guardrails.jpg'};
const IMG={};
window.assetsReady=Promise.all(Object.entries(SHOTS).map(([k,src])=>new Promise(res=>{const im=new Image();im.onload=()=>{IMG[k]=im;res()};im.onerror=()=>res();im.src=src})));
const SIDEBAR_W=444, VIEW_H=1926; // capture px (2x CSS)

// camera: fy = capture-px y at canvas centre, z = zoom on top of fit-width (1 = fit 3456 -> 1920)
function screen(key,p,cam){const im=IMG[key]||IMG[cam.fallback];if(!im){bg(PAPER);return}
  const z=lerp(cam.z0,cam.z1,p),fy=lerp(cam.y0,cam.y1,p),fx=lerp(cam.x0??im.width/2,cam.x1??im.width/2,p);
  const S=(W/im.width)*z;let ox=W/2-fx*S,oy=H/2-fy*S;ox=clamp(ox,W-im.width*S,0);oy=clamp(oy,Math.min(0,H-im.height*S),0);
  g.save();g.fillStyle=PAPER;g.fillRect(0,0,W,H);g.drawImage(im,ox,oy,im.width*S,im.height*S);
  if(oy<-2){const sh=Math.min(VIEW_H,im.height);g.drawImage(im,0,0,SIDEBAR_W,sh,ox,0,SIDEBAR_W*S,sh*S);g.fillStyle='rgba(0,0,0,.06)';g.fillRect(ox+SIDEBAR_W*S-1,0,2,H)}
  g.restore();return {S,ox,oy}}
function box(x,y,w,h,p,T){if(p<=0||!T)return;g.save();g.globalAlpha=clamp(p);g.strokeStyle=RED;g.lineWidth=3;g.shadowColor='rgba(234,35,40,.45)';g.shadowBlur=14;g.strokeRect(T.ox+x*T.S,T.oy+y*T.S,w*T.S,h*T.S);g.restore()}
function cursor(x,y,click=0){g.save();g.translate(x,y);if(click>0&&click<1){g.strokeStyle=RED;g.lineWidth=3;g.globalAlpha=1-click;g.beginPath();g.arc(0,0,8+click*34,0,7);g.stroke();g.globalAlpha=1}
  g.fillStyle='#000';g.strokeStyle='#fff';g.lineWidth=2;g.beginPath();g.moveTo(0,0);g.lineTo(0,22);g.lineTo(6,17);g.lineTo(10,26);g.lineTo(14,24);g.lineTo(10,15);g.lineTo(17,15);g.closePath();g.fill();g.stroke();g.restore()}
function caption(idx,total,title,l1,l2,p){if(p<=0)return;const e=outExpo(clamp(p));g.save();g.globalAlpha=clamp(p*2);g.translate(0,lerp(60,0,e));
  const h=l2?186:146;g.fillStyle='rgba(11,11,13,.93)';g.fillRect(0,H-h-36,1240,h);g.fillStyle=RED;g.fillRect(0,H-h-36,14,h);
  txt(String(idx).padStart(2,'0')+' / '+total,60,H-h-36+36,{size:19,weight:700,color:RED2,align:'left',ls:4});
  txt(title,60,H-h-36+82,{size:44,weight:800,align:'left',color:'#fff',ls:-1});
  txt(l1,61,H-h-36+128,{size:23,weight:500,align:'left',color:'#D4D4DA'});if(l2)txt(l2,61,H-h-36+162,{size:23,weight:500,align:'left',color:'#D4D4DA'});g.restore()}
function dipIn(lt){const f=1-seg(lt,0,.3);if(f>0){g.fillStyle='rgba(11,11,13,'+f+')';g.fillRect(0,0,W,H)}}
function dipOut(lt,D){const f=seg(lt,D-.25,D);if(f>0){g.fillStyle='rgba(11,11,13,'+f+')';g.fillRect(0,0,W,H)}}
const TOP=972; // fy for the top of a page
const CH=[
 {title:'Agent Memory',l1:'Per-user memory with semantic recall; a consolidation pass runs every 60 minutes.',l2:'Roll-ups supersede their sources rather than deleting them; every entry can be edited or forgotten.',
  shots:[{key:'memory',d:4,cam:{y0:TOP,y1:TOP,z0:1,z1:1.04},boxes:[[506,210,2890,190,.15]],cursorTo:[1420,590]},{key:'memory_view',d:3,cam:{y0:TOP+120,y1:TOP+320,z0:1.04,z1:1.08,x0:2300,x1:2300},boxes:[[1700,920,1700,420,.2]],flash:true}]},
 {title:'Agent Identities',l1:'Agents are records, not strings in a file: owner, role, optional expiry, scoped tools.',l2:'Rotate with an overlap window, or revoke, without a restart; OIDC JWTs accepted.',
  shots:[{key:'identities',d:6,cam:{y0:TOP,y1:TOP+1500,z0:1,z1:1},boxes:[[506,300,2890,310,.12]]}]},
 {title:'MCP Servers',l1:'Only servers registered here and marked trusted ever have their tools ingested.',l2:'Nothing else is reachable through discovery or invoke, however well a query matches.',
  shots:[{key:'servers',d:5,cam:{y0:TOP,y1:TOP,z0:1,z1:1.06},boxes:[[2360,190,170,920,.2]]}]},
 {title:'Tool Catalog',l1:'Every ingested tool with its allowed roles, risk level and trust state.',l2:'One click quarantines a tool for every role.',
  shots:[{key:'catalog',d:5,cam:{y0:TOP,y1:TOP+900,z0:1,z1:1},boxes:[[2960,430,240,560,.2]]}]},
 {title:'Knowledge Base',l1:'Documents, or a Snowflake, Databricks or BigQuery table dump, imported and kept in sync on a schedule.',l2:'25 embedding models across 7 providers, or import your own; retrieval is RBAC + vector pre-filtered.',
  shots:[{key:'knowledge',d:3,cam:{y0:TOP,y1:TOP+120,z0:1,z1:1},boxes:[[1225,856,790,62,.15],[2022,856,756,62,.5]]},{key:'knowledge',d:2,cam:{y0:2950,y1:3000,z0:1,z1:1},boxes:[[590,2190,2804,70,.1]]},{key:'knowledge_models',d:4,cam:{y0:TOP+150,y1:TOP+250,z0:1,z1:1},boxes:[],fallback:'knowledge'}]},
 {title:'Providers & Cache Policy',l1:'Pick the model agents call, then decide when a cached answer stops counting as an answer.',l2:'Spend limits per agent, per role or globally — so no single agent can run away with the LLM budget.',
  shots:[{key:'policy',d:4,cam:{y0:TOP,y1:TOP,z0:1,z1:1.04},boxes:[[506,220,2890,170,.15]]},{key:'policy',d:4,cam:{y0:2950,y1:3050,z0:1,z1:1},boxes:[[636,2452,1346,112,.1],[636,2940,2712,70,.5]]}]},
 {title:'Limits & Budgets',l1:'Requests and tool calls per minute, tokens per hour, spend per day — per caller, with role exemptions.',l2:'Measure first, then enforce: usage is recorded against every ceiling for all agents.',
  shots:[{key:'limits',d:3.5,cam:{y0:1500,y1:1560,z0:1,z1:1},boxes:[[636,1480,1346,130,.1],[2002,1480,1346,130,.5]]},{key:'limits',d:3.5,cam:{y0:3000,y1:3100,z0:1,z1:1},boxes:[[590,2400,2804,70,.1]]}]},
 {title:'LLM Cache',l1:'One endpoint for Claude, ChatGPT and Gemini; exact-hash hits first, then vector similarity.',l2:'A day of traffic from all agents: quiet overnight, ramp from 07:00, peak through the morning.',
  shots:[{key:'llm',d:8,cam:{y0:TOP,y1:TOP+40,z0:1,z1:1.05},boxes:[[520,545,1760,500,.2]]}]},
 {title:'Context Cache',l1:'What agents fetched from their own data sources via context_get / context_set.',l2:'130k lookups at a 94% hit rate; 1 ms per hit versus a live round trip on a miss.',
  shots:[{key:'context',d:7,cam:{y0:TOP,y1:TOP+40,z0:1,z1:1.05},boxes:[[520,660,1760,480,.2]]}]},
 {title:'Traces',l1:'Every discover, invoke, completion and memory recall, grouped into the run it belonged to.',l2:'59,000 runs in the last 24 hours, with denied and errored spans called out.',
  shots:[{key:'traces',d:5,cam:{y0:TOP,y1:TOP+500,z0:1,z1:1},boxes:[[506,190,2890,170,.15]]}]},
 {title:'Live Topology',l1:'All agents on the left; MCP tools and LLM providers on the right; ribbons sized by 24-hour call volume.',l2:'Dashed nodes are registered but idle in the window.',
  shots:[{key:'topology',d:6,cam:{y0:TOP,y1:TOP,z0:1,z1:1.06},boxes:[]}]},
 {title:'Audit Log & Forwarding',l1:'Every discover and invoke decision, ALLOW or DENY, written append-only to Couchbase.',l2:'Forward each entry in real time to Splunk, Elastic, Sumo Logic, Sentinel and more.',
  shots:[{key:'audit_forwarding',d:7,cam:{y0:TOP,y1:TOP+700,z0:1,z1:1},boxes:[[506,170,2890,110,.15]],fallback:'audit'}]},
 {title:'Developer SDK & Skills',l1:'A typed Python client for discover, invoke, complete, memory and knowledge.',l2:'Claude, ChatGPT and Gemini skills apply the same integration to an existing agent codebase.',
  shots:[{key:'sdk',d:3,cam:{y0:TOP,y1:TOP+200,z0:1,z1:1},boxes:[[520,800,2860,620,.2]]},{key:'sdk_full',d:4,cam:{y0:10300,y1:10400,z0:1,z1:1},boxes:[[630,9650,2720,70,.15]],fallback:'agent_code'}]},
 {title:'Roles & RBAC',l1:'Every discover and invoke is authorised against roles stored in Couchbase.',l2:'Seven roles here; each authenticates with a bearer key, and only a hash is ever stored.',
  shots:[{key:'roles',d:5,cam:{y0:TOP,y1:TOP+800,z0:1,z1:1},boxes:[[506,190,2890,150,.15]]}]},
 {title:'Threat Detection',l1:'Tool descriptions are scanned at ingest and on a timer; live responses on every invoke.',l2:'docs-search::search_docs was auto-quarantined: instruction override, data exfiltration.',
  shots:[{key:'threat',d:5.5,cam:{y0:TOP,y1:TOP+100,z0:1,z1:1.05},boxes:[[506,1200,2890,330,.2]]}]},
 {title:'Approvals',l1:'RBAC says whether a role may ever call a tool; approvals say whether this call should happen now.',l2:'Critical tools park for a person: seven calls waiting, each bound to the exact arguments shown.',
  shots:[{key:'approvals',d:5.5,cam:{y0:TOP,y1:TOP+500,z0:1,z1:1},boxes:[[590,222,700,206,.1],[590,472,2804,60,.5]]}]},
 {title:'Evaluations',l1:'Saved trajectories replayed against the live gateway and scored against the previous run.',l2:'A change that made the agents worse shows up as a regression, not a number.',
  shots:[{key:'evals',d:5,cam:{y0:TOP,y1:TOP,z0:1,z1:1.05},boxes:[[506,190,2890,170,.15]]}]},
 {title:'Guardrails & PII',l1:'Email, cards, SSNs, IBANs, keys and tokens are redacted before anything is persisted.',l2:'Prompts and tool arguments are screened for injection; the caller still gets the original answer.',
  shots:[{key:'guardrails',d:6,cam:{y0:TOP,y1:TOP+700,z0:1,z1:1},boxes:[[590,222,2804,60,.1],[590,1500,2804,60,.65]]}]},
];
function title(lt,D){bg();drawLines(lt,.5);const lp=outExpo(seg(lt,.2,1.0));g.save();g.globalAlpha=lp;logo(W/2,H/2-150,100);g.restore();
  txt('Couchbase',W/2,H/2+10,{size:40,weight:600,color:GREY,ls:10,alpha:seg(lt,.6,1.1)});txt('Agent Operations Manager',W/2,H/2+90,{size:84,weight:800,color:'#fff',ls:-2,alpha:seg(lt,.8,1.3)});
  txt('Open Source GA walkthrough',W/2,H/2+190,{size:30,weight:500,color:RED2,ls:6,alpha:seg(lt,1.2,1.7)});dipOut(lt,D)}
function chapter(i){return function(lt,D){const c=CH[i];let t0=0;let shot=c.shots[c.shots.length-1],sl=lt,prev=null;
  for(let k=0;k<c.shots.length;k++){const s=c.shots[k];if(lt<t0+s.d){shot=s;sl=lt-t0;prev=c.shots[k-1]||null;break}t0+=s.d}
  const p=inOutQuint(clamp(sl/shot.d));const T=screen(shot.key,p,{...shot.cam,fallback:shot.fallback});
  for(const [x,y,w,h,at] of shot.boxes||[]){const q=seg(sl,at*shot.d,at*shot.d+.6);const fade=1-seg(sl,shot.d-.5,shot.d-.2);box(x,y,w,h,q*fade,T)}
  if(shot.cursorTo&&T){const cp=seg(sl,shot.d-1.6,shot.d-1.0);if(cp>0){const [cx,cy]=shot.cursorTo;cursor(lerp(W*.6,T.ox+cx*T.S,outQuint(cp)),lerp(H*.7,T.oy+cy*T.S,outQuint(cp)),seg(sl,shot.d-1.0,shot.d-.6))}}
  if(shot.flash&&prev){const f=1-seg(sl,0,.25);if(f>0){g.fillStyle='rgba(255,255,255,'+f*.9+')';g.fillRect(0,0,W,H)}}
  caption(i+1,CH.length,c.title,c.l1,c.l2,seg(lt,.3,.9)*(1-seg(lt,D-.4,D-.1)));dipIn(lt);dipOut(lt,D)}}
function end(lt,D){bg();drawLines(lt,.5);dipIn(lt);g.save();g.globalAlpha=seg(lt,.2,.7);logo(W/2,H/2-230,80);g.restore();
  txt('Couchbase Agent Operations Manager',W/2,H/2-90,{size:56,weight:800,color:'#fff',ls:-1,alpha:seg(lt,.3,.9)});
  txt('Open source  ·  Self-hosted on Couchbase Server Enterprise Edition  ·  Docker Compose or Helm',W/2,H/2-25,{size:26,weight:500,color:GREY,alpha:seg(lt,.6,1.1)});
  const p2=seg(lt,2.2,2.9);if(p2>0){g.save();g.globalAlpha=p2;g.fillStyle=RED;g.fillRect(W/2-440,H/2+40,880,3);g.restore();
    txt('Couchbase Agent Memory and Agent Catalog',W/2,H/2+100,{size:40,weight:800,color:'#fff',alpha:p2});
    txt('are officially licensed Couchbase AI Data Plane products.',W/2,H/2+150,{size:30,weight:500,color:'#D4D4DA',alpha:p2});
    txt('Reach out to your friends at Couchbase to learn more.',W/2,H/2+215,{size:30,weight:600,color:RED2,alpha:seg(lt,5.0,5.7)})}
  txt('github.com/couchbaselabs/couchbase-agent-operations-manager',W/2,H-90,{size:26,weight:600,color:GREY,font:MONO,alpha:seg(lt,.8,1.3)});
  const f=seg(lt,D-.8,D);if(f>0){g.fillStyle='rgba(0,0,0,'+f+')';g.fillRect(0,0,W,H)}}
let TL=[];function setEdition(){let t=0;const list=[[title,4],...CH.map((c,i)=>[chapter(i),c.shots.reduce((a,s)=>a+s.d,0)]),[end,10]];TL=list.map(([fn,d])=>{const s=t;t+=d;return [s,t,fn,d]});return t}
function seek(t){g.setTransform(1,0,0,1,0,0);g.globalAlpha=1;g.globalCompositeOperation='source-over';for(const [a,b,fn,d] of TL){if(t>=a&&t<b){fn(t-a,d);break}}
  const v=g.createRadialGradient(W/2,H/2,700,W/2,H/2,1400);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.18)');g.fillStyle=v;g.fillRect(0,0,W,H)}
window.seek=seek;window.setEdition=setEdition;window.CH=CH;
if(!navigator.webdriver){window.assetsReady.then(()=>{const total=setEdition();let st=performance.now();(function f(){seek(((performance.now()-st)/1000)%total);requestAnimationFrame(f)})()})}
