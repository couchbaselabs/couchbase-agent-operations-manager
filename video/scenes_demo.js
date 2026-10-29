// ================= DEMO (product walkthrough, no marketing) =================
const IMGW=1457,IMGH=812;
const SHOTS={dashboard:'shots/dashboard.jpg',servers:'shots/servers.jpg',catalog:'shots/catalog.jpg',roles:'shots/roles.jpg',identities:'shots/identities.jpg',threat:'shots/threat.jpg',topology:'shots/topology.jpg',memory:'shots/memory.jpg',memory_view:'shots/memory_view.jpg',knowledge:'shots/knowledge.jpg',context:'shots/context_cache.jpg',llm:'shots/llm_cache.jpg',policy:'shots/policy.jpg',audit:'shots/audit.jpg',sdk:'shots/sdk.jpg'};
const IMG={};
window.assetsReady=Promise.all(Object.entries(SHOTS).map(([k,src])=>new Promise(res=>{const im=new Image();im.onload=()=>{IMG[k]=im;res()};im.onerror=()=>res();im.src=src})));

function screenBegin(key,p,cam){const im=IMG[key];const z=lerp(cam.z0,cam.z1,p),fx=lerp(cam.x0,cam.x1,p),fy=lerp(cam.y0,cam.y1,p);
  const S=(H/IMGH)*z;let ox=W/2-fx*S,oy=H/2-fy*S;ox=clamp(ox,W-IMGW*S,0);oy=clamp(oy,H-IMGH*S,0);
  g.save();g.translate(ox,oy);g.scale(S,S);g.fillStyle=PAPER;g.fillRect(0,0,IMGW,IMGH);if(im)g.drawImage(im,0,0,IMGW,IMGH);return S}
function screenEnd(){g.restore()}
function box(x,y,w,h,p){if(p<=0)return;g.save();g.globalAlpha=clamp(p);g.strokeStyle=RED;g.lineWidth=3;g.shadowColor='rgba(234,35,40,.45)';g.shadowBlur=14;g.strokeRect(x,y,w,h);g.restore()}
function cursor(x,y,click=0){g.save();g.translate(x,y);if(click>0&&click<1){g.strokeStyle=RED;g.lineWidth=3;g.globalAlpha=1-click;g.beginPath();g.arc(0,0,8+click*34,0,7);g.stroke();g.globalAlpha=1}
  g.fillStyle='#000';g.strokeStyle='#fff';g.lineWidth=2;g.beginPath();g.moveTo(0,0);g.lineTo(0,22);g.lineTo(6,17);g.lineTo(10,26);g.lineTo(14,24);g.lineTo(10,15);g.lineTo(17,15);g.closePath();g.fill();g.stroke();g.restore()}
function caption(idx,total,title,l1,l2,p){if(p<=0)return;const e=outExpo(clamp(p));g.save();g.globalAlpha=clamp(p*2);g.translate(0,lerp(60,0,e));
  const h=l2?190:150;g.fillStyle='rgba(11,11,13,.93)';g.fillRect(0,H-h-40,1180,h);g.fillStyle=RED;g.fillRect(0,H-h-40,14,h);
  txt(String(idx).padStart(2,'0')+' / '+total,60,H-h-40+38,{size:20,weight:700,color:RED2,align:'left',ls:4,base:'middle'});
  txt(title,60,H-h-40+84,{size:46,weight:800,align:'left',color:'#fff',ls:-1});
  txt(l1,61,H-h-40+132,{size:24,weight:500,align:'left',color:'#D4D4DA'});if(l2)txt(l2,61,H-h-40+166,{size:24,weight:500,align:'left',color:'#D4D4DA'});
  g.restore()}
function dipIn(lt){const f=1-seg(lt,0,.35);if(f>0){g.fillStyle='rgba(11,11,13,'+f+')';g.fillRect(0,0,W,H)}}
function dipOut(lt,D){const f=seg(lt,D-.3,D);if(f>0){g.fillStyle='rgba(11,11,13,'+f+')';g.fillRect(0,0,W,H)}}

// chapter: {key,title,l1,l2,cam,boxes:[[x,y,w,h,atFrac]],extra}
const CH=[
 {key:'dashboard',title:'Dashboard',l1:'Servers, tools, access decisions and the highest-severity findings in one place.',l2:'833,041 access-log events in the last 24 hours; zero denied.',cam:{x0:728,y0:380,z0:1,x1:700,y1:330,z1:1.22},boxes:[[253,85,1174,86,.18],[253,465,1174,110,.55]]},
 {key:'servers',title:'MCP Servers',l1:'Only servers registered here and marked trusted ever have their tools ingested.',l2:'Nothing else is reachable through discovery or invoke, however well a query matches.',cam:{x0:728,y0:330,z0:1,x1:840,y1:320,z1:1.22},boxes:[[1003,96,75,455,.25]]},
 {key:'catalog',title:'Tool Catalog',l1:'Every ingested tool with its allowed roles, risk level and trust state.',l2:'Quarantine a tool with one click; it disappears from discovery for every role.',cam:{x0:728,y0:380,z0:1,x1:900,y1:340,z1:1.22},boxes:[[775,225,160,275,.22],[1275,225,95,275,.55]]},
 {key:'roles',title:'Roles & RBAC',l1:'Every discover and invoke is authorized against these roles in Couchbase.',l2:'Each role authenticates with a bearer API key; only a hash is ever stored.',cam:{x0:728,y0:380,z0:1,x1:700,y1:300,z1:1.2},boxes:[[253,95,1174,70,.18],[270,192,1140,45,.5]]},
 {key:'identities',title:'Agent Identities',l1:'Agents are records, not strings in a file: owner, role, optional end date and scope.',l2:'Rotate with an overlap window, or revoke, without a restart. OIDC JWTs accepted.',cam:{x0:728,y0:380,z0:1,x1:760,y1:300,z1:1.22},boxes:[[253,153,1174,152,.2],[1130,175,280,26,.55]]},
 {key:'threat',title:'Threat Detection',l1:'Tool descriptions are scanned at ingest and on a timer; live responses on every invoke.',l2:'docs-search::search_docs was auto-quarantined: instruction override, data exfiltration.',cam:{x0:728,y0:380,z0:1,x1:760,y1:470,z1:1.25},boxes:[[253,445,1174,155,.25],[555,485,650,90,.55]]},
 {key:'topology',title:'Live Topology',l1:'Which agent is calling which MCP tool or LLM provider, sized by 24-hour call volume.',l2:'Dashed nodes are registered but idle in the window.',cam:{x0:840,y0:430,z0:1.02,x1:840,y1:430,z1:1.15},boxes:[[280,165,145,530,.2],[1256,575,145,115,.55]]},
 {key:'memory',title:'Agent Memory',l1:'Per-user memory with semantic recall; a consolidation pass runs every 60 minutes.',l2:'29 users remembered across 54,274 entries on this instance.',cam:{x0:728,y0:380,z0:1,x1:600,y1:260,z1:1.25},boxes:[[253,110,1174,86,.2]],click:true},
 {key:'memory_view',title:'What the agent remembers',l1:'Rollups supersede their sources rather than deleting them; each entry can be edited or deleted.',l2:'Semantic search here is the same vector recall an agent gets, without needing its key.',cam:{x0:1137,y0:420,z0:1.2,x1:1137,y1:520,z1:1.45},boxes:[[849,448,577,178,.3]]},
 {key:'knowledge',title:'Knowledge Base',l1:'Documents, or a Snowflake, Databricks or BigQuery table dump, embedded into the same Couchbase cluster.',l2:'Import from S3, GCS, Azure or a URL and keep it in sync; retrieval is RBAC + vector pre-filtered.',cam:{x0:728,y0:380,z0:1,x1:700,y1:380,z1:1.25},boxes:[[253,97,1174,90,.12],[522,363,330,24,.38],[858,363,318,24,.62]]},
 {key:'context',title:'Context Cache',l1:'What agents cached from their own data sources via context_get / context_set.',l2:'54.5k lookups in 24h at a 99.6% hit rate; 1 ms per hit versus 8 ms per miss.',cam:{x0:728,y0:330,z0:1.02,x1:600,y1:320,z1:1.25},boxes:[[253,130,1174,86,.18],[253,236,676,242,.5]]},
 {key:'llm',title:'LLM Cache',l1:'One endpoint for Claude, ChatGPT and Gemini; exact-hash hits first, then vector similarity.',l2:'644,751 requests in 24h, 88.8% served from cache; 2 ms per hit versus 3,597 ms per miss.',cam:{x0:728,y0:330,z0:1.02,x1:600,y1:300,z1:1.25},boxes:[[253,85,1174,86,.18],[944,190,482,240,.5]]},
 {key:'policy',title:'Providers & Cache Policy',l1:'Pick the model agents call, then decide when a cached answer stops counting as an answer.',l2:'Similarity threshold, TTL, stale-while-revalidate, eviction and never-cache rules.',cam:{x0:728,y0:380,z0:1,x1:700,y1:420,z1:1.2},boxes:[[253,113,1174,82,.18],[253,460,1174,165,.55]]},
 {key:'audit',title:'Audit Log',l1:'Every discover and invoke decision, ALLOW or DENY, written append-only to Couchbase.',l2:'Including authentication failures; forward to up to six SIEM destinations.',cam:{x0:728,y0:380,z0:1,x1:840,y1:480,z1:1.22},boxes:[[1060,235,62,560,.25]]},
 {key:'sdk',title:'Developer SDK',l1:'A typed Python client for discover, invoke, complete, memory and knowledge.',l2:'discover only returns tools your role can invoke; both are re-checked against Couchbase.',cam:{x0:728,y0:400,z0:1,x1:840,y1:540,z1:1.22},boxes:[[270,408,1138,285,.25]]},
];
const DUR=[7,7,7,7,7,8,8,8,7,8,8,9,7,7,6]; // chapter durations (sum 111)
function title(lt,D){bg();drawLines(lt,.5);const lp=outExpo(seg(lt,.2,1.2));g.save();g.globalAlpha=lp;logo(W/2,H/2-150,100);g.restore();
  txt('Couchbase',W/2,H/2+10,{size:40,weight:600,color:GREY,ls:10,alpha:seg(lt,.8,1.4)});
  txt('Agent Operations Manager',W/2,H/2+90,{size:84,weight:800,color:'#fff',ls:-2,alpha:seg(lt,1.0,1.6)});
  txt('Open Source GA walkthrough',W/2,H/2+190,{size:30,weight:500,color:RED2,ls:6,alpha:seg(lt,1.6,2.2)});
  dipOut(lt,D)}
function chapter(i){return function(lt,D){const c=CH[i];const p=inOutQuint(clamp(lt/D));screenBegin(c.key,p,c.cam);
  for(const [x,y,w,h,at] of c.boxes){const q=seg(lt,at*D,at*D+.6);const fade=1-seg(lt,D-.6,D-.3);box(x,y,w,h,q*fade)}
  if(c.click){const cp=seg(lt,D-1.6,D-1.0);if(cp>0)cursor(lerp(900,712,outQuint(cp)),lerp(520,384,outQuint(cp)),seg(lt,D-1.0,D-.6))}
  screenEnd();
  caption(i+1,CH.length,c.title,c.l1,c.l2,seg(lt,.3,.9)*(1-seg(lt,D-.4,D-.1)));
  dipIn(lt);if(!c.click)dipOut(lt,D);else{const f=seg(lt,D-.25,D);if(f>0){g.fillStyle='rgba(255,255,255,'+f*.9+')';g.fillRect(0,0,W,H)}}}}
function end(lt,D){bg();drawLines(lt,.5);dipIn(lt);g.save();g.globalAlpha=seg(lt,.3,.9);logo(W/2,H/2-120,80);g.restore();
  txt('Couchbase Agent Operations Manager',W/2,H/2+20,{size:56,weight:800,color:'#fff',ls:-1,alpha:seg(lt,.5,1.1)});
  txt('Self-hosted on Couchbase Server Enterprise Edition  ·  Docker Compose or Helm',W/2,H/2+90,{size:28,weight:500,color:GREY,alpha:seg(lt,.9,1.4)});
  txt('github.com/couchbaselabs/couchbase-agent-operations-manager',W/2,H/2+160,{size:28,weight:600,color:RED2,font:MONO,alpha:seg(lt,1.1,1.6)});
  const f=seg(lt,D-.8,D);if(f>0){g.fillStyle='rgba(0,0,0,'+f+')';g.fillRect(0,0,W,H)}}

let TL=[];function setEdition(){let t=0;const list=[[title,5],...CH.map((_,i)=>[chapter(i),DUR[i]]),[end,4]];TL=list.map(([fn,d])=>{const s=t;t+=d;return [s,t,fn,d]});return t}
function seek(t){g.setTransform(1,0,0,1,0,0);g.globalAlpha=1;g.globalCompositeOperation='source-over';for(const [a,b,fn,d] of TL){if(t>=a&&t<b){fn(t-a,d);break}}
  const v=g.createRadialGradient(W/2,H/2,600,W/2,H/2,1300);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.25)');g.fillStyle=v;g.fillRect(0,0,W,H)}
window.seek=seek;window.setEdition=setEdition;
if(!navigator.webdriver){window.assetsReady.then(()=>{const total=setEdition();let st=performance.now();(function f(){seek(((performance.now()-st)/1000)%total);requestAnimationFrame(f)})()})}
