"""Job pipeline: fetch -> score -> tailor -> queue.

  python scripts/jobs.py fetch            # Reed + Adzuna official APIs (keys in private/keys.env)
  python scripts/jobs.py score            # score every new job 0-100, discard poor matches
  python scripts/jobs.py tailor           # build CV + cover note for each kept job
  python scripts/jobs.py run              # all three
  python scripts/jobs.py add URL --title T --company C [--description D] [--source S]

LinkedIn, Indeed, Totaljobs, Welcome to the Jungle and Handshake are never scraped.
Jobs from those sites arrive via their alert emails or links you add yourself.
"""
import argparse
import base64
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_cv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PRIV = ROOT / "private"
STORE = PRIV / "jobs" / "jobs.json"
NOTES = PRIV / "jobs" / "notes"
CONFIG = PRIV / "jobs" / "config.json"

DEFAULT_CONFIG = {
    "api_sources": [],  # add "reed" and/or "adzuna" to switch the official APIs back on
    "location": "London",
    "distance_miles": 25,
    "salary_floor": 24000,
    "keep_threshold": 60,
    "max_days_old": 7,
    "queries": [
        "sales development representative", "business development representative",
        "graduate sales", "graduate business development", "medical sales representative",
        "pharmaceutical sales representative", "medical device sales", "inside sales",
        "lead generation", "customer success associate", "junior account manager",
        "client onboarding", "trainee sales",
    ],
    "exclude_words": ["commission only", "commission-only", "commission based only", "commission-based only",
                      "100% commission", "uncapped commission only", "ote only", "self-employed",
                      "door to door", "door-to-door"],
}
SENIOR = re.compile(r"\b(senior|sr\.?|head of|director|principal|vp|vice president|manager of|global account|"
                    r"enterprise|strategic account|large cust\w*)\b", re.I)
LANGUAGE = re.compile(r"\b(french|german|spanish|italian|dutch|arabic|mandarin|portuguese|polish|japanese)[- ]speak", re.I)
EXPERIENCE = re.compile(r"\b([3-9]|1\d)\+?\s*(?:-\s*\d+\s*)?years?'?(?:\s+[\w-]+){0,4}?\s+experience", re.I)
ENTRY = re.compile(r"\b(graduate|entry[- ]level|junior|trainee|associate|no experience|apprentice)\b", re.I)


# ------------------------------------------------------------------ storage

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


ACTIVITY = PRIV / "activity.jsonl"


def log_activity(agent, summary, status="ok", **detail):
    """Append one line to the private activity log shown on M.A.R.C's Agents page."""
    ACTIVITY.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": agent, "status": status,
             "summary": summary, **detail}
    with ACTIVITY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_activity(limit=200):
    if not ACTIVITY.exists():
        return []
    lines = ACTIVITY.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG, {}))
    if not CONFIG.exists():
        save_json(CONFIG, cfg)
    return cfg


def keys():
    out = {}
    path = PRIV / "keys.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def job_id(source, ext_id, url):
    return hashlib.sha1(f"{source}|{ext_id or url}".encode()).hexdigest()[:12]


def dedupe_key(job):
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return norm(job["title"]) + "|" + norm(job["company"])


def merge(new_jobs):
    store = load_json(STORE, [])
    seen_ids = {j["id"] for j in store}
    seen_keys = {dedupe_key(j) for j in store}
    added = 0
    for j in new_jobs:
        if j["id"] in seen_ids or dedupe_key(j) in seen_keys:
            continue
        j.setdefault("status", "new")
        j.setdefault("found", date.today().isoformat())
        store.append(j)
        seen_ids.add(j["id"])
        seen_keys.add(dedupe_key(j))
        added += 1
    save_json(STORE, store)
    return added


# ------------------------------------------------------------------ fetch (official APIs only)

def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "MARC-personal/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_reed(cfg, key):
    auth = base64.b64encode(f"{key}:".encode()).decode()
    out = []
    for q in cfg["queries"]:
        params = urllib.parse.urlencode({"keywords": q, "locationName": cfg["location"],
                                         "distanceFromLocation": cfg["distance_miles"], "resultsToTake": 100})
        data = _get(f"https://www.reed.co.uk/api/1.0/search?{params}", {"Authorization": f"Basic {auth}"})
        for r in data.get("results", []):
            out.append({
                "id": job_id("reed", r.get("jobId"), r.get("jobUrl")), "source": "Reed",
                "title": r.get("jobTitle", ""), "company": r.get("employerName", ""),
                "location": r.get("locationName", ""), "salary_min": r.get("minimumSalary"),
                "salary_max": r.get("maximumSalary"), "url": r.get("jobUrl", ""),
                "description": r.get("jobDescription", ""), "posted": r.get("date", ""),
            })
    return out


def fetch_adzuna(cfg, app_id, app_key):
    out = []
    for q in cfg["queries"]:
        params = urllib.parse.urlencode({"app_id": app_id, "app_key": app_key, "what": q,
                                         "where": cfg["location"], "distance": round(cfg["distance_miles"] * 1.6),
                                         "results_per_page": 50, "max_days_old": cfg["max_days_old"]})
        data = _get(f"https://api.adzuna.com/v1/api/jobs/gb/search/1?{params}")
        for r in data.get("results", []):
            out.append({
                "id": job_id("adzuna", r.get("id"), r.get("redirect_url")), "source": "Adzuna",
                "title": r.get("title", ""), "company": (r.get("company") or {}).get("display_name", ""),
                "location": (r.get("location") or {}).get("display_name", ""),
                "salary_min": r.get("salary_min"), "salary_max": r.get("salary_max"),
                "url": r.get("redirect_url", ""), "description": r.get("description", ""),
                "posted": r.get("created", ""),
            })
    return out


def fetch():
    cfg, k = config(), keys()
    found, notes = [], []
    sources = cfg.get("api_sources", [])
    if not sources:
        notes.append("API sources off: jobs come from alert emails and links you add")
    if "reed" in sources:
        if k.get("REED_API_KEY"):
            found += fetch_reed(cfg, k["REED_API_KEY"])
        else:
            notes.append("Reed skipped: no REED_API_KEY in private/keys.env")
    if "adzuna" in sources:
        if k.get("ADZUNA_APP_ID") and k.get("ADZUNA_APP_KEY"):
            found += fetch_adzuna(cfg, k["ADZUNA_APP_ID"], k["ADZUNA_APP_KEY"])
        else:
            notes.append("Adzuna skipped: no ADZUNA_APP_ID / ADZUNA_APP_KEY in private/keys.env")
    added = merge(found)
    return {"fetched": len(found), "added": added, "notes": notes}


def clean_url(url):
    """Strip tracking and one-time login tokens from job-board links."""
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", url or "")
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    m = re.search(r"indeed\.com/.*?[?&]jk=([0-9a-f]+)", url or "")
    if m:
        return f"https://uk.indeed.com/viewjob?jk={m.group(1)}"
    parts = urllib.parse.urlsplit(url or "")
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
            if not re.match(r"(utm_|trk|token|otp|mid|lipi|ref|eid|tracking)", k, re.I)]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(keep), ""))


def make_job(url, title, company, description="", source="Manual", location="", salary_min=None, salary_max=None):
    url = clean_url(url)
    return {"id": job_id(source.lower(), None, url), "source": source, "title": title.strip(),
            "company": (company or "").strip(), "location": (location or "").strip(),
            "salary_min": salary_min, "salary_max": salary_max, "url": url,
            "description": description or "", "posted": ""}


def add_link(url, title, company, description="", source="Manual", location="", salary_min=None):
    return merge([make_job(url, title, company, description, source, location, salary_min)])


ALERT_NOISE = re.compile(r"^(apply with resume.*|easy apply|actively recruiting|promoted|be an early applicant|"
                         r"\d+ (?:company )?alumni.*|new|view all jobs.*|https?://.*|-+)$", re.I)


def parse_linkedin_alert(text):
    """Pull jobs out of a LinkedIn job-alert email (plain-text body)."""
    out = []
    for block in re.split(r"\n-{10,}\n", text):
        m = re.search(r"View job:\s*(\S+)", block)
        if not m:
            continue
        before = block[:m.start()].strip().splitlines()
        lines = [l.strip() for l in before if l.strip() and not ALERT_NOISE.match(l.strip())]
        if len(lines) >= 3:
            title, company, location = lines[-3], lines[-2], lines[-1]
        elif len(lines) == 2:
            title, company, location = lines[0], lines[1], ""
        else:
            continue
        out.append(make_job(m.group(1), title, company, source="LinkedIn", location=location))
    return out


def import_alerts(items):
    """items: [{"source": "LinkedIn", "text": "<plain-text email>"} | {"title":…, "company":…, "url":…, …}]"""
    found = []
    for it in items:
        if "text" in it and it.get("source", "").lower() == "linkedin":
            found += parse_linkedin_alert(it["text"])
        elif it.get("title") and it.get("url"):
            found.append(make_job(it["url"], it["title"], it.get("company", ""), it.get("description", ""),
                                  it.get("source", "Alert"), it.get("location", ""),
                                  it.get("salary_min"), it.get("salary_max")))
    added = merge(found)
    sources = sorted({j["source"] for j in found})
    log_activity("job-scout", f"Read alerts: {len(found)} roles found, {added} new" + (f" ({', '.join(sources)})" if sources else ""),
                 parsed=len(found), added=added)
    return {"parsed": len(found), "added": added}


# ------------------------------------------------------------------ score

def tokens(s):
    return set(re.findall(r"[a-z]+", (s or "").lower())) - {"and", "the", "of", "a", "to", "in", "for"}


# Other names employers use for the same entry-level jobs -> the target role they map to.
ALIASES = {
    "account specialist": "Trainee Medical Sales Representative",
    "hospital sales representative": "Trainee Medical Sales Representative",
    "healthcare representative": "Trainee Medical Sales Representative",
    "medical representative": "Trainee Medical Sales Representative",
    "territory manager": "Trainee Field Sales Representative",
    "dental sales representative": "Trainee Medical Device Sales Representative",
    "account development representative": "Sales Development Representative (SDR)",
    "sales associate": "Graduate Sales Executive",
    "new business executive": "Business Development Executive (Graduate / Junior)",
    "recruitment consultant": "Trainee Recruitment Consultant",
    "graduate recruitment consultant": "Trainee Recruitment Consultant",
    "recruitment resourcer": "Trainee Recruitment Consultant",
    "associate recruitment consultant": "Trainee Recruitment Consultant",
}


def best_role(title, roles):
    t = tokens(title)
    best, best_overlap = None, 0.0
    by_title = {r["title"]: r for r in roles}
    candidates = [(r, r["title"]) for r in roles]
    candidates += [(by_title[target], alias) for alias, target in ALIASES.items() if target in by_title]
    for role, name in candidates:
        r = tokens(re.sub(r"\(.*?\)", "", name))
        overlap = len(t & r) / max(len(r), 1)
        if overlap > best_overlap:
            best, best_overlap = role, overlap
    return best, best_overlap


def score_job(job, roles, cfg):
    """Return (score 0-100, matched role, reasons)."""
    reasons = []
    text = f"{job['title']} {job.get('description', '')}".lower()
    role, overlap = best_role(job["title"], roles)
    if not role:
        return 0, None, ["Title doesn't match any of your 20 roles"]
    score = 30 + round(30 * overlap)
    reasons.append(f"Matches '{role['title']}' ({round(overlap * 100)}% title match)")

    if job.get("description"):
        hits = [kw for kw in role["keywords"] if kw.lower() in text]
        score += min(20, len(hits) * 4)
        if hits:
            reasons.append("Keywords: " + ", ".join(hits[:6]))
    else:
        score += 10  # alert emails carry no description; don't punish that
        reasons.append("Scored on title only: check the advert")

    if ENTRY.search(text):
        score += 10
        reasons.append("Entry-level wording")
    if SENIOR.search(job["title"]):
        score -= 40
        reasons.append("Senior title")
    lang = LANGUAGE.search(text)
    if lang:
        score -= 35
        reasons.append(f"Needs {lang.group(1).title()} speaker")
    m = EXPERIENCE.search(text)
    if m:
        score -= 25
        reasons.append(f"Asks for '{m.group(0)}'")

    sal = job.get("salary_max") or job.get("salary_min")
    if sal and sal < cfg["salary_floor"]:
        score -= 60  # hard rule: below the floor is discarded
        reasons.append(f"Salary £{int(sal):,} below your £{cfg['salary_floor']:,} floor")
    elif sal:
        score += 5
    else:
        reasons.append("Salary not stated: check it's at least £{:,}".format(cfg["salary_floor"]))

    loc = (job.get("location") or "").lower() + " " + text[:600]
    if any(w in loc for w in ("london", "remote", "essex", "hybrid", "thurrock")):
        score += 5
    for w in cfg["exclude_words"]:
        if w in text:
            score -= 50
            reasons.append(f"Excluded: '{w}'")
    return max(0, min(100, score)), role, reasons


def score():
    cfg = config()
    roles = load_json(PRIV / "role-targets.json", {"roles": []})["roles"]
    store = load_json(STORE, [])
    kept = dropped = 0
    for j in store:
        if j["status"] != "new":
            continue
        s, role, why = score_job(j, roles, cfg)
        j.update(score=s, role=role["title"] if role else None, reasons=why)
        if s >= cfg["keep_threshold"]:
            j["status"] = "scored"
            kept += 1
        else:
            j["status"] = "discarded"
            dropped += 1
    save_json(STORE, store)
    return {"kept": kept, "discarded": dropped}


# ------------------------------------------------------------------ tailor

def role_key(role_title, master):
    """Map a role-targets title (e.g. 'Sales Development Representative (SDR)') to a master.json role."""
    clean = re.sub(r"\s*\(.*?\)", "", role_title or "").strip()
    for r in master["roles"]:
        if r.lower() in clean.lower() or clean.lower() in r.lower():
            return r
    best, _ = best_role(clean, [{"title": r} for r in master["roles"]])
    return best["title"] if best else "Graduate Sales Executive"


PITCH = {
    "tech_sales": "I manage 5 client relationships in my current role, where I've taken part in several deals and contract signings. I've also done cold calling and prospecting, including on CBRE's sales programme.",
    "pharma_sales": "I studied Pharmaceutical Science at the University of Birmingham, hold A grades in A-level Biology and Chemistry, and now manage 5 client relationships in a sales-facing role.",
    "fintech_client": "I manage 5 client relationships and have taken part in several deals and contract signings, with finance exposure from GHO Capital and Citi's finance programme.",
    "account_customer": "I manage 5 client relationships and turn their requirements into daily priorities for my team, so I know what keeps a client happy.",
    "recruitment": "I manage 5 client relationships, have taken part in several deals and contract signings, and I've done cold calling. I also guided around 20 students one-to-one through Sixth Form applications, and every one of them received an offer.",
    "commercial_ops": "I've worked across client sales, warehouse operations and investment research, and used data analysis on BCG's programme to produce business recommendations.",
}


def cover_note(job, master, role):
    variant = master["roles"][role]
    name = master["contact"]["name"]
    company = job["company"] or "your team"
    return (f"Dear Hiring Team,\n\n"
            f"I'm applying for the {job['title']} role at {company}. {PITCH[variant]}\n\n"
            f"I was named one of Powerful Media's Top 150 Future Black Leaders, I'm based in Essex within easy reach of London, "
            f"hold a full UK driving licence and can start immediately. I'd welcome the chance to talk.\n\n"
            f"Kind regards,\n{name}")


def tailor():
    master = json.loads(build_cv.MASTER.read_text(encoding="utf-8"))
    store = load_json(STORE, [])
    NOTES.mkdir(parents=True, exist_ok=True)
    made = 0
    for j in store:
        if j["status"] != "scored":
            continue
        role = role_key(j.get("role"), master)
        cv = build_cv.make(master, role=role)
        note_path = NOTES / f"{j['id']}.txt"
        if not note_path.exists():  # keep any personalised note Claude wrote
            note_path.write_text(cover_note(j, master, role), encoding="utf-8")
        j.update(cv_role=role, cv_file=str(cv.relative_to(ROOT)).replace("\\", "/"),
                 note_file=str(note_path.relative_to(ROOT)).replace("\\", "/"),
                 status="queued", queued=datetime.now().isoformat(timespec="minutes"))
        made += 1
    save_json(STORE, store)
    if made:
        log_activity("cv-tailor", f"Prepared {made} tailored CV{'s' if made != 1 else ''} and cover note{'s' if made != 1 else ''}", made=made)
    return {"queued": made}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("fetch", "score", "tailor", "run"):
        sub.add_parser(c)
    sub.add_parser("import", help="read alert items as JSON from stdin, then score + tailor")
    a = sub.add_parser("add")
    a.add_argument("url")
    a.add_argument("--title", required=True)
    a.add_argument("--company", required=True)
    a.add_argument("--description", default="")
    a.add_argument("--source", default="Manual")
    a.add_argument("--location", default="")
    args = ap.parse_args()
    if args.cmd == "import":
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
        print(import_alerts(json.loads(raw))); print(score()); print(tailor())
    elif args.cmd == "add":
        print({"added": add_link(args.url, args.title, args.company, args.description, args.source, args.location)})
    elif args.cmd == "run":
        print(fetch()); print(score()); print(tailor())
    else:
        print({"fetch": fetch, "score": score, "tailor": tailor}[args.cmd]())
