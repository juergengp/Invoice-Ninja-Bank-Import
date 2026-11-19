#!/usr/bin/env python3
"""Import outgoing invoices from ebInterface XML (e.g. ProSaldo.net export) into Invoice Ninja.

  ./ebinterface2ninja.py DIR_OR_FILES [--prefix PS-]            # dry run: what would be created
  ./ebinterface2ninja.py DIR_OR_FILES --commit                  # create clients + invoices (marked sent, no email)
  ./ebinterface2ninja.py --rollback last --commit               # delete what a run created

- invoice number in Invoice Ninja: <prefix><year>-<number, 3 digits>, e.g. PS-2024-019
  (cannot collide with Invoice Ninja's own 2024/000019 numbering)
- clients are matched by VAT number (UID), then by name; missing ones are created
- line items with quantity, unit price, discount %, VAT (S = standard rate, AE = reverse charge, other = exempt)
- a credit memo that fully cancels an earlier invoice of the same client (same amount) cancels that invoice
  in Invoice Ninja; other credit memos are reported and skipped
- the PDF next to the XML (same name) is attached to the invoice
Config: same .env as camt2ninja.py (IN_URL, IN_TOKEN, IN_CA_FILE).
"""
import argparse
import datetime
import json
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import camt2ninja as c

NS = {"e": "http://www.ebinterface.at/schema/6p0/"}
COUNTRY_IDS = {"AT": "40", "DE": "276", "IT": "380", "CH": "756", "NL": "528", "LU": "442", "FR": "250", "CZ": "203"}
RUNS = c.HERE / "runs"


def tx(el, path):
    f = el.find(path, NS) if el is not None else None
    return (f.text or "").strip() if f is not None and f.text else ""


def parse(path):
    r = ET.parse(path).getroot()
    rec = r.find("e:InvoiceRecipient", NS)
    lines = []
    for li in r.iterfind(".//e:ListLineItem", NS):
        descs = [d.text.strip() for d in li.findall("e:Description", NS) if d.text and d.text.strip()]
        tp = li.find("e:TaxItem/e:TaxPercent", NS)
        lines.append({
            "product_key": tx(li, "e:ArticleNumber"),
            "title": descs[0] if descs else "",
            "notes": "\n".join(dict.fromkeys(descs)),
            "quantity": float(tx(li, "e:Quantity") or 1),
            "cost": float(tx(li, "e:UnitPrice") or 0),
            "discount": float(tx(li, ".//e:ReductionListLineItem/e:Percentage") or 0),
            "tax_cat": tp.get("TaxCategoryCode") if tp is not None else "S",
            "tax_rate": float(tp.text) if tp is not None and tp.text else 0.0,
            "amount": float(tx(li, "e:LineItemAmount") or 0),
        })
    addr = rec.find("e:Address", NS)
    country = addr.find("e:Country", NS)
    return {
        "file": str(path), "pdf": str(Path(path).with_suffix(".pdf")),
        "type": r.get("DocumentType"), "number": int(tx(r, "e:InvoiceNumber")), "date": tx(r, "e:InvoiceDate"),
        "due": tx(r, "e:PaymentConditions/e:DueDate") or tx(r, "e:InvoiceDate"),
        "title": tx(r, "e:Details/e:HeaderDescription"), "footer": tx(r, "e:Details/e:FooterDescription"),
        "po": tx(r, "e:Biller/e:OrderReference/e:OrderID"),
        "client": {"name": tx(addr, "e:Name"), "vat": tx(rec, "e:VATIdentificationNumber").replace(" ", "").upper(),
                   "street": tx(addr, "e:Street"), "town": tx(addr, "e:Town"), "zip": tx(addr, "e:ZIP"),
                   "country": country.get("CountryCode") if country is not None else "AT"},
        "gross": float(tx(r, "e:TotalGrossAmount") or 0), "lines": lines,
    }


def collect(paths):
    files = []
    for p in map(Path, paths):
        files += sorted(p.rglob("*.xml")) if p.is_dir() else [p]
    return sorted((parse(f) for f in files), key=lambda d: (d["date"], d["number"]))


def pair_credits(docs):
    """credit memo -> the invoice it fully cancels (same client UID/name, same gross, issued on or before)"""
    used, pairs = set(), {}
    invs = [d for d in docs if d["type"] == "Invoice"]
    for cm in (d for d in docs if d["type"] == "CreditMemo"):
        key = cm["client"]["vat"] or cm["client"]["name"].lower()
        cands = [i for i in invs if (i["client"]["vat"] or i["client"]["name"].lower()) == key
                 and abs(i["gross"] - cm["gross"]) < 0.005 and i["date"] <= cm["date"] and id(i) not in used]
        if cands:
            # cancel + re-issue: prefer an invoice of the same day, otherwise the oldest one
            best = min(cands, key=lambda i: (i["date"] != cm["date"], i["date"], i["number"]))
            used.add(id(best))
            pairs[id(cm)] = best
    return pairs


def number_of(prefix, d):
    return f"{prefix}{d['date'][:4]}-{d['number']:03d}"


def upload_pdf(api, inv_id, pdf):
    data = Path(pdf).read_bytes()
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"_method\"\r\n\r\nPUT\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"documents[]\"; filename=\"{Path(pdf).name}\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"{api.url}/api/v1/invoices/{inv_id}/upload", data=body, method="POST", headers={
        "X-API-TOKEN": api.token, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60, context=api.ctx) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--prefix", default="PS-")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--rollback", metavar="RUN")
    a = ap.parse_args()
    c.load_env()
    api = c.api_from_env()
    if a.rollback:
        return rollback(api, a.rollback, a.commit)
    if not a.paths:
        ap.error("give XML file(s) or folder")

    docs = collect(a.paths)
    pairs = pair_credits(docs)
    cancelled = {id(i): cm for cm_id, i in pairs.items() for cm in docs if id(cm) == cm_id}
    clients = [x for x in api.all("clients") if not x.get("is_deleted")]
    by_vat = {(x.get("vat_number") or "").replace(" ", "").upper(): x for x in clients if x.get("vat_number")}
    by_name = {x["name"].lower().strip(" ."): x for x in clients}
    existing = {i["number"]: i for i in api.all("invoices") if not i.get("is_deleted")}
    rates = [r for r in api.all("tax_rates") if not r.get("is_deleted")]
    rate_name = {float(r["rate"]): r["name"] for r in rates if float(r["rate"]) > 0}
    named = {r["name"].lower(): r["name"] for r in rates}

    def tax_of(li):
        if li["tax_cat"] == "S" and li["tax_rate"] > 0:
            return rate_name.get(li["tax_rate"], f"USt {li['tax_rate']:g}%"), li["tax_rate"]
        if li["tax_cat"] == "AE":
            return named.get("reverse charge", "Reverse Charge"), 0
        return named.get("steuerbefreit", "Steuerbefreit"), 0

    plan, new_clients = [], {}
    for d in docs:
        num = number_of(a.prefix, d)
        if d["type"] == "CreditMemo":
            inv = pairs.get(id(d))
            plan.append(("credit", d, num, f"cancels {number_of(a.prefix, inv)}" if inv else "NO MATCHING INVOICE - skipped"))
            continue
        if num in existing:
            plan.append(("exists", d, num, "already in Invoice Ninja"))
            continue
        cl = d["client"]
        hit = by_vat.get(cl["vat"]) if cl["vat"] and set(cl["vat"]) != {"0"} else None
        hit = hit or by_name.get(cl["name"].lower().strip(" ."))
        if not hit:
            new_clients.setdefault(cl["vat"] if set(cl["vat"] or "0") != {"0"} else cl["name"].lower(), cl)
        net = sum(li["cost"] * li["quantity"] * (1 - li["discount"] / 100) for li in d["lines"])
        tax = sum(li["cost"] * li["quantity"] * (1 - li["discount"] / 100) * tax_of(li)[1] / 100 for li in d["lines"])
        note = "cancel (credit memo)" if id(d) in cancelled else ""
        plan.append(("create", d, num, f"{(hit or {}).get('name') or cl['name'] + ' (NEW)'}" + (f" - {note}" if note else "")
                     + ("" if abs(net + tax - d["gross"]) < 0.02 else f" !! computed {net + tax:.2f} != {d['gross']:.2f}")))

    for kind, d, num, info in plan:
        print(f"  {kind:7} {num:13} {d['date']} {d['gross']:10.2f}  {info}")
    cnt = {k: sum(1 for p in plan if p[0] == k) for k in ("create", "exists", "credit")}
    print(f"\n{len(docs)} documents: create {cnt['create']} invoices, {cnt['exists']} already there, "
          f"{cnt['credit']} credit memos; new clients ({len(new_clients)}): {', '.join(x['name'] for x in new_clients.values()) or '-'}")
    if not a.commit:
        print("dry run - add --commit")
        return

    RUNS.mkdir(exist_ok=True)
    run_id = "inv-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = {"run": run_id, "kind": "ebinterface", "invoices": [], "clients": []}
    run_file = RUNS / f"{run_id}.json"
    save = lambda: run_file.write_text(json.dumps(log, indent=1))
    client_ids = {}
    for key, cl in new_clients.items():
        r = api.req("POST", "clients", {
            "name": cl["name"], "vat_number": cl["vat"] if set(cl["vat"] or "0") != {"0"} else "",
            "address1": cl["street"], "city": cl["town"], "postal_code": cl["zip"],
            "country_id": COUNTRY_IDS.get(cl["country"], "40"), "contacts": [{"first_name": "", "last_name": ""}]})["data"]
        client_ids[key] = r["id"]
        log["clients"].append(r["id"])
        save()
        print(f"  created client {cl['name']}")
    for kind, d, num, _ in plan:
        if kind != "create":
            continue
        cl = d["client"]
        hit = by_vat.get(cl["vat"]) if cl["vat"] and set(cl["vat"]) != {"0"} else None
        hit = hit or by_name.get(cl["name"].lower().strip(" ."))
        cid = hit["id"] if hit else client_ids[cl["vat"] if set(cl["vat"] or "0") != {"0"} else cl["name"].lower()]
        items = []
        for li in d["lines"]:
            tn, tr = tax_of(li)
            items.append({"product_key": li["product_key"] or li["title"][:40], "notes": li["notes"], "cost": li["cost"],
                          "quantity": li["quantity"], "discount": li["discount"], "tax_name1": tn, "tax_rate1": tr,
                          "type_id": "1"})
        cm = cancelled.get(id(d))
        body = {"client_id": cid, "number": num, "date": d["date"], "due_date": d["due"], "po_number": d["po"],
                "public_notes": d["title"], "footer": d["footer"], "line_items": items,
                "uses_inclusive_taxes": False, "is_amount_discount": False,
                "private_notes": f"Import aus ProSaldo.net: Rechnung Nr. {d['number']} vom {d['date']}"
                                 + (f" · storniert durch Gutschrift Nr. {cm['number']} vom {cm['date']}" if cm else "")}
        inv = api.req("POST", "invoices", body)["data"]
        log["invoices"].append(inv["id"])
        save()
        api.req("POST", "invoices/bulk", {"action": "mark_sent", "ids": [inv["id"]]})
        if cm:
            api.req("POST", "invoices/bulk", {"action": "cancel", "ids": [inv["id"]]})
        if not a.no_pdf and Path(d["pdf"]).exists():
            upload_pdf(api, inv["id"], d["pdf"])
        chk = api.req("GET", f"invoices/{inv['id']}")["data"]
        flag = "" if abs(float(chk["amount"]) - d["gross"]) < 0.02 else f"  !! amount {chk['amount']} != {d['gross']}"
        print(f"  created {num} {chk['amount']:>10} status {chk['status_id']}{flag}")
    print(f"run id {run_id} - undo: ./ebinterface2ninja.py --rollback {run_id} --commit")


def rollback(api, run, commit):
    runs = sorted(RUNS.glob("inv-*.json"))
    f = runs[-1] if run == "last" and runs else RUNS / f"{run}.json"
    d = json.loads(f.read_text())
    print(f"run {d['run']}: delete {len(d['invoices'])} invoices, {len(d['clients'])} clients")
    if not commit:
        print("dry run - add --commit")
        return
    for k in range(0, len(d["invoices"]), 100):
        api.req("POST", "invoices/bulk", {"action": "delete", "ids": d["invoices"][k:k + 100]})
    if d["clients"]:
        api.req("POST", "clients/bulk", {"action": "delete", "ids": d["clients"]})
    d["rolled_back"] = True
    f.write_text(json.dumps(d, indent=1))
    print("done")


if __name__ == "__main__":
    sys.exit(main())
