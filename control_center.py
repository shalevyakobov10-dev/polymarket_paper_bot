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
<article class="card" id="card-news-intelligence"><h2>Global news intelligence</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-perps-account-supervisor"><h2>Perps account supervision</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-perps-paper"><h2>Perps Paper engine</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-event-paper"><h2>Event Paper engine</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-execution-readiness"><h2>Live execution readiness</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-event-release"><h2>Event-market live release gates</h2><div class="empty">Loading status…</div></article>
<article class="card" id="card-release"><h2>Perps live release gates</h2><div class="empty">Loading status…</div></article>
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
 {id:"news-intelligence",url:"/news",title:"Global news intelligence"},
 {id:"perps-account-supervisor",url:"/perps-account-supervisor",title:"Perps account supervision"},
 {id:"perps-paper",url:"/perps-paper",title:"Perps Paper engine"},
 {id:"event-paper",url:"/event-paper",title:"Event Paper engine"},
 {id:"execution-readiness",url:"/execution-readiness",title:"Live execution readiness"},
 {id:"event-release",url:"/event-live-gate",title:"Event-market live release gates"},
 {id:"release",url:"/perps-live-gate",title:"Perps live release gates"},
 {id:"supervisor",url:"/service-supervisor",title:"Service supervisor"}
];
const byId=id=>document.getElementById("card-"+id);
function text(value){if(value===null||value===undefined||value==="")return "—";if(typeof value==="boolean")return value?"Yes":"No";if(Array.isArray(value))return value.length?value.join(", "):"None";if(typeof value==="object")return JSON.stringify(value);return String(value);}
function row(label,value){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=text(value);return [dt,dd];}
function addRows(container,items){const dl=document.createElement("dl");dl.className="rows";for(const item of items){const pair=row(item[0],item[1]);dl.append(pair[0],pair[1]);}container.append(dl);}
function statusClass(value){const s=String(value||"unknown").toLowerCase();if(["healthy","running","open","success"].includes(s))return "healthy";if(["degraded","starting","restarting","unknown","disabled","stale","blocked"].includes(s))return "degraded";if(["unhealthy","halted","error","stopped","failed"].includes(s))return "unhealthy";return "degraded";}
function addStatus(container,value){const node=document.createElement("div");node.className="status "+statusClass(value);const dot=document.createElement("span");dot.className="dot";const label=document.createElement("span");label.textContent=text(value||"unknown");node.append(dot,label);container.append(node);}
function metricEntries(id,d){
 if(id==="system"){
  const components=d.components||{};const failures=d.required_component_failures||[];
  const backup=components.perps_paper_backup||{};
  const rows=[
   ["Overall",d.status],
   ["Required failures",failures.length?failures:"None"],
   ["Last success",d.last_success_at],
   ["Perps journal present",backup.perps_order_journal_present],
   ["Perps journal included in backup",backup.perps_order_journal_backed_up],
   ["Perps backup files",String(backup.backup_files_last_run??"—")+"/"+String(backup.backup_files_expected_last_run??"—")],
   ["Last error",d.last_error]
  ];
  return {status:d.status||"unknown",rows,extra:Object.entries(components).map(([name,c])=>[name,c&&c.status||"unknown"])};
 }
 if(id==="perps-market")return {status:d.worker_state,rows:[["Enabled",d.enabled],["Markets observed",d.markets_in_last_snapshot],["Fee schedule verified",d.markets_with_verified_fee_schedule],["Fee coverage",d.fee_coverage_status],["Last success",d.last_success_at],["Last error",d.last_error]]};
 if(id==="news-intelligence"){
  const g=d.gdelt||{};
  const rssHealthy=(d.feeds_succeeded||0)>0;
  const gdeltHealthy=(g.topics_succeeded||0)>0;
  const status=rssHealthy&&gdeltHealthy?"healthy":(rssHealthy||gdeltHealthy?"degraded":"stale");
  return {status,rows:[
   ["RSS last poll",d.last_poll_at],
   ["RSS last success",d.last_success_at],
   ["RSS feeds succeeded / configured",String(d.feeds_succeeded??0)+"/"+String(d.feeds_configured??0)],
   ["RSS feed failures",d.feeds_failed],
   ["Languages succeeded",d.languages_succeeded],
   ["Languages configured",d.languages_configured],
   ["Regions succeeded",d.regions_succeeded],
   ["Non-English headlines archived",d.non_english_items_added],
   ["Directional analysis languages",d.directional_analysis_languages],
   ["GDELT last success",g.last_success_at],
   ["GDELT topics succeeded / configured",String(g.topics_succeeded??0)+"/"+String(g.topics_configured??0)],
   ["GDELT topic failures",g.topics_failed],
   ["GDELT articles added / recent",String(g.items_added_last_poll??0)+"/"+String(g.items_total_recent??0)],
   ["GDELT languages seen",g.languages_seen],
   ["GDELT source countries seen",g.countries_seen],
   ["GDELT topic lanes represented",g.topic_categories_seen],
   ["Non-English GDELT articles archived",g.non_english_items_total_recent],
   ["GDELT last error",g.last_error]
  ]};
 }
 if(id==="perps-paper"){
  const p=d.portfolio||{};
  const rows=[["Enabled",d.enabled],["Worker state",d.worker_state],["Fee coverage",d.fee_coverage_status],["New Paper entries ready",d.fee_verified_entry_path_available],["Open positions",p.open_positions],["Closed positions",p.closed_positions],["Open notional (USD)",p.open_notional_usd],["Effective leverage",p.effective_leverage],["Peak drawdown (%)",p.peak_drawdown_pct],["UTC-day PnL (%)",p.daily_pnl_pct],["Halt reason",p.halt_reason||d.degraded_reason]];
  return {status:d.worker_state,rows};
 }
 if(id==="perps-account-supervisor"){
  const o=d.observations||{};
  return {status:d.worker_state,rows:[
   ["Enabled",d.enabled],
   ["PID-1 worker state",d.process_supervisor_state],
   ["PID-1 restart count",d.process_restart_count],
   ["Read-only preflight passed",d.read_only_preflight_passed],
   ["Read-only enforcement",d.read_only_enforcement],
   ["Venue enforces read-only credential scope",d.credential_scope_enforced_by_venue],
   ["Credential may sign write operations",d.credential_write_capability],
   ["OS process isolation verified",d.same_uid_process_isolation_verified],
   ["Heartbeat fresh",d.heartbeat_fresh],
   ["Heartbeat age (seconds)",d.heartbeat_age_seconds],
   ["Startup recovery complete",d.recovery_completed],
   ["Interrupted submissions recovered",d.recovered_interrupted_submissions],
   ["Unresolved local orders",o.unresolved_order_count],
   ["Unknown venue orders",o.unknown_remote_open_orders],
   ["Missing venue open orders",o.missing_remote_open_orders],
   ["Auto-Cancel deadline verified",o.auto_cancel_armed],
   ["Last preflight",d.last_preflight_at],
   ["Blocked reasons",d.blocked_reasons],
   ["Last error code",d.last_error_code],
   ["Order submission allowed",d.order_submission_allowed]
  ]};
 }
 if(id==="event-paper")return {status:d.worker_state,rows:[
  ["Worker state",d.worker_state],
  ["Heartbeat fresh",d.heartbeat_fresh],
  ["Last success",d.last_success_at],
  ["Open positions",d.portfolio&&d.portfolio.open_positions],
  ["Gamma candidates discovered",d.market_universe_discovered_last_cycle],
  ["Gamma pages attempted / succeeded",String(d.market_discovery_pages_attempted_last_cycle??0)+"/"+String(d.market_discovery_pages_succeeded_last_cycle??0)],
  ["Gamma pages failed",d.market_discovery_pages_failed_last_cycle],
  ["Gamma discovery complete",d.market_discovery_scan_complete_last_cycle],
  ["Markets selected for book checks",d.markets_selected_for_orderbooks_last_cycle],
  ["Fresh two-sided market snapshots",d.market_snapshots_usable_last_cycle],
  ["CLOB outcome-token book requests",d.market_orderbook_token_requests_last_cycle],
  ["Category quota",d.market_category_quota],
  ["Market categories represented",d.market_selected_categories_last_cycle],
  ["RSS data path",d.news_poll_status_available_last_cycle?"Direct poll":"Shared read-only archive"],
  ["RSS live poll succeeded / configured",d.news_poll_status_available_last_cycle?String(d.news_feeds_succeeded_last_cycle??0)+"/"+String(d.news_feeds_configured_last_cycle??0):"Not polled by this process"],
  ["RSS live poll failures",d.news_poll_status_available_last_cycle?d.news_feeds_failed_last_cycle:"Not polled"],
  ["RSS requests made by Paper worker",d.news_network_requests_made_last_cycle],
  ["RSS articles available in shared archive",d.news_archive_items_total_recent_last_cycle],
  ["GDELT data path",d.gdelt_poll_status_available_last_cycle?"Direct poll":"Shared read-only archive"],
  ["GDELT live poll succeeded / configured",d.gdelt_poll_status_available_last_cycle?String(d.gdelt_topics_succeeded_last_cycle??0)+"/"+String(d.gdelt_topics_configured_last_cycle??0):"Not polled by this process"],
  ["GDELT live poll failures",d.gdelt_poll_status_available_last_cycle?d.gdelt_topics_failed_last_cycle:"Not polled"],
  ["GDELT archive lanes represented / configured",String(d.gdelt_topics_seen_in_archive_count??0)+"/"+String(d.gdelt_topics_configured_last_cycle??0)],
  ["GDELT API requests made by Paper worker",d.gdelt_network_requests_made_last_cycle],
  ["GDELT articles available in shared archive",d.gdelt_items_total_recent_last_cycle],
  ["GDELT new records seen by Paper",d.gdelt_items_added_last_cycle],
  ["Articles considered / unique after URL fusion",String(d.news_items_considered_last_cycle??0)+"/"+String(d.news_unique_articles_last_cycle??0)],
  ["Distinct publishers in fused articles",d.news_distinct_publishers_last_cycle],
  ["News source types observed",d.news_source_types_seen],
  ["Articles with publisher publication time",d.news_articles_with_publisher_time_last_cycle],
  ["Index-only / unknown publication time",d.news_articles_with_index_time_only_last_cycle],
  ["Non-English articles kept unscored",d.news_non_english_items_unscored_last_cycle],
  ["Languages observed",d.news_languages_seen],
  ["Coverage tags (regions / countries)",d.news_regions_seen],
  ["GDELT error type",d.gdelt_error_type],
  ["Consecutive worker failures",d.consecutive_failures],
  ["Last error",d.last_error_type]
 ]};
 if(id==="execution-readiness"){
  const event=d.gateway_inventory&&d.gateway_inventory.event_contracts||{};
  const perps=d.gateway_inventory&&d.gateway_inventory.perps||{};
  const risk=d.shared_paper_risk||{};
  const policy=d.platform_eligibility_policy||{};
  const rows=[
   ["Overall readiness",d.status],
   ["Execution mode",d.mode],
   ["Event gateway present / integrated",String(!!event.adapter_code_present)+" / "+String(!!event.integrated_into_paper_worker)],
   ["Event order scope",String(event.supported_side||"—")+" "+String(event.supported_order_type||"—")],
   ["Event geoblock checked per order",event.per_order_geoblock_check],
   ["Perps gateway present / integrated",String(!!perps.adapter_code_present)+" / "+String(!!perps.integrated_into_strategy_worker)],
   ["Perps order scope",String(perps.supported_order_type||"—")+" "+String((perps.supported_time_in_force||[]).join("/"))],
   ["Perps geoblock checked per order",perps.per_order_geoblock_check],
   ["Shared admission wired to both engines",String(!!risk.wired_into_event_engine)+" / "+String(!!risk.wired_into_perps_engine)],
   ["Combined daily loss enforced",risk.combined_daily_loss_enforced],
   ["Combined drawdown enforced",risk.combined_drawdown_enforced],
   ["Runtime geoblock checked by this endpoint",policy.runtime_geoblock_checked_by_this_endpoint],
   ["Minimum platform age",policy.minimum_user_age_years],
   ["Live execution enabled",d.live_execution_enabled],
   ["Order submission allowed",d.order_submission_allowed]
  ];
  const blockers=(d.blockers||[]).map(item=>[item.code,item.detail]);
  return {status:d.status||"blocked",rows,extra:blockers};
 }
 if(id==="event-release"||id==="release"){
  const gates=d.release_gates||{};
  const rows=[["All source gates passed",d.all_release_gates_passed],["Eligible for separate review",d.eligible_for_separate_gateway_review],["Order submission allowed",d.order_submission_allowed]];
  if(id==="release")rows.splice(1,0,["Runtime flags passed",d.runtime_flags_passed]);
  return {status:d.all_release_gates_passed?"healthy":"blocked",rows,extra:Object.entries(gates).map(([name,value])=>[name,value&&value.passed===true])};
 }
 if(id==="supervisor")return {status:d.service_state,rows:[["Supervised",d.supervised],["Status fresh",d.status_fresh],["Service state",d.service_state],["Monitor state",d.monitor_state],["Event Paper worker",d.event_paper_worker_state],["Perps worker",d.perps_market_worker_state],["Perps account worker",d.perps_account_supervisor_state],["Perps account restarts",d.perps_account_supervisor_restart_count],["Restart count",d.restart_count],["Execution",d.execution]]};
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
