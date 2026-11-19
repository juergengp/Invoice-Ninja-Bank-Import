#!/usr/bin/env python3
"""Import Erste/George Business bank statements (CAMT.053 XML or George JSON export) into Invoice Ninja.

Usage:
  ./camt2ninja.py FILE_OR_DIR [...]              # dry run: parse + show what would be imported
  ./camt2ninja.py FILE_OR_DIR [...] --commit     # actually import
  ./camt2ninja.py ... --limit 5 --commit        # small test import
  ./camt2ninja.py ... --match                    # also link incoming payments to open invoices
  ./camt2ninja.py ... --expenses expense_map.json  # turn outgoing bookings into expenses (vendor/category)
  ./camt2ninja.py --runs                         # list import runs
  ./camt2ninja.py --rollback last [--commit]     # undo a run (dry run without --commit)
Options:
  --credits-only    import only incoming payments (skip card payments / debits)
  --since YYYY-MM-DD  ignore bookings before this date

Config (.env next to this script or environment):
  IN_URL=https://invoicing.example.com
  IN_TOKEN=<Invoice Ninja API token>
  IN_BANK_INTEGRATION_ID=<optional; auto-created/looked up by IBAN if missing>
  IN_CA_FILE=<optional; extra CA/intermediate PEM if your server sends an incomplete chain>

Duplicates are skipped via the bank's AcctSvcrRef, tracked in imported_refs.json
and cross-checked against transactions already in Invoice Ninja.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "imported_refs.json"
CURRENCY_IDS = {"EUR": "3", "USD": "1", "GBP": "2", "CHF": "17"}


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


# ---------------------------------------------------------------- CAMT parsing
def strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def txt(el, path):
    if el is None:
        return ""
    f = el.find(path)
    return (f.text or "").strip() if f is not None and f.text else ""


def parse_camt(path):
    root = strip_ns(ET.parse(path).getroot())
    for stmt in root.iter("Stmt"):
        iban = txt(stmt, "Acct/Id/IBAN")
        ccy = txt(stmt, "Acct/Ccy") or "EUR"
        for ntry in stmt.findall("Ntry"):
            if txt(ntry, "Sts") not in ("BOOK", ""):
                continue  # skip pending
            ind = txt(ntry, "CdtDbtInd")
            date = txt(ntry, "BookgDt/Dt") or txt(ntry, "BookgDt/DtTm")[:10]
            entry_ref = txt(ntry, "AcctSvcrRef")
            details = ntry.findall("NtryDtls/TxDtls") or [None]
            for i, tx in enumerate(details):
                if tx is not None and len(details) > 1:
                    amt = txt(tx, "AmtDtls/TxAmt/Amt") or txt(tx, "Amt")
                else:
                    amt = txt(ntry, "Amt")
                # counterparty: who paid us (credit) or whom we paid (debit)
                side = "Dbtr" if ind == "CRDT" else "Cdtr"
                name = txt(tx, f"RltdPties/{side}/Nm") or txt(tx, f"RltdPties/{side}/Pty/Nm")
                cp_iban = txt(tx, f"RltdPties/{side}Acct/Id/IBAN")
                ustrd = " ".join(
                    (u.text or "").strip() for u in (tx.findall("RmtInf/Ustrd") if tx is not None else [])
                ).strip()
                sref = txt(tx, "RmtInf/Strd/CdtrRefInf/Ref")
                e2e = txt(tx, "Refs/EndToEndId")
                if e2e == "NOTPROVIDED":
                    e2e = ""
                ref = txt(tx, "Refs/AcctSvcrRef") or entry_ref
                if ref and len(details) > 1:
                    ref = f"{ref}#{i}"
                if not ref:
                    ref = "h:" + hashlib.sha1(f"{iban}|{date}|{ind}|{amt}|{ustrd}|{i}".encode()).hexdigest()[:20]
                # key_desc: legacy format, only used for the booking key -> keeps saved
                # selections and imported_refs.json valid. Do not change.
                key_desc = " | ".join(p for p in [name, sref, ustrd, e2e and f"E2E {e2e}"] if p)
                # what goes to Invoice Ninja: only the payer's text (reference + purpose);
                # name is in participant_name, IBAN in participant
                desc = " ".join(p for p in [sref, ustrd] if p) or name
                # Erste reuses one AcctSvcrRef for all period-end charges -> make key unique
                ref = f"{ref}|{ind}|{float(amt):.2f}|" + hashlib.sha1(key_desc.encode()).hexdigest()[:8]
                yield {
                    "ref": ref,
                    "iban": iban,
                    "currency": ccy,
                    "date": date,
                    "amount": f"{float(amt):.2f}",
                    "base_type": "CREDIT" if ind == "CRDT" else "DEBIT",
                    "participant": cp_iban,
                    "participant_name": name,
                    "description": desc[:500] or "(no text)",
                }


def party_of(t):
    if t["participant_name"]:
        return t["participant_name"].strip()
    d = t["description"]
    m = re.search(r"\d\d\.\d\d\. \d\d:\d\d(.+)", d)  # card payment: merchant after timestamp
    if m:
        s = re.split(r"SPESEN:|\s{2,}", m.group(1))[0]
        s = re.sub(r"\s\d{6,}\b", "", s)  # ticket/receipt numbers (e.g. WIENER L 2026090400612)
        s = re.sub(r"(\s+\d+)+\s*$", "", s).strip()
        kind = d.split()[0] if d.split() else ""
        return f"{s}" + (f" ({kind})" if kind in ("ATM",) else "")
    return re.split(r"\s{2,}|\d", d)[0].strip()[:40] or d[:40]


CARD_RE = re.compile(r"\d\d\.\d\d\. \d\d:\d\d")


def ninja_participant(t):
    """Counterparty name for Invoice Ninja: real name, card merchant, or the bank itself
    (interest, fees, securities bookings carry no counterparty)."""
    if t["participant_name"]:
        return t["participant_name"]
    if CARD_RE.search(t["description"]):
        return party_of(t)
    return "Erste Bank"


def parse_george_json(path):
    """George (Erste Bank) JSON export: list of bookings. Same output as parse_camt."""
    m = re.match(r"([A-Z]{2}\d{2}[A-Z0-9]{8,30})_", Path(path).name)
    own = m.group(1) if m else ""
    for r in json.loads(Path(path).read_text(encoding="utf-8")):
        a = r.get("amount") or {}
        if r.get("booking") is None or not a.get("value"):
            continue
        val = a["value"] / 10 ** a.get("precision", 2)
        ind = "CRDT" if val > 0 else "DBIT"
        amt = abs(val)
        text = " ".join(" ".join(p.split()) for p in [r.get("receiverReference"), r.get("reference")] if p)
        name = (r.get("partnerName") or "").strip()
        if CARD_RE.search(text):
            name = ""  # card payment: merchant comes from the text, same as in CAMT
        cp_iban = ((r.get("partnerAccount") or {}).get("iban") or "").replace(" ", "")
        e2e = r.get("e2eReference") or ""
        key_desc = " | ".join(p for p in [name, text, e2e and f"E2E {e2e}"] if p)
        bank_ref = r.get("referenceNumber") or "h:" + hashlib.sha1(
            f"{own}|{r['booking']}|{val}|{text}".encode()).hexdigest()[:20]
        yield {
            "ref": f"{bank_ref}|{ind}|{amt:.2f}|j" + hashlib.sha1(key_desc.encode()).hexdigest()[:8],
            "iban": own or (r.get("ownerAccountNumber") or ""),
            "currency": a.get("currency") or "EUR",
            "date": r["booking"][:10],
            "amount": f"{amt:.2f}",
            "base_type": "CREDIT" if ind == "CRDT" else "DEBIT",
            "participant": cp_iban,
            "participant_name": name,
            "description": text[:500] or name or "(no text)",
        }


def load_paypal(folder=None):
    """PayPal activity CSV exports (folder PAYPAL/ or IN_PAYPAL_DIR): bank reference -> (merchant, item).
    The bank only shows "<13 digit ref>/PAYPAL"; PayPal's 'Bankgutschrift auf PayPal-Konto' row carries the
    same number as 'Bankreferenz' and points to the real payment via 'Zugehöriger Transaktionscode'."""
    import csv
    folder = Path(folder or os.environ.get("IN_PAYPAL_DIR") or HERE / "PAYPAL")
    if not folder.is_dir():
        return {}
    rows = []
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() == ".csv":
            with open(f, encoding="utf-8-sig", newline="") as fh:
                rows += [r for r in csv.DictReader(fh) if "Bankreferenz" in r]
    by_tx = {r["Transaktionscode"]: r for r in rows}
    out = {}
    for r in rows:
        if r.get("Bankreferenz"):
            rel = by_tx.get(r.get("Zugehöriger Transaktionscode") or "")
            name = (rel or {}).get("Name", "").strip() or "PayPal Ausgleich"
            item = (rel or {}).get("Artikelbezeichnung", "").strip()
            out.setdefault(r["Bankreferenz"], (name, item))
    return out


PAYPAL_RE = re.compile(r"^(\d{10,16})/PAYPAL\b")


def resolve_paypal(txs):
    """replace the PayPal counterparty by the real merchant from the PayPal export (key/ref unchanged)"""
    pp = load_paypal()
    if not pp:
        return 0
    n = 0
    for t in txs:
        m = PAYPAL_RE.match(t["description"])
        if m and m.group(1) in pp:
            name, item = pp[m.group(1)]
            t["participant_name"] = name if name == "PayPal Ausgleich" else f"{name} (PayPal)"
            t["description"] = " · ".join(p for p in [t["description"], name, item] if p)[:500]
            n += 1
    return n


def base_ref(ref):
    """bank reference + direction + amount: identical for the same booking in CAMT and JSON"""
    return "|".join(ref.split("|")[:3])


def collect(paths, state=None):
    files = []
    for p in map(Path, paths):
        files += sorted([*p.rglob("*.xml"), *p.rglob("*.json")]) if p.is_dir() else [p]
    seen, out = set(), []
    for f in files:
        parse = parse_george_json if f.suffix.lower() == ".json" else parse_camt
        for t in parse(f):
            if t["ref"] not in seen:  # overlapping statements
                seen.add(t["ref"])
                out.append(t)
    # the same booking from another format (CAMT vs JSON) has another text hash:
    # drop it when that booking is already known under a different ref (in this run or imported)
    if state is None:  # imported bookings + bookings already decided in the review (business or private)
        state = set(json.loads(STATE.read_text())) if STATE.exists() else set()
        sel = HERE / "ninja_selection.json"
        if sel.exists():
            state |= set(json.loads(sel.read_text()).get("decisions", {}))
    known = collections.defaultdict(set)
    for ref in [*state, *(t["ref"] for t in out if "|j" not in t["ref"])]:
        known[base_ref(ref)].add(ref)
    slots = {b: len(refs) for b, refs in known.items()}
    for t in out:  # rows that are themselves known use up their own slot
        if t["ref"] in known.get(base_ref(t["ref"]), ()):
            slots[base_ref(t["ref"])] -= 1
    keep = []
    for t in out:
        b = base_ref(t["ref"])
        if "|j" in t["ref"] and t["ref"] not in known.get(b, ()) and slots.get(b, 0) > 0:
            slots[b] -= 1  # duplicate of a CAMT / already imported booking
            continue
        keep.append(t)
    resolve_paypal(keep)
    return sorted(keep, key=lambda t: (t["date"], t["ref"]))


# ---------------------------------------------------------------- Invoice Ninja API
def ssl_context():
    # Some servers send only the leaf certificate. IN_CA_FILE adds the missing intermediate.
    # Proper fix: configure the full chain on the web server.
    import ssl
    ctx = ssl.create_default_context()
    extra = os.environ.get("IN_CA_FILE")
    if extra:
        extra = Path(extra) if Path(extra).is_absolute() else HERE / extra
        ctx.load_verify_locations(cafile=str(extra))
    return ctx


class Ninja:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        self.ctx = ssl_context()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            f"{self.url}/api/v1/{path}", data=data, method=method,
            headers={"X-API-TOKEN": self.token, "X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30, context=self.ctx) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"API {method} {path} -> HTTP {e.code}: {e.read()[:800].decode(errors='replace')}")

    def all(self, path):
        page, out = 1, []
        while True:
            sep = "&" if "?" in path else "?"
            r = self.req("GET", f"{path}{sep}per_page=500&page={page}")
            out += r.get("data", [])
            pag = r.get("meta", {}).get("pagination", {})
            if not pag or page >= pag.get("total_pages", 1):
                return out
            page += 1

    def bank_integration(self, iban, currency):
        if os.environ.get("IN_BANK_INTEGRATION_ID"):
            return os.environ["IN_BANK_INTEGRATION_ID"], False
        name = f"Erste Bank {iban}"
        for bi in self.all("bank_integrations"):
            if not bi.get("is_deleted") and (bi.get("bank_account_name") == name or iban in (bi.get("bank_account_number") or "")):
                return bi["id"], False
        return name, True  # needs creation

    def create_bank_integration(self, iban, currency):
        r = self.req("POST", "bank_integrations", {
            "bank_account_name": f"Erste Bank {iban}",
            "bank_account_number": iban,
            "provider_name": "Erste Bank (CAMT import)",
            "currency": currency,
        })
        return r["data"]["id"]


# ---------------------------------------------------------------- country / VAT
EU = set("AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split())
CARD_CC_RE = re.compile(r"^(?:POS|E-COMM|ATM)\s+[\d.,]+\s+([A-Z]{2,3})\s+K\d")
VAT = {  # code -> (rate, label for the UI, note stored on the expense)
    "20": (20, "20 % USt", ""),
    "13": (13, "13 % USt", ""),
    "10": (10, "10 % USt", ""),
    "0": (0, "0 % – steuerfrei (Versicherung, Bank, SVS)", "steuerfrei"),
    "rc": (0, "Reverse Charge (Leistung aus dem Ausland)", "Reverse Charge – USt als Leistungsempfänger (§19 UStG) erklären"),
    "foreign": (0, "Ausländische USt – nicht abzugsfähig", "ausländische USt, kein Vorsteuerabzug in AT"),
    "nodeduct": (0, "Kein Vorsteuerabzug (PKW)", "PKW – kein Vorsteuerabzug"),
}


def booking_country(t):
    """country of the payee: card country code, USD card -> US, IBAN country, bank-internal -> AT"""
    m = CARD_CC_RE.search(t["description"])
    if m:
        cc = m.group(1)
        return "US" if cc in ("USD", "US") else cc
    if t["participant"][:2].isalpha():
        return t["participant"][:2].upper()
    return "AT"


# ---------------------------------------------------------------- invoice matching
INV_RE = re.compile(r"(?<!\d)(20\d\d)\s*[/-]\s*0*(\d{1,6})\b")            # 2026/000174, 2026-000181, 2026/173
RN_RE = re.compile(r"\b(?:RN|Re|Rg|Rechnung\w*)[:.\s]*(?:Nr\.?\s*)?0*(\d{2,6})\b", re.I)  # "RN 182" (no year)


def norm_invoice_no(n):
    m = re.match(r"\s*(20\d\d)\s*[/-]\s*0*(\d+)\s*$", n or "")
    return f"{m.group(1)}/{int(m.group(2))}" if m else (n or "").strip()


def invoice_refs(t):
    refs = [f"{y}/{int(n)}" for y, n in INV_RE.findall(t["description"])]
    if not refs:
        refs = [f"{t['date'][:4]}/{int(n)}" for n in RN_RE.findall(t["description"])]
    return list(dict.fromkeys(refs))


class Matcher:
    """Exact matching only: every invoice number in the reference must exist, be open
    (sent/partial) and the open balances must add up to the booked amount to the cent."""

    def __init__(self, api):
        self.api = api
        self.open = {}
        for i in api.all("invoices"):
            if not i.get("is_deleted") and int(i.get("status_id") or 0) in (2, 3) and float(i.get("balance") or 0) > 0:
                self.open[norm_invoice_no(i["number"])] = i
        self.used = set()
        self.load_paid()

    def find(self, t):
        if t["base_type"] != "CREDIT":
            return None, "outgoing"
        if override(t):
            return None, f"override: {override(t)['action']}"
        refs = invoice_refs(t)
        if not refs:
            return None, "no invoice number in reference"
        invs = [self.open.get(r) for r in refs]
        if None in invs:
            return None, f"no open invoice {', '.join(r for r, i in zip(refs, invs) if i is None)}"
        if any(i["id"] in self.used for i in invs):
            return None, "invoice already matched in this run"
        total = sum(float(i["balance"]) for i in invs)
        if abs(total - float(t["amount"])) > 0.005:
            return None, f"amount {t['amount']} != open {total:.2f}"
        return invs, "ok"

    def take(self, invs):
        self.used.update(i["id"] for i in invs)

    def load_paid(self):
        """invoices already marked paid in Invoice Ninja whose payment is not linked to a bank booking yet"""
        self.inv_all = {norm_invoice_no(i["number"]): i for i in self.api.all("invoices") if not i.get("is_deleted")}
        self.free_pay = collections.defaultdict(list)
        for p in self.api.all("payments?include=paymentables"):
            if p.get("is_deleted") or p.get("transaction_id"):
                continue
            for x in p.get("paymentables") or []:
                self.free_pay[x.get("invoice_id")].append(p)
            self.free_pay[("number", p["number"])].append(p)

    def find_paid(self, t):
        """-> (payment, reason): the one existing unlinked payment for the referenced invoice(s), same amount"""
        if t["base_type"] != "CREDIT":
            return None, "outgoing"
        o = override(t)
        if o:
            if o["action"] == "replace_payment":
                return None, f"replace payment {o['payment']} by {t['amount']} on invoice {o['invoice']}"
            if o["action"] != "link_payment":
                return None, f"{o['action']}: {o.get('note', '')}"
            ps = self.free_pay.get(("number", o["payment"]))
            return (ps[0], "override") if ps else (None, f"override: payment {o['payment']} not found or already linked")
        refs = invoice_refs(t)
        invs = [self.inv_all.get(r) for r in refs]
        if not refs or None in invs:
            return None, "no invoice"
        pays = {p["id"]: p for i in invs for p in self.free_pay.get(i["id"], [])}
        cand = [p for p in pays.values() if abs(float(p["amount"]) - float(t["amount"])) <= 0.005
                and p["id"] not in self.used]
        if len(cand) != 1:
            got = ", ".join(f"{p['number']} {float(p['amount']):.2f}" for p in pays.values()) or "none"
            return None, f"invoice {', '.join(i['number'] for i in invs)} already paid; unlinked payments: {got}"
        return cand[0], "ok"


RUNS = HERE / "runs"
# overrides.json (local, not in git): special cases per booking, keyed by "bankref|CRDT/DBIT|amount"
#   {"action": "passthrough", "note": "..."}               no invoice / no expense, note added to the text
#   {"action": "link_payment", "payment": "2026/000123"}   link to this existing payment (amount may differ)
#   {"action": "expense", "vendor": "...", "category": "...", "vat": "rc", "note": "..."}
#   {"action": "replace_payment", "payment": "2026/000123", "invoice": "2026/000456"}
#       existing payment has the wrong amount: delete it, pay the invoice with the bank amount instead
#       (rollback re-creates the old payment; Invoice Ninja cannot restore deleted payments)
OVERRIDES = {}


def override(t):
    return OVERRIDES.get(base_ref(t["ref"]))
STATUS = {1: "unmatched", 2: "matched", 3: "converted"}


def api_from_env():
    url, token = os.environ.get("IN_URL"), os.environ.get("IN_TOKEN")
    if not url or not token:
        sys.exit("IN_URL / IN_TOKEN missing (put them in .env, see .env.example)")
    return Ninja(url, token)


def list_runs():
    runs = sorted(RUNS.glob("*.json")) if RUNS.exists() else []
    if not runs:
        print("no import runs logged")
    for r in runs:
        d = json.loads(r.read_text())
        print(f"  {r.stem}  {len(d['created']):4} transactions  {'ROLLED BACK' if d.get('rolled_back') else 'active'}")


class ExpenseMapper:
    """Outgoing bookings -> expenses, using expense_map.json {counterparty: {vendor, category, action}}.
    Links to an already existing, unlinked expense (same vendor + amount) instead of creating a duplicate."""

    def __init__(self, api, mapping):
        self.api, self.map = api, mapping
        self.vendors = {v["name"].strip().lower(): v["id"] for v in api.all("vendors") if not v.get("is_deleted")}
        self.cats = {x["name"].strip().lower(): x["id"] for x in api.all("expense_categories") if not x.get("is_deleted")}
        vname = {v: k for k, v in self.vendors.items()}
        self.free = [e for e in api.all("expenses")
                     if not e.get("is_deleted") and not e.get("transaction_id") and e.get("vendor_id") in vname]
        for e in self.free:
            e["_vendor"] = vname[e["vendor_id"]]
        self.links = {}
        self.created_vendors, self.created_cats = [], []
        # tax names as configured in Invoice Ninja (Settings -> Tax Rates)
        rates = [r for r in api.all("tax_rates") if not r.get("is_deleted")]
        by_rate = {float(r["rate"]): r["name"] for r in rates if float(r["rate"]) > 0}
        by_name = {r["name"].lower(): r["name"] for r in rates}
        self.tax_name = {
            "20": by_rate.get(20.0, "USt 20%"), "13": by_rate.get(13.0, "USt 13%"), "10": by_rate.get(10.0, "USt 10%"),
            "0": by_name.get("steuerbefreit", ""), "rc": by_name.get("reverse charge", ""),
            "foreign": "", "nodeduct": "",
        }

    def rule(self, t):
        o = override(t)
        if o:
            return o if o["action"] == "expense" else {"action": "skip", "vendor": "", "category": o["action"]}
        return self.map.get(party_of(t))

    def plan_links(self, items):
        """assign each free expense to the booking (same vendor+amount) closest in date"""
        from datetime import date
        d = lambda s: date.fromisoformat(s[:10])
        for e in self.free:
            cands = [t for t in items if t["base_type"] == "DEBIT" and (r := self.rule(t)) and r["action"] == "expense"
                     and r["vendor"].strip().lower() == e["_vendor"]
                     and abs(float(t["amount"]) - float(e["amount"])) < 0.005 and t["ref"] not in self.links]
            if cands:
                best = min(cands, key=lambda t: abs((d(t["date"]) - d(e["date"])).days))
                self.links[best["ref"]] = e

    def plan(self, t):
        if t["base_type"] != "DEBIT":
            return ("none", None)
        r = self.rule(t)
        if not r:
            return ("unmapped", None)
        if r["action"] != "expense":
            return ("skip", r)
        if t["ref"] in self.links:
            return ("link", self.links[t["ref"]])
        return ("create", r)

    def _ensure(self, kind, name):
        table, path = (self.vendors, "vendors") if kind == "vendor" else (self.cats, "expense_categories")
        key = name.strip().lower()
        if key not in table:
            body = {"name": name.strip()}
            if kind == "category":
                body["color"] = "#2f6b4f"
            table[key] = self.api.req("POST", path, body)["data"]["id"]
            (self.created_vendors if kind == "vendor" else self.created_cats).append(table[key])
            print(f"  created {kind} '{name}'")
        return table[key]

    def apply(self, tx_id, t):
        kind, obj = self.plan(t)
        if kind == "link":
            self.api.req("POST", "bank_transactions/match", {"transactions": [{"id": tx_id, "expense_id": obj["id"]}]})
            return {"expense_id": obj["id"], "expense_linked": True}
        if kind == "create":
            vid, cid = self._ensure("vendor", obj["vendor"]), self._ensure("category", obj["category"])
            self.api.req("POST", "bank_transactions/match", {"transactions": [
                {"id": tx_id, "vendor_id": vid, "ninja_category_id": cid}]})
            x = self.api.req("GET", f"bank_transactions/{tx_id}")["data"]
            res = {"expense_id": x.get("expense_id"), "expense_created": True}
            code = obj.get("vat")
            if code in VAT and res["expense_id"]:
                rate, _, note = VAT[code]
                e = self.api.req("GET", f"expenses/{res['expense_id']}")["data"]
                e.update(uses_inclusive_taxes=True, calculate_tax_by_amount=False,
                         tax_name1=self.tax_name.get(code, ""), tax_rate1=rate,
                         private_notes=" · ".join(p for p in [obj.get("note"), note, f"Land: {booking_country(t)}",
                                                              e.get("private_notes")] if p))
                self.api.req("PUT", f"expenses/{res['expense_id']}", e)
                res["vat"] = code
            return res
        return None


def rollback(run, commit):
    runs = sorted(RUNS.glob("*.json")) if RUNS.exists() else []
    active = [r for r in runs if not json.loads(r.read_text()).get("rolled_back")]
    f = (active[-1] if active else None) if run == "last" else RUNS / f"{run}.json"
    if not f or not f.exists():
        sys.exit(f"run not found: {run} (use --runs to list)")
    d = json.loads(f.read_text())
    if d.get("rolled_back"):
        sys.exit(f"run {f.stem} was already rolled back")
    api = api_from_env()
    ok, linked = [], []
    for c in d["created"]:
        x = api.req("GET", f"bank_transactions/{c['id']}")["data"]
        if x.get("is_deleted"):
            continue
        st = int(x.get("status_id") or 1)
        ours = (c.get("payment_id") and x.get("payment_id") == c["payment_id"]) or \
               (c.get("expense_id") and c["expense_id"] in (x.get("expense_id") or ""))
        (ok if st in (1, 2) or ours else linked).append((c, x, st))
    exist = [c for c, _, _ in ok if c.get("existing_tx")]
    ok = [o for o in ok if not o[0].get("existing_tx")]
    pays = [c for c, _, _ in ok if c.get("payment_id") and not c.get("payment_linked")]
    pay_link = [c for c, _, _ in ok if c.get("payment_linked")]
    exp_new = [c for c, _, _ in ok if c.get("expense_created")]
    exp_link = [c for c, _, _ in ok if c.get("expense_linked")]
    print(f"run {f.stem}: {len(d['created'])} created -> delete {len(ok)} transactions, "
          f"{len(pays)} payments (invoices reopen), {len(exp_new)} expenses; "
          f"unlink {len(exp_link)} pre-existing expenses and {len(pay_link)} pre-existing payments (kept, old date restored); "
          f"{len(d.get('created_vendors', []))} vendors, {len(d.get('created_categories', []))} categories created by the run")
    for c, x, st in linked:
        print(f"  KEEP {x['date']} {x['amount']:>10} {STATUS.get(st, st)}  {x['description'][:70]}")
    if linked:
        print("  -> you matched these yourself: undo the match/expense in Invoice Ninja first, then run rollback again")
    if not commit:
        print("\ndry run - add --commit to delete")
        return

    def bulk(path, action, ids):
        for k in range(0, len(ids), 100):
            api.req("POST", f"{path}/bulk", {"action": action, "ids": ids[k:k + 100]})

    if exist:  # transactions from an earlier run: unlink + restore text, keep the transaction
        bulk("bank_transactions", "unlink", [c["id"] for c in exist if c.get("expense_id")])
        bulk("expenses", "delete", [e for c in exist if c.get("expense_created")
                                    for e in (c.get("expense_id") or "").split(",") if e])
        for c in exist:
            x = api.req("GET", f"bank_transactions/{c['id']}")["data"]
            api.req("PUT", f"bank_transactions/{c['id']}", {
                "bank_integration_id": x["bank_integration_id"], "date": x["date"], "amount": x["amount"],
                "base_type": x["base_type"], **c["old"]})
        print(f"  existing transactions: unlinked {len(exist)}, their created expenses deleted, text restored")
    bulk("payments", "delete", [c["payment_id"] for c in pays])
    for c in pays:  # payment replaced by this run: re-create it (deleted payments cannot be restored)
        rp = c.get("replaced_payment")
        if rp:
            body = {k: rp[k] for k in ("client_id", "amount", "date", "type_id", "transaction_reference") if rp.get(k)}
            body.update(invoices=rp["invoices"],
                        private_notes=" · ".join(p for p in [rp.get("private_notes"), f"ersetzt {rp['number']} (Rollback)"] if p))
            r = api.req("POST", "payments", body)["data"]
            print(f"  re-created payment {rp['number']} as {r['number']}")
    bulk("expenses", "delete", [e for c in exp_new for e in (c.get("expense_id") or "").split(",") if e])
    for c in exp_link:  # pre-existing expense: only detach it from the transaction
        e = api.req("GET", f"expenses/{c['expense_id']}")["data"]
        e["transaction_id"] = ""
        api.req("PUT", f"expenses/{c['expense_id']}", e)
    for c in pay_link:  # pre-existing payment: restore its date; deleting the transaction detaches it
        if c.get("payment_old_date"):
            api.req("PUT", f"payments/{c['payment_id']}", {"date": c["payment_old_date"]})
    bulk("bank_transactions", "delete", [c["id"] for c, _, _ in ok])
    if not linked:
        bulk("vendors", "delete", d.get("created_vendors", []))
        bulk("expense_categories", "delete", d.get("created_categories", []))
    state = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    state -= {c["ref"] for c, _, _ in ok}
    STATE.write_text(json.dumps(sorted(state)))
    d["created"] = [c for c, _, _ in linked]
    d["rolled_back"] = not linked
    f.write_text(json.dumps(d, indent=1))
    print(f"done: deleted {len(pays)} payments, {len(exp_new)} expenses, {len(ok)} transactions; "
          f"unlinked {len(exp_link)} expenses; they can be imported again")


def run_existing(api, items, expm, commit, label):
    """already imported bookings that are still unmatched in Invoice Ninja: add expenses afterwards
    (and refresh text/counterparty, e.g. PayPal merchant). Rollback unlinks instead of deleting."""
    ids = {}
    for f in sorted(RUNS.glob("*.json")) if RUNS.exists() else []:
        for e in json.loads(f.read_text()).get("created", []):
            ids[e["ref"]] = e["id"]
    todo = []
    for t in items:
        if t["ref"] not in ids:
            continue
        x = api.req("GET", f"bank_transactions/{ids[t['ref']]}")["data"]
        if x.get("is_deleted") or int(x.get("status_id") or 1) != 1:
            continue
        todo.append((t, x))
    expm.plan_links([t for t, _ in todo])
    plans = [(t, x, *expm.plan(t)) for t, x in todo]
    cnt = collections.Counter(k for *_, k, _ in plans)
    print(f"\nexisting unmatched bookings: {len(todo)} -> create {cnt['create']} expenses, link {cnt['link']}, "
          f"no expense {cnt['skip'] + cnt['none']}, NOT IN MAP {cnt['unmapped']}")
    for t, x, k, o in plans:
        what = {"create": lambda: f"{o['vendor']} [{o['category']}] {VAT.get(o.get('vat'), (0, '-'))[1]}",
                "link": lambda: f"existing expense {o.get('number')}",
                "skip": lambda: "no expense", "none": lambda: "incoming - unchanged",
                "unmapped": lambda: "NOT IN MAP - unchanged"}[k]()
        print(f"  {t['date']} -{t['amount']:>8} {party_of(t)[:34]:34} -> {what}")
    if not commit:
        print("\ndry run - re-run with --commit")
        return
    RUNS.mkdir(exist_ok=True)
    run_id = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    log = {"run": run_id, "label": label, "created": [], "created_vendors": [], "created_categories": []}
    run_file = RUNS / f"{run_id}.json"
    for t, x, k, o in plans:
        entry = {"id": x["id"], "ref": t["ref"], "existing_tx": True,
                 "old": {"description": x.get("description"), "participant_name": x.get("participant_name")}}
        changed = (t["description"], ninja_participant(t)) != (x.get("description"), x.get("participant_name"))
        if changed:
            api.req("PUT", f"bank_transactions/{x['id']}", {
                "bank_integration_id": x["bank_integration_id"], "date": x["date"], "amount": x["amount"],
                "base_type": x["base_type"], "description": t["description"], "participant_name": ninja_participant(t)})
        res = expm.apply(x["id"], t) if k in ("create", "link") else None
        if res:
            entry.update(res)
        if changed or res:
            log["created"].append(entry)
            log["created_vendors"], log["created_categories"] = expm.created_vendors, expm.created_cats
            run_file.write_text(json.dumps(log, indent=1))
    print(f"updated {len(log['created'])} existing transactions; run id {run_id} - undo: ./camt2ninja.py --rollback {run_id} --commit")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--credits-only", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--only", help="ninja_selection.json from review.html: import only bookings ticked as business")
    ap.add_argument("--limit", type=int, help="import at most N bookings (for test runs)")
    ap.add_argument("--match", action="store_true",
                    help="link incoming payments to open invoices (exact number + amount) -> payment, invoice paid")
    ap.add_argument("--expenses", metavar="MAP",
                    help="expense_map.json from expenses.html: turn outgoing bookings into expenses")
    ap.add_argument("--debits-only", action="store_true")
    ap.add_argument("--existing", action="store_true",
                    help="only already imported, still unmatched bookings: add expenses / refresh text afterwards")
    ap.add_argument("--overrides", metavar="FILE", help="special cases per booking (default: overrides.json if present)")
    ap.add_argument("--runs", action="store_true", help="list logged import runs")
    ap.add_argument("--rollback", metavar="RUN", help="delete everything a run created ('last' or run id); add --commit")
    a = ap.parse_args()
    load_env()

    if a.runs:
        return list_runs()
    if a.rollback:
        return rollback(a.rollback, a.commit)
    if not a.paths:
        ap.error("give CAMT file(s) or folder")

    ov_file = Path(a.overrides) if a.overrides else HERE / "overrides.json"
    if ov_file.exists():
        OVERRIDES.update({k: v for k, v in json.loads(ov_file.read_text()).items() if not k.startswith("_")})
    txs = collect(a.paths)
    for t in txs:
        o = override(t)
        if o and o.get("note"):
            t["description"] = f"{t['description']} [{o['note']}]"[:500]
    forced = {t["ref"] for t in txs if override(t)}
    if a.only:
        dec = json.loads(Path(a.only).read_text())["decisions"]
        undecided = [t for t in txs if t["ref"] not in dec]
        if undecided:
            print(f"warning: {len(undecided)} bookings not in {a.only} (not reviewed) -> skipped; re-run make_review.py")
        txs = [t for t in txs if dec.get(t["ref"]) is True or t["ref"] in forced]
    if a.credits_only:
        txs = [t for t in txs if t["base_type"] == "CREDIT"]
    if a.debits_only:
        txs = [t for t in txs if t["base_type"] == "DEBIT"]
    if a.since:
        txs = [t for t in txs if t["date"] >= a.since]
    state = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    if a.existing:
        if not a.expenses:
            ap.error("--existing needs --expenses MAP")
        api = api_from_env()
        return run_existing(api, [t for t in txs if t["ref"] in state],
                            ExpenseMapper(api, json.loads(Path(a.expenses).read_text())), a.commit, "existing")
    new = [t for t in txs if t["ref"] not in state]
    if a.limit:
        new = new[:a.limit]

    cr = sum(float(t["amount"]) for t in new if t["base_type"] == "CREDIT")
    db = sum(float(t["amount"]) for t in new if t["base_type"] == "DEBIT")
    print(f"selected {len(txs)} bookings, {len(txs) - len([t for t in txs if t['ref'] not in state])} already imported, "
          f"{len(new)} to import{' (limited)' if a.limit else ''} (credits {cr:.2f}, debits {db:.2f})")
    if not (a.match or a.expenses):
        for t in new[:15]:
            sign = "+" if t["base_type"] == "CREDIT" else "-"
            print(f"  {t['date']} {sign}{t['amount']:>10} {t['description'][:90]}")
        if len(new) > 15:
            print(f"  ... {len(new) - 15} more")

    if not new:
        return
    api = api_from_env() if (a.commit or a.match or a.expenses) else None
    matcher = None
    if a.match:
        matcher = Matcher(api)
        plan, why = [], {}
        links = []
        for t in new:
            invs, reason = matcher.find(t)
            if invs:
                matcher.take(invs)
                plan.append((t, invs))
            elif t["base_type"] == "CREDIT":
                pay, r2 = matcher.find_paid(t)
                if pay:
                    matcher.used.add(pay["id"])
                    links.append((t, pay))
                else:
                    why[t["ref"]] = reason if r2 == "no invoice" else r2
        print(f"\nmatch: {len(plan)} of {sum(t['base_type'] == 'CREDIT' for t in new)} incoming payments -> open invoice, "
              f"{len(links)} -> existing payment (invoice already paid)")
        for t, invs in plan:
            print(f"  {t['date']} +{t['amount']:>9} {ninja_participant(t)[:26]:26} -> {', '.join(i['number'] for i in invs)}")
        for t, pay in links:
            print(f"  {t['date']} +{t['amount']:>9} {ninja_participant(t)[:26]:26} -> link payment {pay['number']}"
                  + (f" (date {pay['date']} -> {t['date']})" if pay["date"] != t["date"] else ""))
        for t in new:
            if t["ref"] in why:
                print(f"  {t['date']} +{t['amount']:>9} {ninja_participant(t)[:26]:26}    stays unmatched: {why[t['ref']]}")
        matcher.used.clear()

    expm = None
    if a.expenses:
        expm = ExpenseMapper(api, json.loads(Path(a.expenses).read_text()))
        expm.plan_links(new)
        plans = [(t, *expm.plan(t)) for t in new if t["base_type"] == "DEBIT"]
        cnt = collections.Counter(k for _, k, _ in plans)
        print(f"\nexpenses: {len(plans)} outgoing -> create {cnt['create']}, link to existing {cnt['link']}, "
              f"no expense {cnt['skip']}, NOT IN MAP {cnt['unmapped']}")
        for t, k, o in plans:
            if k == "link":
                print(f"  link   {t['date']} -{t['amount']:>9} {party_of(t)[:28]:28} -> existing expense {o.get('number') or o['id']} ({o['date']})")
        for t, k, o in plans:
            if k == "unmapped":
                print(f"  ??     {t['date']} -{t['amount']:>9} {party_of(t)[:28]:28} not in {a.expenses} -> stays unmatched")
        by = collections.defaultdict(lambda: [0, 0.0])
        vat_sum = collections.defaultdict(float)
        for t, k, o in plans:
            if k == "create":
                key = (o["vendor"], o["category"], o.get("vat", "-"))
                by[key][0] += 1
                by[key][1] += float(t["amount"])
                rate = VAT.get(o.get("vat"), (0,))[0]
                vat_sum[o.get("vat", "-")] += float(t["amount"]) * rate / (100 + rate)
        for (v, cat, vat), (n, s) in sorted(by.items(), key=lambda x: -x[1][1]):
            print(f"  create {n:3}x {s:9.2f}  {v[:28]:28} [{cat}] {VAT.get(vat, (0, 'NO VAT RULE'))[1]}")
        print(f"  input VAT (Vorsteuer) from these expenses: {sum(vat_sum.values()):.2f} EUR")
        nv = sorted({o["vendor"] for _, k, o in plans if k == "create" and o["vendor"].strip().lower() not in expm.vendors})
        nc = sorted({o["category"] for _, k, o in plans if k == "create" and o["category"].strip().lower() not in expm.cats})
        print(f"  new vendors ({len(nv)}): {', '.join(nv) or '-'}")
        print(f"  new categories ({len(nc)}): {', '.join(nc) or '-'}")

    if not a.commit:
        print("\ndry run - re-run with --commit to import")
        return

    RUNS.mkdir(exist_ok=True)
    run_id = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    run_file = RUNS / f"{run_id}.json"
    log = {"run": run_id, "created": [], "created_vendors": [], "created_categories": []}

    def save_log():
        if expm:
            log["created_vendors"], log["created_categories"] = expm.created_vendors, expm.created_cats
        run_file.write_text(json.dumps(log, indent=1))

    by_iban = {}
    for t in new:
        by_iban.setdefault((t["iban"], t["currency"]), []).append(t)

    for (iban, ccy), items in by_iban.items():
        bi_id, create = api.bank_integration(iban, ccy)
        if create:
            bi_id = api.create_bank_integration(iban, ccy)
            print(f"created bank account 'Erste Bank {iban}' ({bi_id})")
        # remote duplicate guard (in case imported_refs.json was lost); ignore deleted ones
        existing = {(x.get("date"), f"{float(x.get('amount', 0)):.2f}", x.get("base_type"), x.get("description"))
                    for x in api.all(f"bank_transactions?bank_integration_id={bi_id}")
                    if x.get("bank_integration_id") == bi_id and not x.get("is_deleted")}
        ok = matched = expensed = 0
        for t in items:
            key = (t["date"], t["amount"], t["base_type"], t["description"])
            if key not in existing:
                r = api.req("POST", "bank_transactions", {
                    "bank_integration_id": bi_id,
                    "amount": t["amount"],
                    "currency_id": CURRENCY_IDS.get(ccy, "3"),
                    "date": t["date"],
                    "base_type": t["base_type"],
                    "description": t["description"],
                    "participant": t["participant"],
                    "participant_name": ninja_participant(t),
                })
                entry = {"id": r["data"]["id"], "ref": t["ref"]}
                log["created"].append(entry)
                save_log()
                ok += 1
                o = override(t)
                if matcher and o and o["action"] == "replace_payment":
                    old = next(p for p in api.all("payments?include=paymentables")
                               if p["number"] == o["payment"] and not p.get("is_deleted"))
                    inv = api.req("GET", f"invoices?number={o['invoice']}")["data"][0]
                    entry["replaced_payment"] = {k: old.get(k) for k in (
                        "number", "client_id", "amount", "date", "type_id", "transaction_reference", "private_notes")}
                    entry["replaced_payment"]["invoices"] = [{"invoice_id": x["invoice_id"], "amount": x["amount"]}
                                                             for x in old.get("paymentables") or []]
                    save_log()
                    api.req("POST", "payments/bulk", {"action": "delete", "ids": [old["id"]]})
                    api.req("POST", "bank_transactions/match", {"transactions": [
                        {"id": entry["id"], "invoice_ids": inv["id"]}]})
                    x = api.req("GET", f"bank_transactions/{entry['id']}")["data"]
                    entry.update(payment_id=x.get("payment_id"), invoice_numbers=[inv["number"]])
                    save_log()
                    matched += 1
                    print(f"  replaced payment {o['payment']} -> {t['amount']} on {inv['number']}")
                elif matcher:
                    invs, _ = matcher.find(t)
                    if invs:
                        api.req("POST", "bank_transactions/match", {"transactions": [
                            {"id": entry["id"], "invoice_ids": ",".join(i["id"] for i in invs)}]})
                        matcher.take(invs)
                        x = api.req("GET", f"bank_transactions/{entry['id']}")["data"]
                        entry.update(payment_id=x.get("payment_id"), invoice_numbers=[i["number"] for i in invs])
                        save_log()
                        matched += 1
                        print(f"  matched {t['date']} +{t['amount']} -> {', '.join(entry['invoice_numbers'])}"
                              f" (payment {entry['payment_id']}, status {STATUS.get(int(x.get('status_id') or 0))})")
                    else:
                        pay, _ = matcher.find_paid(t)
                        if pay:
                            api.req("POST", "bank_transactions/match", {"transactions": [
                                {"id": entry["id"], "payment_id": pay["id"]}]})
                            matcher.used.add(pay["id"])
                            entry.update(payment_id=pay["id"], payment_linked=True, payment_number=pay["number"],
                                         payment_old_date=pay["date"])
                            if pay["date"] != t["date"]:
                                api.req("PUT", f"payments/{pay['id']}", {"date": t["date"]})
                            save_log()
                            matched += 1
                            print(f"  linked {t['date']} +{t['amount']} -> existing payment {pay['number']}")
                if expm:
                    res = expm.apply(entry["id"], t)
                    if res:
                        entry.update(res)
                        save_log()
                        expensed += 1
            state.add(t["ref"])
            STATE.write_text(json.dumps(sorted(state)))
        print(f"{iban}: imported {ok}, matched {matched} to invoices, {expensed} expenses, "
              f"skipped {len(items) - ok} already present remotely")
    print(f"run id {run_id} - undo with: ./camt2ninja.py --rollback {run_id} --commit")


if __name__ == "__main__":
    main()
