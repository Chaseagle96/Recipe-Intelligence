from __future__ import annotations

import json
from pathlib import Path

from .app_feed import write_app_feed
from .runtime import vertical_name


def _dashboard_html() -> str:
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recipe Intelligence — Air Fryer Rankings</title>
<style>
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color-scheme:light dark}
body{max-width:1450px;margin:0 auto;padding:1rem 1.25rem;line-height:1.45}
header{display:flex;gap:1rem;align-items:flex-end;justify-content:space-between;flex-wrap:wrap}
h1{margin-bottom:.25rem}.muted{opacity:.72}.controls{display:flex;gap:.5rem;flex-wrap:wrap;margin:1rem 0}
input,select{font:inherit;padding:.55rem .7rem;min-width:13rem}table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:.5rem;border-bottom:1px solid currentColor;vertical-align:top}th{position:sticky;top:0;background:Canvas;z-index:1}
a{color:LinkText}.score{font-variant-numeric:tabular-nums}.badge{white-space:nowrap}.grade{font-weight:700}.range{white-space:nowrap}details{margin:.25rem 0}details.provenance{font-size:.82rem;max-width:38rem}footer{margin-top:2rem}.sr{position:absolute;left:-10000px}
@media(max-width:900px){table{font-size:.80rem}th:nth-child(5),td:nth-child(5),th:nth-child(10),td:nth-child(10),th:nth-child(11),td:nth-child(11){display:none}}
</style>
</head>
<body>
<header><div><h1>Recipe Intelligence</h1><div class="muted">Air Fryer Rankings</div><div class="muted" id="generated">Loading…</div></div><div class="muted">Evidence-aware hierarchical Bayesian ranking with uncertainty and robustness</div></header>
<div class="controls">
<label><span class="sr">Search recipes</span><input id="search" type="search" placeholder="Search recipes or publishers"></label>
<label><span class="sr">Category</span><select id="category"><option value="">All categories</option></select></label>
<label><span class="sr">Minimum evidence confidence</span><select id="confidence"><option value="0">Any evidence confidence</option><option value="0.65">≥ 0.65</option><option value="0.8">≥ 0.80</option><option value="0.95">≥ 0.95</option></select></label>
<label><span class="sr">Minimum rank confidence</span><select id="rankconfidence"><option value="0">Any rank confidence</option><option value="0.7">≥ 70%</option><option value="0.85">≥ 85%</option><option value="0.95">≥ 95%</option></select></label>
</div>
<div id="stats" class="muted"></div>
<table aria-describedby="stats"><thead><tr><th>Rank</th><th>Recipe</th><th>Publisher</th><th>Score</th><th>Stars</th><th>Ratings</th><th>Evidence</th><th>Rank confidence</th><th>Likely rank</th><th>7d growth</th><th>Velocity/day</th><th>Movement</th></tr></thead><tbody id="rows"></tbody></table>
<details><summary>Methodology and model diagnostics</summary><pre id="methodology" style="white-space:pre-wrap"></pre></details>
<footer class="muted">Recipe Intelligence · Air Fryer vertical. Data is automatically refreshed from public recipe pages. Rankings are statistical estimates with explicit uncertainty, not taste-test judgments.</footer>
<script>
let DATA={leaderboard:[],categories:[],methodology:{}};
const esc=s=>String(s??"").replace(/[&<>\"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}[c]));
function pct(v){return v==null?'—':`${(Number(v)*100).toFixed(0)}%`}
function render(){
 const q=document.querySelector('#search').value.trim().toLowerCase();
 const cat=document.querySelector('#category').value;
 const min=Number(document.querySelector('#confidence').value||0);
 const rankMin=Number(document.querySelector('#rankconfidence').value||0);
 const rows=DATA.leaderboard.filter(r=>(!q||(`${r.title} ${r.source} ${r.combined_sources}`).toLowerCase().includes(q)))&&(!cat||(r.categories||'').split(' | ').includes(cat))&&Number(r.evidence_confidence||0)>=min&&Number(r.rank_confidence||0)>=rankMin);
 document.querySelector('#stats').textContent=`Showing ${rows.length} of ${DATA.leaderboard.length} ranked recipes`;
 document.querySelector('#rows').innerHTML=rows.map(r=>`<tr><td>${r.rank}</td><td><a href="${esc(r.url)}" rel="noopener noreferrer">${esc(r.title)}</a><br><small>${esc(r.categories||'')}</small>${r.rank_provenance?`<details class="provenance"><summary>Why this rank?</summary>${esc(r.rank_provenance)}</details>`:''}</td><td>${esc(r.combined_sources||r.source)}</td><td class="score">${Number(r.hierarchical_score).toFixed(4)}</td><td>${Number(r.rating).toFixed(2)}</td><td>${Number(r.rating_count).toLocaleString()}</td><td class="badge"><span class="grade">${esc(r.evidence_grade||'—')}</span><br><small>${Number(r.evidence_confidence||0).toFixed(2)} ${esc(r.evidence_status||'')}</small></td><td>${pct(r.rank_confidence)}</td><td class="range">${r.rank_range_low==null?'—':`${r.rank_range_low}–${r.rank_range_high}`}</td><td>${r.review_growth_7d==null?'—':Number(r.review_growth_7d).toLocaleString()}</td><td>${r.review_velocity_per_day==null?'—':Number(r.review_velocity_per_day).toFixed(1)}</td><td>${r.movement==null?'New':(r.movement>0?'▲ '+r.movement:r.movement<0?'▼ '+Math.abs(r.movement):'—')}</td></tr>`).join('');
}
fetch('data.json',{cache:'no-store'}).then(r=>r.json()).then(data=>{
 DATA=data;
 document.querySelector('#generated').textContent=`Generated ${data.generated_at} · ${data.source_count} configured publishers`;
 const categories=[...new Set(data.leaderboard.flatMap(r=>(r.categories||'').split(' | ').filter(Boolean)))].sort();
 document.querySelector('#category').innerHTML='<option value="">All categories</option>'+categories.map(c=>`<option>${esc(c)}</option>`).join('');
 document.querySelector('#methodology').textContent=JSON.stringify(data.methodology,null,2);
 render();
}).catch(e=>{document.querySelector('#generated').textContent='Could not load leaderboard data';document.querySelector('#stats').textContent=String(e)});
for(const id of ['search','category','confidence','rankconfidence'])document.querySelector('#'+id).addEventListener('input',render);
</script>
</body></html>"""
    return html.replace("Air Fryer", vertical_name())


def _existing_normalized_corpus(docs_dir: str | Path) -> tuple[list[dict] | None, int | None]:
    """Read already-crawled clean state for production mobile publication.

    Production runs call write_dashboard with the relative `docs` directory. Tests
    and alternate report destinations do not implicitly read repository state.
    This is deliberately a local projection only: no discovery or network request
    occurs here, so publishing the corpus cannot perturb ranking or crawl history.
    """
    if Path(docs_dir) != Path("docs"):
        return None, None
    state_path = Path("data/state.json")
    if not state_path.exists():
        return None, None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    recipes = state.get("recipes", {}) if isinstance(state, dict) else {}
    catalog = state.get("url_catalog", {}) if isinstance(state, dict) else {}
    if not isinstance(recipes, dict):
        return None, len(catalog) if isinstance(catalog, dict) else None
    return [dict(row) for row in recipes.values() if isinstance(row, dict)], len(catalog) if isinstance(
        catalog, dict
    ) else None


def write_dashboard(
    docs_dir: str | Path,
    generated_at: str,
    ranked: list[dict],
    reliability: list[dict],
    anomalies: list[dict],
    methodology: dict,
    source_count: int,
    *,
    corpus: list[dict] | None = None,
    duplicate_groups: list[dict] | None = None,
    stale_days: int = 14,
    catalog_url_count: int | None = None,
) -> None:
    docs = Path(docs_dir)
    docs.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at,
        "vertical": vertical_name(),
        "source_count": source_count,
        "leaderboard": ranked[:500],
        "source_reliability": reliability,
        "anomalies": anomalies[:100],
        "methodology": methodology,
    }
    (docs / "data.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (docs / "index.html").write_text(_dashboard_html(), encoding="utf-8")
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    if corpus is None:
        existing_corpus, existing_catalog_count = _existing_normalized_corpus(docs_dir)
        corpus = existing_corpus
        if catalog_url_count is None:
            catalog_url_count = existing_catalog_count
    effective_stale_days = int(methodology.get("stale_days") or stale_days)
    write_app_feed(
        docs,
        generated_at,
        ranked,
        source_count,
        corpus=corpus,
        duplicate_groups=duplicate_groups,
        stale_days=effective_stale_days,
        catalog_url_count=catalog_url_count,
    )
