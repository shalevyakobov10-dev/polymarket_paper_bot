"""Read-only browser control center for the Polymarket research/Paper service."""

CONTROL_CENTER_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Polymarket Bot Control Center</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c2f;--line:#26334d;--muted:#9aa9c3;--text:#edf3ff;--accent:#8fb7ff;--ok:#55d6a0;--warn:#f3c66d;--bad:#ff8585}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#17233c 0,#0b1020 55%);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 20px 48px}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:22px}
h1{font-size:clamp(25px,4vw,38px);line-height:1.12;margin:0 0 8px;letter-spacing:-.03em}
p{margin:0;color:var(--muted)}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.15em;color:var(--accent);font-weight:700;margin-bottom:8px}
button{background:#243958;color:var(--text);border:1px solid #466187;border-radius:10px;padding:10px 15px;font:inherit;font-weight:650;cursor:pointer}
button:hover{background:#2d466c}button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.banner{border:1px solid #705d33;background:#302817;color:#ffe1a0;border-radius:12px;padding:13px 16px;margin:18px 0 22px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}
.card{background:linear-gradient(145deg,rgba(24,35,57,.97),rgba(18,26,43,.97));border:1px solid var(--line);border-radius:15px;padding:18px;min-width:0;box-shadow:0 12px 34px rgba(0,0,0,.14)}
.card h2{font-size:17px;margin:0 0 12px}.status{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:99px;padding:4px 9px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-bottom:12px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--muted)}.healthy .dot{background:var(--ok)}.degraded .dot,.starting .dot,.restarting .dot{background:var(--warn)}.unhealthy .dot,.halted .dot,.error .dot,.stopped .dot{background:var(--bad)}
.rows{display:grid;grid-template-columns:minmax(110px,.8fr) minmax(0,1.4fr);gap:7px 12px;margin:0}
dt{font-size:12px;color:var(--muted)}dd{margin:0;min-width:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.gates{display:grid;gap:8px}.gate{display:flex;justify-content:space-between;gap:12px;border-top:1px solid var(--line);padding-top:8px;font-size:13px}.pass{color:var(--ok)}.blocked{color:var(--warn)}
.subhead{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:14px 0 8px}
.empty{color:var(--muted);font-size:13px}.updated{font-size:12px;color:var(--muted);margin-top:18px}
footer{margin-top:24px;color:var(--muted);font-size:12px}
@media(max-width:720px){main{padding:20px 13px 36px}header{flex-direction:column}.grid{grid-template-columns:1fr}.card{padding:15px}}
</style>
</head>
<body>
<main>
<header>
<div><div class="eyebrow">Operations · Research environment</div><h1>Polymarket Bot Control Center</h1><p>Read-only monitoring for market data, Paper engines, risk gates and service supervision.</p></div>
<button id="refresh" type="button">Refresh now</button>
</header>
<div class="banner"><strong>Paper / research only.</strong> This console has no trade controls and does not accept API keys, private keys or wallet secrets. Live order submission and signing remain disabled.</div>
<section class="grid" aria-live="polite">
<article class="card" id="card-system"><h2>System health</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-perps-market"><h2>Perps market data</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-perps-paper"><h2>Perps Paper engine</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-event-paper"><h2>Event Paper engine</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-release"><h2>Live release gates</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-supervisor"><h2>Service supervisor</h2><div class="empty">Loading status…</div></article>
</section>
<div class="updated" id="updated">Waiting for first refresh.</div>
<footer>Automatic refresh every 15 seconds while this page is visible. Data comes from this service’s sanitized read-only status endpoints.</footer>
</main>
<script>
"use strict";
const sources=[
 {id:"system",url:"/health",title:"System health"},
 {id:"perps-market",url:"/perps-market",title:"Perps market data"},
 {id:"perps-paper",url:"/perps-paper",title:"Perps Paper engine"},
 {id:"event-paper",url:"/event-paper",title:"Event Paper engine"},
 {id:"release",url:"/perps-live-gate",title:"Live release gates"},
 {id:"supervisor",url:"/service-supervisor",title:"Service supervisor"}
];
const byId=id=>document.getElementById("card-"+id);
function text(value){if(value===null||value===undefined||value==="")return "—";if(typeof value==="boolean")return value?"Yes":"No";if(Array.isArray(value))return value.length?value.join(", "):"None";if(typeof value==="object")return JSON.stringify(value);return String(value);}
function row(label,value){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=text(value);return [dt,dd];}
function addRows(container,items){const dl=document.createElement("dl");dl.className="rows";for(const item of items){const pair=row(item[0],item[1]);dl.append(pair[0],pair[1]);}container.append(dl);}
function statusClass(value){const s=String(value||"unknown").toLowerCase();if(["healthy","running","open","success"].includes(s))return "healthy";if(["degraded","starting","restarting","unknown","disabled","stale"].includes(s))return "degraded";if(["unhealthy","halted","error","stopped","failed"].includes(s))return "unhealthy";return "degraded";}
function addStatus(container,value){const node=document.createElement("div");node.className="status "+statusClass(value);const dot=document.createElement("span");dot.className="dot";const label=document.createElement("span");label.textContent=text(value||"unknown");node.append(dot,label);container.append(node);}
function metricEntries(id,d){
 if(id==="system"){
  const components=d.components||{};const failures=d.required_component_failures||[];
  const rows=[["Overall",d.status],["Required failures",failures.length?failures:"None"],["Last success",d.last_success_at],["Last error",d.last_error]];
  return {status:d.status||"unknown",rows,extra:Object.entries(components).map(([name,c])=>[name,c&&c.status||"unknown"])};
 }
 if(id==="perps-market")return {status:d.worker_state,rows:[["Enabled",d.enabled],["Markets observed",d.markets_in_last_snapshot],["Fee schedule verified",d.markets_with_verified_fee_schedule],["Fee coverage",d.fee_coverage_status],["Last success",d.last_success_at],["Last error",d.last_error]]};
 if(id==="perps-paper"){
  const p=d.portfolio||{};
  const rows=[["Enabled",d.enabled],["Worker state",d.worker_state],["Fee coverage",d.fee_coverage_status],["New Paper entries ready",d.fee_verified_entry_path_available],["Open positions",p.open_positions],["Closed positions",p.closed_positions],["Open notional (USD)",p.open_notional_usd],["Effective leverage",p.effective_leverage],["Peak drawdown (%)",p.peak_drawdown_pct],["UTC-day PnL (%)",p.daily_pnl_pct],["Halt reason",p.halt_reason||d.degraded_reason]];
  return {status:d.worker_state,rows};
 }
 if(id==="event-paper")return {status:d.worker_state,rows:[["Worker state",d.worker_state],["Heartbeat fresh",d.heartbeat_fresh],["Last success",d.last_success_at],["Consecutive failures",d.consecutive_failures],["Open positions",d.paper_portfolio&&d.paper_portfolio.open_positions],["Last error",d.last_error]]};
 if(id==="release"){
  const gates=d.release_gates||{};return {status:d.all_release_gates_passed?"healthy":"blocked",rows:[["All source gates passed",d.all_release_gates_passed],["Runtime flags passed",d.runtime_flags_passed],["Eligible for separate review",d.eligible_for_separate_gateway_review],["Order submission allowed",d.order_submission_allowed]],extra:Object.entries(gates).map(([name,value])=>[name,value&&value.passed===true])};
 }
 if(id==="supervisor")return {status:d.service_state,rows:[["Supervised",d.supervised],["Status fresh",d.status_fresh],["Service state",d.service_state],["Monitor state",d.monitor_state],["Event Paper worker",d.event_paper_worker_state],["Perps worker",d.perps_market_worker_state],["Restart count",d.restart_count],["Execution",d.execution]]};
 return {status:"unknown",rows:[]};
}
function draw(id,d){
 const card=byId(id);const title=sources.find(x=>x.id===id).title;card.replaceChildren();
 const h=document.createElement("h2");h.textContent=title;card.append(h);
 const model=metricEntries(id,d);addStatus(card,model.status);addRows(card,model.rows);
 if(model.extra&&model.extra.length){const sub=document.createElement("div");sub.className="subhead";sub.textContent=id==="release"?"Individual source-controlled gates":"Components";card.append(sub);if(id==="release"){const wrap=document.createElement("div");wrap.className="gates";for(const [name,passed] of model.extra){const line=document.createElement("div");line.className="gate";const left=document.createElement("span"),right=document.createElement("strong");left.textContent=name;right.textContent=passed?"PASS":"BLOCKED";right.className=passed?"pass":"blocked";line.append(left,right);wrap.append(line);}card.append(wrap);}else{const wrap=document.createElement("div");wrap.className="gates";for(const [name,value] of model.extra){const line=document.createElement("div");line.className="gate";const left=document.createElement("span"),right=document.createElement("strong");left.textContent=name;right.textContent=text(value);line.append(left,right);wrap.append(line);}card.append(wrap);}}
}
async function load(source){
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),6000);
 try{const response=await fetch(source.url,{cache:"no-store",headers:{"Accept":"application/json"},signal:controller.signal});let data={};try{data=await response.json();}catch(_){throw new Error("Invalid status response");}if(!response.ok&&!data.status)throw new Error("HTTP "+response.status);draw(source.id,data);}
 catch(_){const card=byId(source.id);const title=sources.find(x=>x.id===source.id).title;card.replaceChildren();const h=document.createElement("h2");h.textContent=title;card.append(h);addStatus(card,"unhealthy");const p=document.createElement("p");p.className="empty";p.textContent="Status endpoint unavailable. Check the service health and worker logs.";card.append(p);}
 finally{clearTimeout(timeout);}
}
let refreshing=false;
async function refresh(){if(refreshing||document.hidden)return;refreshing=true;const button=document.getElementById("refresh");button.disabled=true;try{await Promise.all(sources.map(load));document.getElementById("updated").textContent="Last updated: "+new Date().toLocaleString();}finally{refreshing=false;button.disabled=false;}}
document.getElementById("refresh").addEventListener("click",refresh);
refresh();setInterval(refresh,15000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
</script>
</body>
</html>"""
