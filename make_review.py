#!/usr/bin/env python3
"""Build review.html: tick which bank bookings are business (= import into Invoice Ninja).

  ./make_review.py [CAMT_FILE_OR_DIR ...]     (default: all *camt folders/xml here)

Open review.html in a browser, decide, click "Save selection" -> ninja_selection.json
(saved to Downloads; move it next to this script). Then:
  ./camt2ninja.py <camt dir> --only ninja_selection.json            # dry run
  ./camt2ninja.py <camt dir> --only ninja_selection.json --commit   # import

If ninja_selection.json already exists here, earlier decisions are pre-filled
(per booking, and per counterparty for new bookings from known counterparties).
"""
import html
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("camt2ninja", HERE / "camt2ninja.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

INVOICE_RE = re.compile(r"\b(RN|Rechnung\w*|Re\.?)?\s*20\d\d[/-]\d{3,6}\b", re.I)


party_of = c.party_of  # shared with the importer


def main():
    paths = sys.argv[1:] or [str(p) for p in HERE.iterdir() if p.name.endswith("camt") or p.suffix == ".xml"]
    txs = c.collect(paths)
    prev = HERE / "ninja_selection.json"
    prev = json.loads(prev.read_text()) if prev.exists() else {}
    prev_refs = prev.get("decisions", {})
    prev_party = prev.get("parties", {})

    rows = []
    for t in txs:
        p = party_of(t)
        if t["ref"] in prev_refs:
            biz, why = prev_refs[t["ref"]], "saved"
        elif p in prev_party:
            biz, why = prev_party[p], "counterparty rule"
        elif t["base_type"] == "CREDIT" and INVOICE_RE.search(t["description"]):
            biz, why = True, "invoice no. in reference"
        else:
            biz, why = False, ""
        rows.append({**t, "party": p, "biz": biz, "why": why})

    period = f"{rows[0]['date']} – {rows[-1]['date']}" if rows else ""
    page = TEMPLATE.replace("__DATA__", json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")) \
                   .replace("__PERIOD__", html.escape(period))
    out = HERE / "review.html"
    out.write_text(page, encoding="utf-8")
    print(f"{len(rows)} bookings ({period}) -> {out}")
    print(f"pre-ticked as business: {sum(r['biz'] for r in rows)}")


TEMPLATE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bank Import Review</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a66;--line:#e3e1dc;--acc:#2f6b4f;--accbg:#e4f0e9;--neg:#9a3b2f;--pos:#2f6b4f;--hi:#fff7d6}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--card:#211f1e;--ink:#ecebe7;--mute:#9a9893;--line:#353330;--acc:#7cc3a0;--accbg:#22342b;--neg:#e08a7c;--pos:#7cc3a0;--hi:#3a3420}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
h1{font-size:17px;margin:0 0 2px}.sub{color:var(--mute);font-size:12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
.bar input[type=search]{flex:1;min-width:180px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:var(--card);color:var(--ink);padding:7px 10px;cursor:pointer;font:inherit}
.seg button.on{background:var(--acc);color:var(--card)}
.btn{border:1px solid var(--acc);background:var(--acc);color:var(--card);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit;font-weight:600}
.btn.ghost{background:transparent;color:var(--acc)}
.stats{display:flex;flex-wrap:wrap;gap:16px;margin-top:10px;font-size:13px}.stats b{font-variant-numeric:tabular-nums}
main{padding:12px 16px 60px;max-width:1100px;margin:0 auto}
.grp{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px;overflow:hidden}
.gh{display:flex;align-items:center;gap:10px;padding:9px 12px;cursor:pointer}
.gh .name{flex:1;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gh .meta{color:var(--mute);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
.grp.biz{border-color:var(--acc)}.grp.biz .gh{background:var(--accbg)}
.rows{display:none;border-top:1px solid var(--line)}.grp.open .rows{display:block}
.r{display:grid;grid-template-columns:22px 84px 92px 1fr;gap:10px;padding:6px 12px;border-bottom:1px solid var(--line);align-items:start;font-size:13px}
.r:last-child{border-bottom:0}.r.biz{background:var(--accbg)}
.amt{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}.amt.c{color:var(--pos)}.amt.d{color:var(--neg)}
.desc{color:var(--mute);word-break:break-word}.why{font-size:11px;color:var(--acc);margin-left:6px}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--acc);cursor:pointer}
.tw{font-size:12px;color:var(--mute)}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:8px;display:none}
@media(max-width:600px){.r{grid-template-columns:22px 1fr 80px}.r .desc{grid-column:2/4}}
</style></head><body>
<header>
  <h1>Bank import review — Erste Bank</h1>
  <div class="sub">__PERIOD__ · Tick = business, gets imported into Invoice Ninja. Unticked = private, skipped. Nothing is uploaded from this page.</div>
  <div class="bar">
    <input type="search" id="q" placeholder="Search counterparty or text…">
    <div class="seg" id="ty"><button data-v="all" class="on">All</button><button data-v="CREDIT">Incoming</button><button data-v="DEBIT">Outgoing</button></div>
    <div class="seg" id="st"><button data-v="all" class="on">Any</button><button data-v="biz">Business</button><button data-v="priv">Private</button></div>
    <label class="tw"><input type="checkbox" id="exp"> expand all</label>
    <button class="btn" id="save">Save selection</button>
  </div>
  <div class="stats" id="stats"></div>
</header>
<main id="list"></main>
<div class="toast" id="toast"></div>
<script>
const DATA=__DATA__;
const KEY='ninja-review:'+DATA.length+':'+(DATA[0]||{}).date;
try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(s)DATA.forEach(r=>{if(r.ref in s)r.biz=s[r.ref]})}catch(e){}
const persist=()=>{try{localStorage.setItem(KEY,JSON.stringify(Object.fromEntries(DATA.map(r=>[r.ref,r.biz]))))}catch(e){}};
const fmt=n=>n.toLocaleString('de-AT',{minimumFractionDigits:2,maximumFractionDigits:2});
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let f={q:'',ty:'all',st:'all'},open=new Set();
function visible(r){if(f.ty!=='all'&&r.base_type!==f.ty)return false;if(f.st==='biz'&&!r.biz)return false;if(f.st==='priv'&&r.biz)return false;
 if(f.q){const q=f.q.toLowerCase();return(r.party+' '+r.description).toLowerCase().includes(q)}return true}
function render(){
 const groups=new Map();DATA.forEach((r,i)=>{if(!visible(r))return;(groups.get(r.party)||groups.set(r.party,[]).get(r.party)).push(i)});
 const arr=[...groups.entries()].sort((a,b)=>b[1].length-a[1].length||a[0].localeCompare(b[0]));
 const all=document.getElementById('exp').checked;
 document.getElementById('list').innerHTML=arr.map(([p,ix])=>{
  const rs=ix.map(i=>DATA[i]),n=rs.filter(r=>r.biz).length,sum=rs.reduce((s,r)=>s+(r.base_type==='CREDIT'?1:-1)*+r.amount,0);
  const cls=n===rs.length?'biz':'';
  return `<div class="grp ${cls} ${all||open.has(p)?'open':''}" data-p="${esc(p)}">
   <div class="gh"><input type="checkbox" class="gc" ${n===rs.length?'checked':''} data-ind="${n>0&&n<rs.length?1:0}">
   <span class="name">${esc(p)}</span><span class="meta">${rs.length}× · ${fmt(sum)} € ${n&&n<rs.length?'· '+n+' business':''}</span></div>
   <div class="rows">${rs.map(r=>`<label class="r ${r.biz?'biz':''}"><input type="checkbox" class="rc" data-ref="${esc(r.ref)}" ${r.biz?'checked':''}>
    <span>${r.date}</span><span class="amt ${r.base_type==='CREDIT'?'c':'d'}">${r.base_type==='CREDIT'?'+':'−'}${fmt(+r.amount)}</span>
    <span class="desc">${esc(r.description)}${r.why?`<span class="why">${esc(r.why)}</span>`:''}</span></label>`).join('')}</div></div>`}).join('')||'<p class="tw">No bookings match.</p>';
 document.querySelectorAll('.gc').forEach(c=>{if(c.dataset.ind==='1')c.indeterminate=true});
 const b=DATA.filter(r=>r.biz),inc=b.filter(r=>r.base_type==='CREDIT').reduce((s,r)=>s+ +r.amount,0),out=b.filter(r=>r.base_type==='DEBIT').reduce((s,r)=>s+ +r.amount,0);
 document.getElementById('stats').innerHTML=`<span>Business: <b>${b.length}</b> of ${DATA.length}</span><span>Incoming <b>+${fmt(inc)} €</b></span><span>Outgoing <b>−${fmt(out)} €</b></span><span>Private/skipped: <b>${DATA.length-b.length}</b></span>`;
}
const byRef=Object.fromEntries(DATA.map(r=>[r.ref,r]));
document.getElementById('list').addEventListener('click',e=>{
 const g=e.target.closest('.grp');if(!g)return;const p=g.dataset.p;
 if(e.target.classList.contains('gc')){const v=e.target.checked;DATA.forEach(r=>{if(r.party===p&&visible(r)){r.biz=v;r.why=''}});persist();render();return}
 if(e.target.classList.contains('rc')){byRef[e.target.dataset.ref].biz=e.target.checked;byRef[e.target.dataset.ref].why='';persist();render();return}
 if(e.target.closest('.gh')){open.has(p)?open.delete(p):open.add(p);g.classList.toggle('open')}
});
document.getElementById('q').addEventListener('input',e=>{f.q=e.target.value;render()});
document.getElementById('exp').addEventListener('change',render);
for(const id of['ty','st'])document.getElementById(id).addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
 f[id]=b.dataset.v;[...b.parentNode.children].forEach(x=>x.classList.toggle('on',x===b));render()});
document.getElementById('save').addEventListener('click',()=>{
 const parties={};const pg={};DATA.forEach(r=>{(pg[r.party]=pg[r.party]||[]).push(r.biz)});
 for(const[p,v]of Object.entries(pg)){if(v.every(x=>x))parties[p]=true;else if(v.every(x=>!x))parties[p]=false}
 const out={created:new Date().toISOString(),decisions:Object.fromEntries(DATA.map(r=>[r.ref,r.biz])),parties};
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:'application/json'}));
 a.download='ninja_selection.json';a.click();
 const t=document.getElementById('toast');t.textContent='Saved ninja_selection.json to Downloads — move it into the Ninja folder';t.style.display='block';setTimeout(()=>t.style.display='none',4000);
});
render();
</script></body></html>"""

if __name__ == "__main__":
    main()
