// AOM load generator — browser edition.
// Paste into the DevTools console of an AOM dashboard tab where you are signed in as admin
// (the admin session cookie is used to create roles and issue agent keys; agents then call
// the gateway with their own Bearer keys).
//
//   AOMLoad.setup()            -> creates 4 extra roles and issues 100 agents (keys kept in
//                                 window.AOMLoad.agents and localStorage 'aom_loadgen_agents')
//   AOMLoad.run(minutes, conc) -> drives discover / invoke / complete / memory / context traffic
//   AOMLoad.run(1440, 8, {diurnal:true}) -> 24h run paced by a day/night curve (realistic variable load)
//   AOMLoad.stop()             -> stops the run loop
//   AOMLoad.stats              -> live counters
//
// Re-running setup() reuses the agents saved in localStorage instead of issuing new keys.
(function(){
const BASE='';
const J=async(path,body,key)=>{const h={'Content-Type':'application/json'};if(key)h['Authorization']='Bearer '+key;
  const r=await fetch(BASE+path,{method:body?'POST':'GET',headers:h,body:body?JSON.stringify(body):undefined,credentials:'same-origin'});
  let j=null;try{j=await r.json()}catch(e){}return {ok:r.ok,status:r.status,j}};
const rnd=a=>a[Math.floor(Math.random()*a.length)];
const pick=(w)=>{let s=0;for(const x of w)s+=x[1];let r=Math.random()*s;for(const x of w){r-=x[1];if(r<=0)return x[0]}return w[w.length-1][0]};

const EXTRA_ROLES=[
 ['procurement_buyer','Procurement Buyer - PR and PO status, supplier pricing and oversupply checks.'],
 ['ops_engineer','Operations Engineer - warehouse and lakehouse health, read-only metrics.'],
 ['data_steward','Data Steward - catalog, lineage and table discovery across the warehouses.'],
 ['sales_assistant','Sales Assistant - customer 360 summaries, ticket lookups and order status.'],
];
// role -> [count, name stem, owner, description]
const PLAN={
 finance_analyst:[22,'Spend Analyst','Finance Operations','Supplier spend, price variance and savings narration across every org.'],
 support_agent:[22,'Helpdesk Copilot','Support Engineering','Answers how-do-I questions and tracks tickets for requesters.'],
 procurement_buyer:[18,'PO Status Agent','Procurement','Where is my PO? Oversupply and PO-readiness checks for buyers.'],
 ops_engineer:[12,'Warehouse Watch','Platform Team','Lakehouse and warehouse health, metrics and table freshness.'],
 data_steward:[10,'Catalog Steward','Data Platform Team','Table discovery, lineage and schema questions.'],
 sales_assistant:[12,'Account Copilot','Sales Enablement','Customer 360 summaries, open tickets and order status.'],
 admin:[4,'Ops Admin Agent','Platform Team','Scheduled admin checks and maintenance queries.'],
};
// what each role asks for (discover queries, tools it may invoke with sample args, prompts, context keys)
const Q={
 finance_analyst:{disc:['supplier pricing history from the data warehouse','business metrics to validate purchase order readiness','list warehouse tables for spend analysis'],
   inv:[['snowflake::query',{sql:'select supplier, avg(unit_price) from quotes group by supplier'}],['snowflake::get_metrics',{metric_types:['avg_savings_pct','total_spend'],start_date:'2026-09-01',end_date:'2026-10-01'}],['databricks-procurement::get_metrics',{metric_types:['avg_savings_pct'],start_date:'2026-09-01',end_date:'2026-10-01'}],['databricks-procurement::query',{sql:'select * from supplier_quotes limit 20'}],['bigquery-procurement::compare_suppliers',{item_id:'ITM-1042'}]],
   prompts:['Summarise the price variance for item ITM-1042 across the three incumbent suppliers.','Which supplier offers the best qualified price for pumps this quarter?','Explain the savings calculation on PR #70336 in two sentences.','Is PR #70689 ready to convert to a PO? Answer from the facts provided only.','List the top five categories by spend for Houston Plant 01.'],
   ctx:['supplier-quotes:ITM-1042','metrics:2026-09','org:H01:open-prs']},
 support_agent:{disc:['look up a customer\'s open support tickets','search issues about login failures','escalate a ticket to tier two'],
   inv:[['zendesk::search_tickets',{query:'login failure',status:'open'}],['zendesk::get_ticket',{ticket_id:'ZD-48213'}],['jira::search_issues',{jql:'project = OPS AND status = Open'}],['jira::get_issue',{issue_key:'OPS-4821'}],['web-search::fetch_page',{url:'https://example.com/kb/sso'}]],
   prompts:['How do I reset my SSO password for the Procurement Command Center?','Summarise ticket ZD-48213 for a handover note.','What are the three most common causes of a stuck approval in the Source-to-Pay flow?','Draft a two-line reply telling a requester their PR is awaiting budget approval.'],
   ctx:['kb:sso-reset','ticket:ZD-48213','faq:approvals']},
 procurement_buyer:{disc:['check purchase order status for a requester','inventory and open purchase orders across every org','supplier pricing for an item'],
   inv:[['databricks-procurement::query',{sql:'select status from purchase_orders where pr_id = 70336'}],['bigquery-procurement::list_items',{}],['snowflake-procurement::get_metrics',{metric_types:['open_prs'],start_date:'2026-10-01',end_date:'2026-10-18'}]],
   prompts:['Where is my PO for PR #70336?','Explain the oversupply signal on this PR. (ref #70334)','Is this PR PO-ready? (ref #70689)','Which open PRs for Odessa Field Yard can be fulfilled from existing inventory?'],
   ctx:['inventory:OFY','po:70336','prs:open:H01']},
 ops_engineer:{disc:['warehouse health metrics','list tables in the lakehouse','query recent load failures'],
   inv:[['databricks-procurement::list_tables',{}],['snowflake::list_tables',{database:'ANALYTICS'}],['databricks-procurement::get_metrics',{metric_types:['total_items'],start_date:'2026-10-17',end_date:'2026-10-18'}]],
   prompts:['Summarise the warehouse load health for the last 24 hours.','Which tables were refreshed in the last hour?','Explain what a stale partition means for the spend dashboard.'],
   ctx:['health:lakehouse','tables:analytics','freshness:supplier_quotes']},
 data_steward:{disc:['find tables containing supplier data','schema for the quotes table','lineage for revenue_daily'],
   inv:[['bigquery-procurement::list_tables',{}],['snowflake::list_tables',{database:'ANALYTICS'}],['databricks-procurement::list_items',{}]],
   prompts:['Which tables hold supplier master data?','Describe the columns of the supplier_quotes table.','What is the retention policy for transactions?'],
   ctx:['schema:supplier_quotes','lineage:revenue_daily','glossary:supplier']},
 sales_assistant:{disc:['customer 360 summary','open tickets for an account','order status for a customer'],
   inv:[['zendesk::search_tickets',{query:'Acme',status:'open'}],['jira::search_issues',{jql:'labels = acme'}],['web-search::fetch_page',{url:'https://example.com/acme'}]],
   prompts:['Give me a customer 360 summary for Acme Industrial.','What is my priority for today across my accounts?','Check inventory for the Acme renewal bundle.'],
   ctx:['c360:acme','orders:acme','priority:today']},
 admin:{disc:['manage warehouse users','warehouse maintenance'],
   inv:[['snowflake::list_tables',{database:'ANALYTICS'}],['snowflake::manage_users',{action:'list'}]],
   prompts:['List the maintenance tasks due this week.'],ctx:['maint:weekly']},
};

const L={agents:[],stats:{discover:0,invoke:0,complete:0,memory_add:0,memory_search:0,ctx_get:0,ctx_set:0,denied:0,errors:0,r429:0},running:false};

L.setup=async function(){
  const saved=localStorage.getItem('aom_loadgen_agents');
  if(saved){L.agents=JSON.parse(saved);console.log('reusing',L.agents.length,'saved agents');return L.agents.length}
  for(const [id,description] of EXTRA_ROLES){const r=await J('/v1/roles',{id,description});console.log('role',id,r.status)}
  let n=0;
  for(const [role,[count,stem,owner,desc]] of Object.entries(PLAN)){
    for(let i=1;i<=count;i++){
      const name=`${stem} ${String(i).padStart(2,'0')}`;
      const r=await J('/v1/agents',{name,role,owner,description:desc+' [loadgen]'});
      if(r.ok){L.agents.push({name,role,key:r.j.api_key,id:r.j.agent.agent_id||r.j.agent.id,user:`user-${role}-${i}`});n++}else console.warn('agent',name,r.status,r.j)}}
  localStorage.setItem('aom_loadgen_agents',JSON.stringify(L.agents));
  console.log('issued',n,'agents');return n};

async function step(a){
  const q=Q[a.role];const s=L.stats;
  const kind=pick([['discover',3],['invoke',4],['complete',5],['memory',2],['context',3]]);
  let r;
  try{
    if(kind==='discover'){r=await J('/v1/tools/discover',{query:rnd(q.disc),top_k:5},a.key);s.discover++}
    else if(kind==='invoke'){const [tool_id,args]=rnd(q.inv);r=await J('/v1/tools/invoke',{tool_id,arguments:args},a.key);s.invoke++;if(r.status===403)s.denied++}
    else if(kind==='complete'){const prompt=Math.random()<0.88?rnd(q.prompts):rnd(q.prompts)+` (ref #${70000+Math.floor(Math.random()*999)})`;r=await J('/v1/llm/complete',{prompt,namespace:a.role},a.key);s.complete++}
    else if(kind==='memory'){
      if(Math.random()<0.5){r=await J('/v1/memory',{user_id:a.user,content:`User asked: ${rnd(q.prompts)}`,session_id:`s-${Date.now()%7}`,memory_type:'conversational'},a.key);s.memory_add++}
      else{r=await J('/v1/memory/search',{user_id:a.user,query:'what does this user usually ask for?',top_k:5},a.key);s.memory_search++}}
    else{const key=rnd(q.ctx);r=await J('/v1/context/get',{key,namespace:a.role},a.key);s.ctx_get++;
      if(r.ok&&r.j&&r.j.hit===false){r=await J('/v1/context/set',{key,value:{fetched_at:Date.now(),rows:12,sample:'ok'},namespace:a.role,ttl_seconds:3600,source_latency_ms:180+Math.floor(Math.random()*600)},a.key);s.ctx_set++}}
    if(r&&r.status===429)s.r429++; else if(r&&!r.ok&&r.status!==403)s.errors++;
  }catch(e){s.errors++}
}

// Diurnal multiplier (local hour): quiet overnight, ramp from 07:00, lunch dip, afternoon peak, evening tail.
const DIURNAL=[.18,.14,.12,.12,.15,.22,.38,.62,.85,.95,1.0,.92,.72,.9,1.0,.96,.8,.62,.48,.4,.34,.3,.26,.22];
L.pace=(h=new Date().getHours())=>DIURNAL[h]*(0.85+Math.random()*0.3);
L.run=async function(minutes=20,concurrency=8,opts={}){
  const diurnal=!!opts.diurnal, think=opts.think_ms||1500; // think_ms = per-worker pause at full load
  if(!L.agents.length)await L.setup();
  L.running=true;const end=Date.now()+minutes*60000;
  const worker=async()=>{while(L.running&&Date.now()<end){
    // weight busy roles higher so the topology has real shape
    const role=pick([['finance_analyst',5],['support_agent',4],['procurement_buyer',4],['ops_engineer',2],['data_steward',1],['sales_assistant',2],['admin',1]]);
    const pool=L.agents.filter(x=>x.role===role);const a=rnd(pool);await step(a);if(diurnal)await new Promise(r=>setTimeout(r,think/Math.max(.05,L.pace())))}};
  await Promise.all(Array.from({length:concurrency},worker));L.running=false;console.log('done',L.stats);return L.stats};
L.stop=()=>{L.running=false};
window.AOMLoad=L;
})();
