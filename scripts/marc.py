"""M.A.R.C command toolkit: the only actions voice commands may take.

Claude (running headless for voice) is allowed to call this script and nothing that writes elsewhere.
Every subcommand prints JSON.

  marc.py status                         totals for jobs and research
  marc.py briefing                       data for a spoken daily briefing
  marc.py jobs [--status S] [--limit N]  list jobs (default: queued + approved)
  marc.py find TEXT                      search jobs by company or title
  marc.py set-status ID [ID ...] --to S [--notes TEXT]
                                         S: approved, dismissed, applied, viewed, interview, rejected, offer, queued
  marc.py import-alerts JSON             add jobs from alert emails (list of {source,title,company,location,url,...})
  marc.py add-job URL TITLE COMPANY [--description D] [--source S]
  marc.py cards [--status new|yes|no]    research cards
  marc.py decide ID yes|no|new [--reason TEXT]
  marc.py add-cards JSON                 add research card(s); same format as scripts/invest.py add
  marc.py check-ticker TICKER            is it on the Trading 212 instrument list?
  marc.py log AGENT "summary" [--status ok|error]
                                         record a run (agents: job-scout, researcher, watcher, browser, voice)
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import invest  # noqa: E402
import jobs  # noqa: E402
import server  # noqa: E402


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def brief_job(j):
    return {k: j.get(k) for k in ("id", "score", "status", "title", "company", "location", "source",
                                  "applied", "follow_up", "notes") if j.get(k) not in (None, "")}


def cmd_status(_):
    cards = jobs.load_json(invest.CARDS, [])
    st = server.stats()
    st.pop("daily", None)
    st.pop("weekly", None)
    out({"jobs": st, "research_cards": {s: sum(1 for c in cards if c["status"] == s) for s in ("new", "yes", "no")}})


def cmd_briefing(_):
    store = jobs.load_json(jobs.STORE, [])
    today = date.today().isoformat()
    st = server.stats()
    queued = sorted([j for j in store if j["status"] == "queued"], key=lambda j: -(j.get("score") or 0))
    due = [j for j in store if j.get("follow_up") and j["follow_up"] <= today and j["status"] in ("applied", "viewed")]
    cards = jobs.load_json(invest.CARDS, [])
    out({"date": today, "applied_today": st["applied_today"], "applied_this_week": st["applied_week"],
         "applied_total": st["applied_total"], "response_rate_pct": st["response_rate"],
         "interviews": st["interviews"], "offers": st["offers"],
         "awaiting_approval": len(queued), "top_queued": [brief_job(j) for j in queued[:3]],
         "ready_to_submit": st["approved"], "follow_ups_due": [brief_job(j) for j in due],
         "research_cards_to_review": sum(1 for c in cards if c["status"] == "new"),
         "watchlist": [c["name"] for c in cards if c["status"] == "yes"]})


def cmd_jobs(a):
    store = jobs.load_json(jobs.STORE, [])
    want = [a.status] if a.status else ["queued", "approved"]
    rows = [j for j in store if j["status"] in want]
    rows.sort(key=lambda j: -(j.get("score") or 0))
    out([brief_job(j) for j in rows[: a.limit]])


def cmd_find(a):
    q = a.text.lower()
    out([brief_job(j) for j in jobs.load_json(jobs.STORE, []) if q in (j["title"] + " " + j["company"]).lower()][:15])


def cmd_set_status(a):
    data = {"ids": a.ids, "status": a.to}
    if a.notes:
        data["notes"] = a.notes
    out(server.update(data))


def cmd_import_alerts(a):
    res = jobs.import_alerts(json.loads(a.json))
    out({"import": res, "score": jobs.score(), "tailor": jobs.tailor()})


def cmd_add_job(a):
    added = jobs.add_link(a.url, a.title, a.company, a.description, a.source)
    out({"added": added, "score": jobs.score(), "tailor": jobs.tailor()})


def cmd_cards(a):
    cards = jobs.load_json(invest.CARDS, [])
    rows = [c for c in cards if not a.status or c["status"] == a.status]
    out([{k: c.get(k) for k in ("id", "name", "ticker", "type", "status", "reason", "data_as_of")} | {
        "headline_numbers": c["numbers"][:4], "on_trading212": c["t212"]["status"]} for c in rows])


def cmd_decide(a):
    out(invest.decide(a.id, a.decision, a.reason or ""))


def cmd_add_cards(a):
    out(invest.add_cards(json.loads(a.json)))


def cmd_check_ticker(a):
    out(invest.find_instrument(a.ticker.upper()))


def cmd_log(a):
    if a.agent not in ("job-scout", "researcher", "watcher", "browser", "voice", "cv-tailor"):
        raise ValueError("unknown agent")
    jobs.log_activity(a.agent, a.summary[:300], status=a.status)
    out({"logged": True})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(f=cmd_status)
    sub.add_parser("briefing").set_defaults(f=cmd_briefing)
    p = sub.add_parser("jobs"); p.add_argument("--status"); p.add_argument("--limit", type=int, default=15); p.set_defaults(f=cmd_jobs)
    p = sub.add_parser("find"); p.add_argument("text"); p.set_defaults(f=cmd_find)
    p = sub.add_parser("set-status"); p.add_argument("ids", nargs="+"); p.add_argument("--to", required=True); p.add_argument("--notes"); p.set_defaults(f=cmd_set_status)
    p = sub.add_parser("import-alerts"); p.add_argument("json"); p.set_defaults(f=cmd_import_alerts)
    p = sub.add_parser("add-job"); p.add_argument("url"); p.add_argument("title"); p.add_argument("company")
    p.add_argument("--description", default=""); p.add_argument("--source", default="Voice"); p.set_defaults(f=cmd_add_job)
    p = sub.add_parser("cards"); p.add_argument("--status"); p.set_defaults(f=cmd_cards)
    p = sub.add_parser("decide"); p.add_argument("id"); p.add_argument("decision"); p.add_argument("--reason"); p.set_defaults(f=cmd_decide)
    p = sub.add_parser("add-cards"); p.add_argument("json"); p.set_defaults(f=cmd_add_cards)
    p = sub.add_parser("check-ticker"); p.add_argument("ticker"); p.set_defaults(f=cmd_check_ticker)
    p = sub.add_parser("log", help="record an agent run on the Agents page")
    p.add_argument("agent"); p.add_argument("summary"); p.add_argument("--status", default="ok", choices=["ok", "error"])
    p.set_defaults(f=cmd_log)
    args = ap.parse_args()
    try:
        args.f(args)
    except Exception as e:  # report errors as JSON so the voice assistant can explain them
        out({"error": str(e)})
        sys.exit(1)
