#!/usr/bin/env python3
"""Build expenses.html: per counterparty of outgoing business bookings choose
vendor + expense category (or "no expense"). Save -> expense_map.json, then:

  ./camt2ninja.py <camt dir> --only ninja_selection.json --match --expenses expense_map.json          # preview
  ./camt2ninja.py <camt dir> --only ninja_selection.json --match --expenses expense_map.json --commit

Existing expense_map.json is pre-filled, so next month only new counterparties need a decision.
"""
import collections
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

# suggestion table: substring of counterparty (upper case) -> (vendor, category, action)
SUGGEST = [
    ("WP-KAUF", "Erste Bank", "Wertpapiere", "skip"),
    ("WERTPAPIER-SPARPLAN", "Erste Bank", "Wertpapiere", "skip"),
    ("DEPOTENTG", "Erste Bank", "Wertpapiere", "skip"),
    ("SOLLZINSEN", "Erste Bank", "Bankspesen", "expense"),
    ("KONTOFÜHRUNG", "Erste Bank", "Bankspesen", "expense"),
    ("SOZIALVERSICHERUNG", "SVS", "SVA", "expense"),
]


def suggest(party):
    party = re.sub(r"\s*\(PayPal\)$", "", party)  # PayPal merchant: match the merchant, not "PayPal"
    up = party.upper()
    for key, vendor, cat, action in SUGGEST:
        # match at word start ("ENI" must not hit "XENIA", but "ACME" must hit "ACMELEASING")
        if re.search(r"(?<![A-ZÄÖÜ0-9])" + re.escape(key), up):
            return {"vendor": vendor, "category": cat, "action": action, "why": "suggestion"}
    return {"vendor": party.title() if party.isupper() else party, "category": "Sonstiges", "action": "expense", "why": "default"}


CAT_VAT = {"Versicherungen": "0", "Bankspesen": "0", "SVA": "0", "Wertpapiere": "0", "Gebühren": "0",
           "KFZ": "nodeduct", "Tanken": "nodeduct"}
VENDOR_VAT = {}  # known exceptions to the country rule


def vat_default(m, country, pos=False):
    if m.get("vendor") in VENDOR_VAT:
        return VENDOR_VAT[m["vendor"]], "known vendor"
    cat = m.get("category", "")
    for k, v in CAT_VAT.items():
        if cat == k or cat.startswith(k + " "):
            return v, f"category {cat}"
    if country == "AT":
        return "20", "Austria"
    if country in c.EU:
        if pos:
            return "foreign", f"paid on site in {country}"
        return "rc", f"EU service from {country} (with your UID)"
    return "rc", f"service from {country} (non-EU)"


def main():
    paths = sys.argv[1:] or [str(p) for p in HERE.iterdir() if p.name.endswith("camt") or p.suffix == ".xml"]
    sel = json.loads((HERE / "ninja_selection.json").read_text())["decisions"]
    debits = [t for t in c.collect(paths) if sel.get(t["ref"]) and t["base_type"] == "DEBIT"]
    prev_f = HERE / "expense_map.json"
    prev = json.loads(prev_f.read_text()) if prev_f.exists() else {}

    groups = collections.defaultdict(list)
    for t in debits:
        groups[c.party_of(t)].append(t)
    rows = []
    for p, ts in sorted(groups.items(), key=lambda x: -sum(float(t["amount"]) for t in x[1])):
        m = {**prev[p], "why": "saved"} if p in prev else suggest(p)
        country = collections.Counter(c.booking_country(t) for t in ts).most_common(1)[0][0]
        if not m.get("vat"):
            pos = any(t["description"].startswith(("POS", "ATM")) for t in ts)
            m["vat"], m["vat_why"] = vat_default(m, country, pos)
        rows.append({"party": p, "n": len(ts), "sum": round(sum(float(t["amount"]) for t in ts), 2),
                     "sample": ts[0]["description"][:140], "country": country, **m})

    # existing Invoice Ninja vendors/categories for the dropdowns (read-only)
    vendors, cats = [], []
    try:
        c.load_env()
        api = c.api_from_env()
        vendors = sorted(v["name"] for v in api.all("vendors") if not v.get("is_deleted"))
        cats = sorted(x["name"] for x in api.all("expense_categories") if not x.get("is_deleted"))
    except SystemExit:
        pass
    ninja_v, ninja_c = list(vendors), list(cats)
    cats = sorted(set(cats) | {r["category"] for r in rows})
    vendors = sorted(set(vendors) | {r["vendor"] for r in rows})
    js = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")

    page = (TEMPLATE.replace("__PREV__", json.dumps(prev, ensure_ascii=False).replace("</", "<\\/"))
                    .replace("__DATA__", json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__VENDORS__", json.dumps(vendors, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__CATS__", json.dumps(cats, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__NINJA_V__", js(ninja_v)).replace("__NINJA_C__", js(ninja_c))
            .replace("__VAT__", js({k: v[1] for k, v in c.VAT.items()}))
            .replace("__N__", str(len(debits))))
    out = HERE / "expenses.html"
    out.write_text(page, encoding="utf-8")
    print(f"{len(debits)} outgoing business bookings, {len(rows)} counterparties -> {out}")


TEMPLATE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Expense Mapping</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a66;--line:#e3e1dc;--acc:#2f6b4f;--accbg:#e4f0e9;--warn:#8a5a00;--warnbg:#fbf1dc}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--card:#211f1e;--ink:#ecebe7;--mute:#9a9893;--line:#353330;--acc:#7cc3a0;--accbg:#22342b;--warn:#e7b65c;--warnbg:#3a3020}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
h1{font-size:17px;margin:0 0 2px}.sub{color:var(--mute);font-size:12px}
.bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-top:10px;font-size:13px}.bar b{font-variant-numeric:tabular-nums}
.btn{border:1px solid var(--acc);background:var(--acc);color:var(--card);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit;font-weight:600;margin-left:auto}
main{padding:12px 16px 60px;max-width:1150px;margin:0 auto}
.row{display:grid;grid-template-columns:1.25fr 1fr 1fr 1.1fr 140px;gap:10px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px}
.row.skip{opacity:.55}.row.new{border-color:var(--warn)}
.party{font-weight:600;word-break:break-word}.meta{color:var(--mute);font-size:12px;font-variant-numeric:tabular-nums}
.sample{color:var(--mute);font-size:11px;margin-top:3px;word-break:break-word}
input,select{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink);font:inherit}
label.l{display:block;font-size:11px;color:var(--mute);margin-bottom:2px}
.why2{color:var(--mute);font-size:10px}.tag{font-size:10px;padding:1px 6px;border-radius:6px;background:var(--warnbg);color:var(--warn);margin-left:6px;font-weight:500}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:8px;display:none}
@media(max-width:900px){.row{grid-template-columns:1fr 1fr}.row>div:first-child{grid-column:1/3}}
</style></head><body>
<header>
  <h1>Expenses — vendor & category per counterparty</h1>
  <div class="sub">__N__ outgoing business bookings. Each booking becomes one expense in Invoice Ninja with this vendor and category. Vendors/categories that don't exist yet are created on import (marked <span class="tag">new</span>). Nothing is uploaded from this page.</div>
  <div class="bar" id="stats"></div>
</header>
<main id="list"></main>
<datalist id="vendors"></datalist><datalist id="cats"></datalist>
<div class="toast" id="toast"></div>
<script>
const DATA=__DATA__, VEND=__VENDORS__, CATS=__CATS__, VAT=__VAT__;
const PREV=__PREV__;  // earlier mapping entries (other periods) are kept when saving
const KEY='ninja-expenses-v2:'+DATA.length;
try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(s)DATA.forEach(r=>{if(s[r.party])Object.assign(r,s[r.party])})}catch(e){}
const persist=()=>{try{localStorage.setItem(KEY,JSON.stringify(Object.fromEntries(DATA.map(r=>[r.party,{vendor:r.vendor,category:r.category,vat:r.vat,action:r.action}]))))}catch(e){}};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=n=>n.toLocaleString('de-AT',{minimumFractionDigits:2,maximumFractionDigits:2});
function fill(){
 const vs=[...new Set([...VEND,...DATA.map(r=>r.vendor)])].sort(),cs=[...new Set([...CATS,...DATA.map(r=>r.category)])].sort();
 document.getElementById('vendors').innerHTML=vs.map(v=>`<option value="${esc(v)}">`).join('');
 document.getElementById('cats').innerHTML=cs.map(v=>`<option value="${esc(v)}">`).join('');
}
function stats(){
 const ex=DATA.filter(r=>r.action==='expense');
 const nv=new Set(ex.map(r=>r.vendor)),nc=new Set(ex.map(r=>r.category));
 document.getElementById('stats').innerHTML=`<span>Expenses: <b>${ex.reduce((s,r)=>s+r.n,0)}</b> bookings · <b>${fmt(ex.reduce((s,r)=>s+r.sum,0))} €</b></span>
  <span>No expense: <b>${DATA.filter(r=>r.action!=='expense').reduce((s,r)=>s+r.n,0)}</b></span>
  <span>Vendors: <b>${nv.size}</b></span><span>Categories: <b>${[...nc].join(', ')}</b></span>
  <button class="btn" id="save">Save mapping</button>`;
 document.getElementById('save').onclick=save;
}
function render(){
 fill();
 document.getElementById('list').innerHTML=DATA.map((r,i)=>`<div class="row ${r.action!=='expense'?'skip':''}" data-i="${i}">
  <div><div class="party">${esc(r.party)}</div><div class="meta">${r.n}× · ${fmt(r.sum)} € · Land <b>${esc(r.country)}</b></div><div class="sample">${esc(r.sample)}</div></div>
  <div><label class="l">Vendor <span class="tag vt" ${isNewV(r.vendor)?'':'hidden'}>new</span></label>
   <input list="vendors" data-k="vendor" value="${esc(r.vendor)}"></div>
  <div><label class="l">Category <span class="tag ct" ${isNewC(r.category)?'':'hidden'}>new</span></label>
   <input list="cats" data-k="category" value="${esc(r.category)}"></div>
  <div><label class="l">VAT${r.vat_why?` <span class="why2">(${esc(r.vat_why)})</span>`:''}</label><select data-k="vat">
   ${Object.entries(VAT).map(([k,l])=>`<option value="${k}" ${r.vat===k?'selected':''}>${esc(l)}</option>`).join('')}</select></div>
  <div><label class="l">Action</label><select data-k="action">
   <option value="expense" ${r.action==='expense'?'selected':''}>Create expense</option>
   <option value="skip" ${r.action!=='expense'?'selected':''}>No expense (leave unmatched)</option></select></div>
 </div>`).join('');
 stats();
}
const NINJA_V=new Set(__NINJA_V__.map(v=>v.toLowerCase())),NINJA_C=new Set(__NINJA_C__.map(v=>v.toLowerCase()));
const isNewV=v=>!NINJA_V.has(v.trim().toLowerCase()), isNewC=c=>!NINJA_C.has(c.trim().toLowerCase());
document.getElementById('list').addEventListener('input',e=>{
 const row=e.target.closest('.row');if(!row)return;const r=DATA[+row.dataset.i];r[e.target.dataset.k]=e.target.value;r.why='edited';
 row.querySelector('.vt').hidden=!isNewV(r.vendor);row.querySelector('.ct').hidden=!isNewC(r.category);
 row.classList.toggle('skip',r.action!=='expense');persist();stats();
});
document.getElementById('list').addEventListener('change',()=>fill());
function save(){
 const out={...PREV,...Object.fromEntries(DATA.map(r=>[r.party,{vendor:r.vendor.trim(),category:r.category.trim(),vat:r.vat,action:r.action}]))};
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:'application/json'}));
 a.download='expense_map.json';a.click();
 const t=document.getElementById('toast');t.textContent='Saved expense_map.json to Downloads — move it into the Ninja folder';t.style.display='block';setTimeout(()=>t.style.display='none',4000);
}
render();
</script></body></html>"""

if __name__ == "__main__":
    main()
