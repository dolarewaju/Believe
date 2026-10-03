"""Module 2: investment research cards + Trading 212 availability check.

Research and facts only. This module never places trades and never gives personal advice.

  python scripts/invest.py instruments          # refresh the Trading 212 instrument list (needs read-only API key)
  python scripts/invest.py check VWRP VUAG      # is each ticker available on your Trading 212 account?
  python scripts/invest.py add < card.json      # add research card(s) (a JSON object or list)

Trading 212 API credentials go in private/keys.env as T212_API_KEY and T212_API_SECRET.
Create the key in the Stocks ISA account with read-only permissions only, never "orders".
"""
import base64
import hashlib
import json
import re
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobs import keys, load_json, save_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "private" / "invest"
CARDS = DIR / "cards.json"
INSTRUMENTS = DIR / "t212-instruments.json"
T212 = "https://live.trading212.com/api/v0/equity/metadata/instruments"
REQUIRED = ("name", "type", "what", "case_for", "case_against", "numbers", "risks", "sources")


def refresh_instruments():
    k = keys()
    key, secret = k.get("T212_API_KEY"), k.get("T212_API_SECRET")
    if not key:
        return {"error": "No T212_API_KEY in private/keys.env"}
    auth = f"{key}:{secret}" if secret else key
    header = "Basic " + base64.b64encode(auth.encode()).decode() if secret else key
    req = urllib.request.Request(T212, headers={"Authorization": header, "User-Agent": "MARC-personal/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    save_json(INSTRUMENTS, {"fetched": datetime.now().isoformat(timespec="minutes"), "instruments": data})
    return {"instruments": len(data)}


def find_instrument(symbol=None, isin=None):
    """Match a plain ticker (e.g. VWRP) or ISIN against the cached Trading 212 list."""
    cache = load_json(INSTRUMENTS, {})
    if not cache:
        return None
    sym = (symbol or "").upper()
    best, best_score = None, 0
    for ins in cache["instruments"]:
        t212 = ins.get("ticker", "")
        m = re.match(r"([A-Z0-9.]+)([a-z]?)\d*_", t212)
        base, venue = (m.group(1), m.group(2)) if m else ("", "")
        hit_isin = bool(isin) and ins.get("isin") == isin
        hit_sym = bool(sym) and (ins.get("shortName", "").upper() == sym or base == sym)
        if not (hit_isin or hit_sym):
            continue
        # Prefer the exact ticker, then London ("l") and US listings over other exchanges.
        score = 4 * (base == sym) + 2 * hit_isin + 2 * (venue == "l" or t212.endswith("_US_EQ")) + 1
        if score > best_score:
            best, best_score = ins, score
    if not best:
        return {}
    # shortName is the real exchange ticker (e.g. NBIS); "ticker" is Trading 212's internal ID,
    # which can keep a company's old symbol (e.g. YNDX_US_EQ for Nebius).
    return {"symbol": best.get("shortName"), "t212_id": best.get("ticker"), "name": best.get("name"),
            "isin": best.get("isin"), "currency": best.get("currencyCode"), "type": best.get("type")}


def availability(card):
    found = find_instrument(card.get("ticker"), card.get("isin"))
    if found is None:
        return {"status": "unverified", "note": "Trading 212 list not loaded yet (needs read-only API key)",
                "checked": date.today().isoformat()}
    if found:
        return {"status": "available", "checked": date.today().isoformat(), **found}
    return {"status": "not_found", "note": "Not in your Trading 212 account's instrument list",
            "checked": date.today().isoformat()}


def add_cards(items):
    items = items if isinstance(items, list) else [items]
    cards = load_json(CARDS, [])
    have = {c["id"] for c in cards}
    added = []
    for c in items:
        missing = [f for f in REQUIRED if not c.get(f)]
        if missing:
            raise ValueError(f"{c.get('name', '?')}: missing {', '.join(missing)}")
        if any(not s.get("date") or not s.get("url") for s in c["sources"]):
            raise ValueError(f"{c['name']}: every source needs a url and a date")
        c["id"] = c.get("id") or hashlib.sha1((c.get("isin") or c.get("ticker") or c["name"]).encode()).hexdigest()[:10]
        if c["id"] in have:
            continue
        c.setdefault("status", "new")
        c.setdefault("created", date.today().isoformat())
        c["t212"] = availability(c)
        cards.append(c)
        added.append(c["name"])
    save_json(CARDS, cards)
    if added:
        from jobs import log_activity
        log_activity("researcher", f"Added {len(added)} research card{'s' if len(added) != 1 else ''}: {', '.join(added)}", added=len(added))
    return {"added": added}


def recheck_all():
    cards = load_json(CARDS, [])
    for c in cards:
        c["t212"] = availability(c)
    save_json(CARDS, cards)
    return {s: sum(1 for c in cards if c["t212"]["status"] == s) for s in ("available", "not_found", "unverified")}


def decide(card_id, decision, reason=""):
    if decision not in ("yes", "no", "new"):
        raise ValueError("decision must be yes, no or new")
    cards = load_json(CARDS, [])
    for c in cards:
        if c["id"] == card_id:
            c["status"] = decision
            c["decided"] = date.today().isoformat() if decision != "new" else None
            c["reason"] = reason
            save_json(CARDS, cards)
            return {"ok": True}
    raise KeyError(card_id)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "instruments":
        print(refresh_instruments()); print(recheck_all())
    elif cmd == "check":
        for s in sys.argv[2:]:
            print(s, find_instrument(s))
    elif cmd == "add":
        print(add_cards(json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))))
    else:
        print(__doc__)
