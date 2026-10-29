"""Synthesize per-chapter narration with Piper, fit each clip to its slot, and mix one WAV."""
import subprocess, wave, json, numpy as np, sys
VOICE='voices/en-gb-southern_english_female-low.onnx'
DUR=[7,7,7,7,7,8,8,8,7,8,8,9,7,7,6]
LINES=[
 (5,"Couchbase Agent Operations Manager: a control plane for A.I. agents."),
 (DUR[0],"The dashboard brings servers, tools, access decisions and the highest-severity findings together in one place."),
 (DUR[1],"Only servers registered here and marked trusted ever have their tools ingested."),
 (DUR[2],"The tool catalog lists every tool with its allowed roles, risk and trust state. One click quarantines a tool everywhere."),
 (DUR[3],"Every discover and invoke is authorised against roles stored in Couchbase. Each role uses a bearer key; only its hash is stored."),
 (DUR[4],"Agents are records: an owner, a role, an optional expiry. Rotate or revoke them without a restart."),
 (DUR[5],"Threat detection scans tool descriptions and live responses. Here, a search tool was auto-quarantined for an instruction override."),
 (DUR[6],"Live topology shows which agents are calling which tools and providers, sized by call volume over the last twenty-four hours."),
 (DUR[7],"Agent memory keeps per-user memories with semantic recall, and a consolidation pass runs every hour."),
 (DUR[8],"Roll-ups supersede their sources rather than deleting them, and every entry can be corrected or removed."),
 (DUR[9],"The knowledge base embeds documents, or a Snowflake, Databricks or BigQuery table dump, and keeps them in sync. Retrieval is filtered by the caller's role."),
 (DUR[10],"The context cache holds what agents fetched from their own data: a ninety-nine point six percent hit rate, one millisecond per hit."),
 (DUR[11],"The LLM cache fronts Claude, ChatGPT and Gemini: exact matches first, then vector similarity. Eighty-eight point eight percent are served from cache."),
 (DUR[12],"Providers and policy choose the model, the similarity threshold, and when a cached answer expires."),
 (DUR[13],"Every decision, allow or deny, is written append-only to the audit log, and can be forwarded to your SIEM."),
 (DUR[14],"A typed Python SDK wraps discover, invoke, complete, memory and knowledge."),
 (4,"Open source, self-hosted on Couchbase."),
]
SR=16000
def synth(text,length_scale=1.0):
    out=subprocess.run([sys.executable,'-m','piper','-m',VOICE,'--length-scale',str(length_scale),'-f','-'],input=text.encode(),capture_output=True)
    import io
    w=wave.open(io.BytesIO(out.stdout));assert w.getframerate()==SR,w.getframerate()
    return np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(np.float32)
total=sum(d for d,_ in LINES)
mix=np.zeros(int(total*SR),dtype=np.float32)
t=0.0;report=[]
for d,text in LINES:
    lead=0.5 if t==0 else 0.7
    clip=synth(text)
    avail=d-lead-0.4
    ls=1.0
    if len(clip)/SR>avail:
        ls=max(0.9,avail/(len(clip)/SR)); clip=synth(text,ls)
    if len(clip)/SR>avail:  # last resort: resample a touch faster
        n=int(avail*SR); clip=np.interp(np.linspace(0,len(clip)-1,n),np.arange(len(clip)),clip).astype(np.float32)
    start=int((t+lead)*SR); mix[start:start+len(clip)]+=clip
    report.append((round(t,1),d,round(len(clip)/SR,2),ls))
    t+=d
# gentle normalise
mix=mix/max(1,np.abs(mix).max())*0.85*32767
with wave.open('narration.wav','wb') as w:
    w.setnchannels(1);w.setsampwidth(2);w.setframerate(SR);w.writeframes(mix.astype(np.int16).tobytes())
for r in report: print(r)
print('total',total)
