#!/usr/bin/env python3
"""
Static dashboard generator for Hoboken Helo Accountability Tracker.

Reads flights.db and writes out-dir/index.html + out-dir/data/flights.json.
Safe to re-run: output files are always overwritten.

Usage:
    python dashboard.py [--db-path PATH] [--out-dir PATH]
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)

STATS_DAYS = 7
CHART_DAYS = 14
TABLE_DAYS = 30


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Queries ───────────────────────────────────────────────────────────────────

def query_stats(conn: sqlite3.Connection, since: str) -> dict:
    row = conn.execute("""
        SELECT
            COUNT(*)                              AS total_flights,
            COALESCE(SUM(is_kearny_departure), 0) AS kearny_departures,
            COALESCE(SUM(outside_hhi_hours), 0)   AS outside_hhi_hours,
            COALESCE(SUM(is_tour_operator), 0)    AS tour_operator_flights
        FROM flights
        WHERE started_at >= ? AND confidence = 'high' AND crossed_hoboken = 1
    """, (since,)).fetchone()
    return dict(row)


def query_daily(conn: sqlite3.Connection, since: str) -> list:
    rows = conn.execute("""
        SELECT date(started_at) AS day, COUNT(*) AS count
        FROM flights
        WHERE started_at >= ? AND confidence = 'high' AND crossed_hoboken = 1
        GROUP BY date(started_at)
        ORDER BY day
    """, (since,)).fetchall()
    by_day = {r["day"]: r["count"] for r in rows}
    start = datetime.now(timezone.utc).date() - timedelta(days=CHART_DAYS - 1)
    return [
        {"date": (start + timedelta(days=i)).isoformat(),
         "count": by_day.get((start + timedelta(days=i)).isoformat(), 0)}
        for i in range(CHART_DAYS)
    ]


def query_flights(conn: sqlite3.Connection,
                  since_table: str, since_stats: str) -> list:
    rows = conn.execute("""
        SELECT
            f.flight_id,
            f.icao_hex,
            COALESCE(f.n_number, a.n_number)       AS n_number,
            f.operator_flag,
            COALESCE(a.owner_name, '')              AS owner_name,
            COALESCE(a.model, '')                   AS model,
            f.started_at,
            f.ended_at,
            f.departure_heliport,
            f.arrival_heliport,
            f.min_alt_baro_ft,
            f.min_alt_over_hoboken_ft,
            f.time_in_hoboken_sec,
            f.crossed_hoboken,
            f.is_kearny_departure,
            f.outside_hhi_hours,
            f.is_tour_operator,
            f.confidence,
            f.track_summary
        FROM flights f
        LEFT JOIN aircraft a ON f.icao_hex = a.icao_hex
        WHERE f.started_at >= ?
          AND f.crossed_hoboken = 1
        ORDER BY f.started_at DESC
    """, (since_table,)).fetchall()

    out = []
    for r in rows:
        track = []
        if r["track_summary"]:
            try:
                track = json.loads(r["track_summary"]).get("coordinates", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        out.append({
            "flight_id":               r["flight_id"],
            "icao_hex":                r["icao_hex"],
            "n_number":                r["n_number"],
            "operator_flag":           r["operator_flag"],
            "owner_name":              r["owner_name"],
            "model":                   r["model"],
            "started_at":              r["started_at"],
            "ended_at":                r["ended_at"],
            "departure_heliport":      r["departure_heliport"],
            "arrival_heliport":        r["arrival_heliport"],
            "min_alt_baro_ft":         r["min_alt_baro_ft"],
            "min_alt_over_hoboken_ft": r["min_alt_over_hoboken_ft"],
            "time_in_hoboken_sec":     r["time_in_hoboken_sec"],
            "crossed_hoboken":         bool(r["crossed_hoboken"]),
            "is_kearny_departure":     bool(r["is_kearny_departure"]),
            "outside_hhi_hours":       bool(r["outside_hhi_hours"]),
            "is_tour_operator":        bool(r["is_tour_operator"]),
            "confidence":              r["confidence"],
            "track":                   track,
            "stats_window":            r["started_at"] >= since_stats,
        })
    return out


# ── HTML template ─────────────────────────────────────────────────────────────
# Leaflet 1.9.4 SRI hashes are from the official Leaflet quick-start docs.
# Chart.js 4.4.0 loaded without integrity attribute (hash not independently
# verified; add SRI via https://www.srihash.org/ if desired).

HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hoboken Helicopter Accountability Tracker</title>
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css"
      crossorigin="">
<style>
:root{--bg:#f4f5f7;--surface:#fff;--text:#1a1a1a;--muted:#666;--sh:#333;--hdr:#1a1a2e;--th:#f0f0f0;--th-hover:#e4e4e4;--td-border:#eee;--tr-hover:#fafbfc;--footer:#aaa;--shadow:0 1px 3px rgba(0,0,0,.08)}
body.dark{--bg:#0d1117;--surface:#161b22;--text:#e6edf3;--muted:#8b949e;--sh:#cdd5e0;--hdr:#010409;--th:#21262d;--th-hover:#2d333b;--td-border:#30363d;--tr-hover:#1c2128;--footer:#6e7681;--shadow:0 1px 3px rgba(0,0,0,.4)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;color:var(--text);background:var(--bg);transition:background .2s,color .2s}
header{background:var(--hdr);color:#fff;padding:1rem 2rem;display:flex;justify-content:space-between;align-items:flex-start}
header h1{font-size:1.4rem;font-weight:700}
.hdr-left{flex:1}
.updated{font-size:.78rem;opacity:.65;margin-top:.3rem}
.theme-btn{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);color:#fff;border-radius:5px;padding:.3rem .7rem;font-size:.75rem;cursor:pointer;white-space:nowrap;margin-left:1rem;flex-shrink:0;align-self:center}
.theme-btn:hover{background:rgba(255,255,255,.25)}
.container{max-width:1200px;margin:0 auto;padding:1rem 2rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1.5rem 0}
.card{background:var(--surface);border-radius:8px;padding:1.1rem;text-align:center;box-shadow:var(--shadow);transition:background .2s}
.card .val{font-size:2.2rem;font-weight:700;line-height:1}
.card .lbl{font-size:.72rem;color:var(--muted);margin-top:.35rem;line-height:1.3}
.card.hl .val{color:#c1121f}
.section{background:var(--surface);border-radius:8px;padding:1rem 1.25rem;margin:1rem 0;box-shadow:var(--shadow);transition:background .2s}
.sh{font-size:.9rem;font-weight:600;color:var(--sh);margin-bottom:.6rem}
#map{height:430px;border-radius:6px}
body.dark .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.85) contrast(1.05)}
body.dark .leaflet-container{background:#0d1117}
.legend{display:flex;flex-wrap:wrap;gap:.75rem;font-size:.75rem;margin-bottom:.6rem}
.legend span{display:flex;align-items:center;gap:.3rem}
.legend i{display:inline-block;width:22px;height:3px;border-radius:2px}
canvas{max-height:190px}
table{width:100%;border-collapse:collapse;font-size:.83rem}
th{background:var(--th);color:var(--text);padding:.55rem .7rem;text-align:left;font-weight:600;cursor:pointer;user-select:none;white-space:nowrap;transition:background .2s}
th:hover{background:var(--th-hover)}
td{padding:.45rem .7rem;border-top:1px solid var(--td-border);vertical-align:middle}
tr:hover td{background:var(--tr-hover)}
.b{display:inline-block;padding:.12rem .38rem;border-radius:4px;font-size:.7rem;font-weight:600}
.by{background:#fee2e2;color:#991b1b}
.bn{background:#f0fdf4;color:#166534}
.bl{background:#fef3c7;color:#92400e}
body.dark .by{background:#3d1212;color:#fca5a5}
body.dark .bn{background:#0d2818;color:#86efac}
body.dark .bl{background:#2d2008;color:#fde68a}
.no-flights{text-align:center;padding:2.5rem 1rem;color:var(--muted);font-size:.9rem}
footer{text-align:center;padding:2rem 1rem;font-size:.72rem;color:var(--footer);line-height:1.8}
footer a{color:var(--footer)}
@media(max-width:600px){header h1{font-size:1.1rem}#map{height:300px}.container{padding:.75rem 1rem}}
</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <h1>Hoboken Helicopter Accountability Tracker</h1>
    <div class="updated">Data updated: <span id="ts"></span></div>
  </div>
  <button class="theme-btn" id="theme-btn" onclick="toggleTheme()">Dark</button>
</header>
<div class="container">
  <div class="stats" id="stats"></div>

  <div class="section">
    <div class="sh">Flight tracks — last <span id="mlbl"></span> days</div>
    <div class="legend">
      <span><i style="background:#c1121f"></i>Kearny + Hoboken</span>
      <span><i style="background:#f4a261"></i>Crossed Hoboken</span>
      <span><i style="background:#457b9d"></i>Kearny departure</span>
      <span><i style="background:#bbb"></i>Other</span>
    </div>
    <div id="map"></div>
  </div>

  <div class="section">
    <div class="sh">High-confidence flights per day — last <span id="clbl"></span> days</div>
    <canvas id="chart"></canvas>
  </div>

  <div class="section">
    <div class="sh">All flights — last <span id="tlbl"></span> days</div>
    <div id="ft-wrap" style="overflow-x:auto">
    <table>
      <thead><tr>
        <th onclick="srt(0)">Date/Time ET ↕</th>
        <th onclick="srt(1)">N-Number ↕</th>
        <th onclick="srt(2)">Owner ↕</th>
        <th onclick="srt(3)">Route ↕</th>
        <th onclick="srt(4)">Min Alt ft ↕</th>
        <th onclick="srt(5)">Hoboken ↕</th>
        <th onclick="srt(6)">Hours OK ↕</th>
        <th onclick="srt(7)">Confidence ↕</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    </div>
    <div id="ft-empty" class="no-flights" style="display:none"></div>
  </div>
</div>
<footer>
  ADS-B data: <a href="https://adsb.fi" rel="noopener">adsb.fi</a> /
  <a href="https://adsb.lol" rel="noopener">adsb.lol</a> &middot;
  Registry: FAA ReleasableAircraft &middot;
  <a href="https://github.com/chmavo/hudson-helo-tracker" rel="noopener">Source code</a><br>
  Low-confidence flights shown in table but excluded from headline counts.
  Permitted HHI hours are approximate pending final zoning text.
</footer>

<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"
        crossorigin=""></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
        crossorigin="anonymous"></script>
<script>
const HOBOKEN=[[40.7330,-74.0415],[40.7590,-74.0380],[40.7590,-74.0225],[40.7490,-74.0235],[40.7330,-74.0285]];
const HELIPORTS={'65NJ':[40.7480,-74.1043],'JRB':[40.7012,-74.0090],'6N5':[40.7427,-73.9719],'JRA':[40.7541,-74.0080],'LDJ':[40.6173,-74.2447]};

// ── Theme ─────────────────────────────────────────────────────────────────────
(function(){
  var saved=localStorage.getItem('theme');
  var prefersDark=matchMedia('(prefers-color-scheme:dark)').matches;
  var dark=saved==='dark'||(saved===null&&prefersDark);
  if(dark)document.body.classList.add('dark');
  document.getElementById('theme-btn').textContent=dark?'Light':'Dark';
})();
function toggleTheme(){
  var dark=document.body.classList.toggle('dark');
  localStorage.setItem('theme',dark?'dark':'light');
  document.getElementById('theme-btn').textContent=dark?'Light':'Dark';
}

function etTime(iso){
  if(!iso)return'';
  return new Date(iso).toLocaleString('en-US',{timeZone:'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})+' ET';
}
function bdg(v,y,n){return v?`<span class="b by">${y}</span>`:`<span class="b bn">${n}</span>`}

const _sd={};
function srt(col){
  _sd[col]=!_sd[col];
  const tb=document.getElementById('tbody');
  [...tb.querySelectorAll('tr')].sort((a,b)=>{
    const av=a.children[col].dataset.v??a.children[col].textContent;
    const bv=b.children[col].dataset.v??b.children[col].textContent;
    return _sd[col]?av.localeCompare(bv,undefined,{numeric:true}):bv.localeCompare(av,undefined,{numeric:true});
  }).forEach(r=>tb.appendChild(r));
}

function render(d){
  document.getElementById('ts').textContent=d.generated_at;
  document.getElementById('mlbl').textContent=d.stats_days;
  document.getElementById('clbl').textContent=d.chart_days;
  document.getElementById('tlbl').textContent=d.table_days;

  // Stats cards
  const s=d.stats,sl=d.stats_days+'d';
  document.getElementById('stats').innerHTML=[
    {lbl:`Over Hoboken (${sl})`,      val:s.total_flights,     hl:s.total_flights>0},
    {lbl:`From Kearny (${sl})`,       val:s.kearny_departures, hl:s.kearny_departures>0},
    {lbl:`Outside HHI hours (${sl})`, val:s.outside_hhi_hours, hl:s.outside_hhi_hours>0},
  ].map(i=>`<div class="card${i.hl?' hl':''}"><div class="val">${i.val??'—'}</div><div class="lbl">${i.lbl}</div></div>`).join('');

  // Map
  const map=L.map('map').setView([40.735,-74.04],11);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',maxZoom:18
  }).addTo(map);
  L.polygon(HOBOKEN,{color:'#c1121f',weight:2,fillOpacity:0.07}).addTo(map);
  for(const[id,[lat,lon]]of Object.entries(HELIPORTS)){
    L.circleMarker([lat,lon],{radius:6,color:'#222',fillColor:'#555',fillOpacity:.85})
     .bindTooltip(id).addTo(map);
  }
  for(const f of d.flights){
    if(!f.stats_window||!f.track||f.track.length<2)continue;
    const ll=f.track.map(([lo,la])=>[la,lo]);
    let col='#bbb';
    if(f.crossed_hoboken&&f.is_kearny_departure)col='#c1121f';
    else if(f.crossed_hoboken)col='#f4a261';
    else if(f.is_kearny_departure)col='#457b9d';
    L.polyline(ll,{color:col,weight:f.confidence==='high'?2.5:1.5,opacity:f.confidence==='high'?.8:.35,dashArray:f.confidence==='low'?'5,5':null})
     .bindTooltip([f.n_number||f.icao_hex,f.started_at.slice(0,16).replace('T',' ')+' UTC',f.departure_heliport&&f.arrival_heliport?f.departure_heliport+'→'+f.arrival_heliport:''].filter(Boolean).join('\\n'))
     .addTo(map);
  }

  // Chart
  new Chart(document.getElementById('chart'),{
    type:'bar',
    data:{labels:d.daily.map(x=>x.date.slice(5)),datasets:[{label:'Flights',data:d.daily.map(x=>x.count),backgroundColor:'#457b9d',borderRadius:3}]},
    options:{responsive:true,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,ticks:{stepSize:1,precision:0}}}}
  });

  // Table
  if(d.flights.length){
    document.getElementById('ft-wrap').style.display='';
    document.getElementById('ft-empty').style.display='none';
    document.getElementById('tbody').innerHTML=d.flights.map(f=>{
      const route=[f.departure_heliport,f.arrival_heliport].filter(Boolean).join('→')||'—';
      const minAlt=f.crossed_hoboken&&f.min_alt_over_hoboken_ft!=null?Math.round(f.min_alt_over_hoboken_ft).toLocaleString():'—';
      const owner=f.operator_flag||(f.owner_name?(f.owner_name.length>30?f.owner_name.slice(0,30)+'…':f.owner_name):'—');
      return`<tr>
        <td data-v="${f.started_at}">${etTime(f.started_at)}</td>
        <td>${f.n_number||f.icao_hex}</td>
        <td title="${f.owner_name}">${owner}</td>
        <td>${route}</td>
        <td data-v="${f.min_alt_over_hoboken_ft??99999}">${minAlt}</td>
        <td>${bdg(f.crossed_hoboken,'Yes','No')}</td>
        <td>${bdg(!f.outside_hhi_hours,'Yes','No')}</td>
        <td>${f.confidence==='low'?'<span class="b bl">Low</span>':'High'}</td>
      </tr>`;
    }).join('');
  }else{
    document.getElementById('ft-wrap').style.display='none';
    document.getElementById('ft-empty').style.display='block';
    document.getElementById('ft-empty').textContent='No Hoboken overflights recorded in the last '+d.table_days+' days';
  }
}

fetch('data/flights.json').then(r=>{if(!r.ok)throw new Error(r.status);return r.json();}).then(render)
  .catch(e=>{document.querySelector('.container').innerHTML='<p style="padding:2rem;color:#c1121f">Failed to load data: '+e+'</p>';});
</script>
</body>
</html>
"""


# ── Builder ───────────────────────────────────────────────────────────────────

def build(db_path: Path, out_dir: Path) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    since_stats = _since(STATS_DAYS)
    since_chart = _since(CHART_DAYS)
    since_table = _since(TABLE_DAYS)

    stats   = query_stats(conn, since_stats)
    daily   = query_daily(conn, since_chart)
    flights = query_flights(conn, since_table, since_stats)
    conn.close()

    payload = {
        "generated_at": _now_iso(),
        "stats_days":   STATS_DAYS,
        "chart_days":   CHART_DAYS,
        "table_days":   TABLE_DAYS,
        "stats":        stats,
        "daily":        daily,
        "flights":      flights,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data").mkdir(exist_ok=True)

    json_path = out_dir / "data" / "flights.json"
    json_path.write_text(json.dumps(payload, separators=(",", ":")))
    log.info("wrote %s (%d bytes)", json_path, json_path.stat().st_size)

    html_path = out_dir / "index.html"
    html_path.write_text(HTML)
    log.info("wrote %s", html_path)

    log.info("dashboard built: %d flights | stats %dd | table %dd",
             len(flights), STATS_DAYS, TABLE_DAYS)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Hoboken Helo Accountability — dashboard builder")
    p.add_argument("--db-path", type=Path, default=Path("data-branch/flights.db"))
    p.add_argument("--out-dir", type=Path, default=Path("site"))
    args = p.parse_args()
    build(args.db_path, args.out_dir)


if __name__ == "__main__":
    main()
