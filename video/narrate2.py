"""Narration for the v2 walkthrough (18 chapters). Slots must match scenes_demo2.js shot durations."""
import subprocess, wave, io, numpy as np, sys
VOICE='voices/en-gb-southern_english_female-low.onnx'
LINES=[
 (4,"Couchbase Agent Operations Manager."),
 (7,"Agent memory: per-user memories with semantic recall, consolidated every hour, and correctable by an operator."),
 (6,"Agent identities: an owner, a role, an optional expiry, and a key you can rotate or revoke."),
 (5,"Only MCP servers registered here and marked trusted ever have their tools ingested."),
 (5,"The tool catalog: every tool, its roles and risk; one click quarantines."),
 (9,"The knowledge base ingests documents or a Snowflake, Databricks or BigQuery dump and keeps it in sync, with twenty-five embedding models or your own."),
 (8,"Providers and policy set the model, the cache rules, and spend limits per agent, role or globally, so no agent runs away with the budget."),
 (7,"Limits and budgets measure every caller: requests, tool calls, tokens and spend, for all agents."),
 (8,"LLM caching answers repeat questions from Couchbase: exact matches first, then vector similarity, across a day of traffic."),
 (7,"Context caching does the same for data agents fetch: ninety-four percent hits, one millisecond each."),
 (5,"Every discover, invoke, completion and recall is traced and grouped into its run."),
 (6,"Live topology shows all agents and which tools and providers they call, sized by volume."),
 (7,"Every decision is written append-only to the audit log, and forwarded to Splunk, Elastic, Sentinel and more."),
 (7,"A Python SDK wraps the gateway; Claude, ChatGPT and Gemini skills onboard existing agents in minutes."),
 (5,"Roles authorise every call; seven roles, each with its own bearer key."),
 (5.5,"Threat detection scans tool descriptions and responses, and quarantined a poisoned search tool automatically."),
 (5.5,"Critical tools can park for a human decision: seven calls are waiting on a person right now."),
 (5,"Evaluations replay saved trajectories and flag regressions."),
 (6,"Guardrails redact P.I.I. before anything is persisted, and screen prompts for injection."),
 (10,"Open source, on Couchbase. Couchbase Agent Memory and Agent Catalog are officially licensed Couchbase A.I. Data Plane products. Reach out to your friends at Couchbase to learn more."),
]
SR=16000
def synth(text,ls=1.0):
    out=subprocess.run([sys.executable,'-m','piper','-m',VOICE,'--length-scale',str(ls),'-f','-'],input=text.encode(),capture_output=True)
    w=wave.open(io.BytesIO(out.stdout));assert w.getframerate()==SR
    return np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(np.float32)
total=sum(d for d,_ in LINES);mix=np.zeros(int(total*SR),dtype=np.float32);t=0.0
for d,text in LINES:
    lead=0.5 if t==0 else 0.6;avail=d-lead-0.1;clip=synth(text);ls=1.0
    if len(clip)/SR>avail: ls=max(0.8,avail/(len(clip)/SR));clip=synth(text,ls)
    start=int((t+lead)*SR);n=min(len(clip),len(mix)-start);mix[start:start+n]+=clip[:n];print(round(t,1),d,round(len(clip)/SR,2),round(ls,2));t+=d
mix=mix/max(1,np.abs(mix).max())*0.85*32767
with wave.open('narration2.wav','wb') as w: w.setnchannels(1);w.setsampwidth(2);w.setframerate(SR);w.writeframes(mix.astype(np.int16).tobytes())
print('total',total)
