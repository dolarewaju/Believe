"""M.A.R.C local server. Serves the dashboard and a small JSON API over your private data.

Binds to 127.0.0.1 only, so nothing outside this laptop can reach it.
  python scripts/server.py            # then open http://127.0.0.1:8765/MARC/
"""
import json
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobs  # noqa: E402
import invest  # noqa: E402

ROOT = jobs.ROOT
PORT = 8765
STATUSES = {"queued", "approved", "dismissed", "applied", "viewed", "interview", "rejected", "offer"}
BLOCKED = ("/private", "/.venv", "/.git", "/.claude")
DOWNLOADABLE = ("/private/cv/out/",)


# ------------------------------------------------------------------ voice: headless Claude Code

CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
PY = ".venv/Scripts/python.exe"
VOICE_ALLOWED = [
    "Read", "Grep", "Glob", "WebSearch", "WebFetch", "Skill",
    f"Bash({PY} scripts/marc.py:*)",
    f"Bash({PY} scripts/invest.py instruments)",
    "mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread", "mcp__claude_ai_Gmail__get_message",
]
VOICE_DENIED = [
    "Write", "Edit", "NotebookEdit", "Read(./private/keys.env)", "Bash(git:*)",
    "mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Gmail__reply", "mcp__claude_ai_Gmail__forward",
    "mcp__claude_ai_Gmail__create_draft", "mcp__claude_ai_Gmail__trash_message", "mcp__claude_ai_Gmail__trash_thread",
]
VOICE_SYSTEM = """You are M.A.R.C (Malone Autonomous Response Centre), the voice assistant inside your owner's personal command centre.
Always address the user as "Malone" (never by their legal first name). Their legal name appears in CVs and applications; use it only there.
The user is speaking. Their words come from speech recognition, so allow for mis-heard names and numbers.

Reply for speech: one to three short sentences in plain British English. No markdown, lists, links, code or emojis. Say numbers naturally.

Do all reading and changes with `.venv/Scripts/python.exe scripts/marc.py <command>` (run `--help` once if unsure). Look up job or card ids before changing anything. If a request is ambiguous, ask one short question instead of guessing.
- Import job alerts: follow .claude/skills/import-job-alerts/SKILL.md. Read Gmail with search and get tools only, then add jobs with `marc.py import-alerts '<json>'`.
- Research investments: follow .claude/skills/research-investments/SKILL.md. Check tickers with `marc.py check-ticker`, add cards with `marc.py add-cards '<json>'`. Research and facts, never advice.

You must not submit job applications, place trades, send, reply to or forward emails, edit files directly, use git, or read out keys or full contact details. If asked for any of these, say briefly that it isn't available by voice and point to the M.A.R.C screen."""
VOICE_TASKS = {}
VOICE_LOCK = threading.Lock()


def run_voice(task_id, text, session):
    cmd = [CLAUDE, "-p", "--output-format", "json", "--model", "sonnet",
           "--append-system-prompt", VOICE_SYSTEM + f"\nToday's date: {date.today().isoformat()}.",
           "--allowedTools", *VOICE_ALLOWED, "--disallowedTools", *VOICE_DENIED]
    if session:
        cmd += ["--resume", session]
    try:
        p = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
                           timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(p.stdout or "{}")
        jobs.log_activity("voice", f"“{text[:80]}”", status="error" if data.get("is_error") else "ok")
        VOICE_TASKS[task_id] = {
            "status": "error" if data.get("is_error") else "done",
            "reply": data.get("result") or (p.stderr.strip()[-300:] or "Sorry, I couldn't get an answer."),
            "session": data.get("session_id") or session,
        }
    except subprocess.TimeoutExpired:
        jobs.log_activity("voice", f"“{text[:80]}” timed out", status="error")
        VOICE_TASKS[task_id] = {"status": "error", "reply": "That took too long, so I stopped. Try a smaller request.", "session": session}
    except Exception as e:
        VOICE_TASKS[task_id] = {"status": "error", "reply": f"Voice link problem: {e}", "session": session}
    finally:
        VOICE_LOCK.release()


def start_voice(text, session=None):
    if not Path(CLAUDE).exists() and not shutil.which("claude"):
        raise RuntimeError("Claude Code isn't installed. See Phase 1 in the guide.")
    if not VOICE_LOCK.acquire(blocking=False):
        raise RuntimeError("I'm still working on your last request.")
    task_id = uuid.uuid4().hex[:10]
    VOICE_TASKS[task_id] = {"status": "working"}
    threading.Thread(target=run_voice, args=(task_id, text.strip()[:2000], session), daemon=True).start()
    return task_id


def briefing_text():
    """Spoken daily briefing built from local data only (instant, no Claude usage)."""
    store = jobs.load_json(jobs.STORE, [])
    st = stats()
    today = date.today().isoformat()
    hour = __import__("datetime").datetime.now().hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    queued = sorted([j for j in store if j["status"] == "queued"], key=lambda j: -(j.get("score") or 0))
    due = [j for j in store if j.get("follow_up") and j["follow_up"] <= today and j["status"] in ("applied", "viewed")]
    cards = jobs.load_json(invest.CARDS, [])
    new_cards = sum(1 for c in cards if c["status"] == "new")
    parts = [f"{greet}, Malone."]
    parts.append(f"You've applied to {st['applied_total']} roles in total, {st['applied_week']} this week"
                 + (f" and {st['applied_today']} today." if st["applied_today"] else "."))
    if st["interviews"] or st["offers"]:
        parts.append(f"You have {st['interviews']} interviews and {st['offers']} offers on the go.")
    if queued:
        top = queued[0]
        parts.append(f"{len(queued)} new roles are waiting for approval. The top one is {top['title']} at {top['company'].split('(')[0].strip()}, scoring {top.get('score')}.")
    if st["approved"]:
        parts.append(f"{st['approved']} approved roles are ready to submit.")
    if due:
        parts.append(f"{len(due)} follow-ups are due, starting with {due[0]['company'].split('(')[0].strip()}.")
    else:
        parts.append("No follow-ups are due.")
    if new_cards:
        parts.append(f"You also have {new_cards} research cards to review.")
    return " ".join(parts)


def jobs_view():
    out = []
    for j in jobs.load_json(jobs.STORE, []):
        if j["status"] in ("new", "discarded", "scored"):
            continue
        note = ""
        if j.get("note_file") and (ROOT / j["note_file"]).exists():
            note = (ROOT / j["note_file"]).read_text(encoding="utf-8")
        out.append({**j, "description": (j.get("description") or "")[:700], "note": note})
    return out


def stats():
    store = jobs.load_json(jobs.STORE, [])
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    applied = [j for j in store if j.get("applied")]
    responded = [j for j in applied if j["status"] in ("viewed", "interview", "rejected", "offer")]
    by = lambda s: sum(1 for j in store if j["status"] == s)
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    daily = [{"date": d, "count": sum(1 for j in applied if j["applied"] == d)} for d in days]
    weekly = []
    for w in range(7, -1, -1):
        start = week_start - timedelta(weeks=w)
        end = start + timedelta(days=7)
        weekly.append({"week": start.isoformat(),
                       "count": sum(1 for j in applied if start.isoformat() <= j["applied"] < end.isoformat())})
    return {
        "daily": daily, "weekly": weekly,
        "viewed": by("viewed"), "rejected": by("rejected"),
        "found": len(store), "discarded": by("discarded"), "queued": by("queued"), "approved": by("approved"),
        "applied_today": sum(1 for j in applied if j["applied"] == today.isoformat()),
        "applied_week": sum(1 for j in applied if j["applied"] >= week_start.isoformat()),
        "applied_total": len(applied), "interviews": by("interview"), "offers": by("offer"),
        "response_rate": round(100 * len(responded) / len(applied)) if applied else 0,
        "follow_ups_due": sum(1 for j in store if j.get("follow_up") and j["follow_up"] <= today.isoformat()
                              and j["status"] in ("applied", "viewed")),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/status":
            k = jobs.keys()
            return self._json({"local": True, "reed": bool(k.get("REED_API_KEY")),
                               "adzuna": bool(k.get("ADZUNA_APP_ID") and k.get("ADZUNA_APP_KEY")),
                               "stats": stats()})
        if path == "/api/jobs":
            return self._json(jobs_view())
        if path == "/api/agents":
            return self._json(agents_view())
        if path == "/api/briefing":
            return self._json({"text": briefing_text()})
        if path == "/api/voice":
            task = parse_qs(urlparse(self.path).query).get("task", [""])[0]
            return self._json(VOICE_TASKS.get(task, {"status": "unknown"}))
        if path == "/api/invest":
            prof = jobs.load_json(ROOT / "private" / "profile.json", {}).get("investing", {})
            cache = jobs.load_json(invest.INSTRUMENTS, {})
            return self._json({"cards": jobs.load_json(invest.CARDS, []), "profile": prof,
                               "t212_list": cache.get("fetched"), "t212_count": len(cache.get("instruments", []))})
        if path.startswith(BLOCKED) and not path.startswith(DOWNLOADABLE):
            return self.send_error(403)
        if path.startswith(DOWNLOADABLE) and ".." in path:
            return self.send_error(403)
        return super().do_GET()

    def do_POST(self):
        # Same-origin only: refuse requests a web page on another site could forge.
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send_error(403)
        path = urlparse(self.path).path
        try:
            data = self._body()
            if path == "/api/jobs/update":
                return self._json(update(data))
            if path == "/api/voice":
                return self._json({"task": start_voice(data["text"], data.get("session"))})
            if path == "/api/invest/decide":
                return self._json(invest.decide(data["id"], data["decision"], data.get("reason", "")))
            if path == "/api/jobs/note":
                j = find(data["id"])
                (ROOT / j["note_file"]).write_text(data["text"], encoding="utf-8")
                return self._json({"ok": True})
            if path == "/api/jobs/add":
                added = jobs.add_link(data["url"], data["title"], data["company"], data.get("description", ""),
                                      data.get("source", "Manual"), data.get("location", ""))
                return self._json({"added": added})
            if path == "/api/run":
                step = data.get("step", "run")
                result = {}
                if step in ("fetch", "run"):
                    result["fetch"] = jobs.fetch()
                if step in ("score", "run"):
                    result["score"] = jobs.score()
                if step in ("tailor", "run"):
                    result["tailor"] = jobs.tailor()
                return self._json(result)
        except Exception as e:  # report to the dashboard instead of crashing
            return self._json({"error": str(e)}, 400)
        self.send_error(404)


def find(job_id):
    for j in jobs.load_json(jobs.STORE, []):
        if j["id"] == job_id:
            return j
    raise KeyError(job_id)


def update(data):
    status = data.get("status")
    if status and status not in STATUSES:
        raise ValueError(f"Unknown status {status}")
    ids = set(data["ids"])
    store = jobs.load_json(jobs.STORE, [])
    today = date.today().isoformat()
    for j in store:
        if j["id"] not in ids:
            continue
        if status:
            j["status"] = status
            j.setdefault("history", []).append({"status": status, "date": today})
            if status == "approved":
                j["approved"] = today
            if status == "applied" and not j.get("applied"):
                j["applied"] = today
                j["follow_up"] = (date.today() + timedelta(days=7)).isoformat()
        for field in ("notes", "follow_up", "company", "title"):
            if field in data:
                j[field] = data[field]
    jobs.save_json(jobs.STORE, store)
    if status == "applied" and data.get("via") == "assisted-apply":
        names = [f"{j['title']} at {j['company'].split('(')[0].strip()}" for j in store if j["id"] in ids]
        jobs.log_activity("browser", "Filled and you submitted: " + "; ".join(names), applied=len(names))
    return {"updated": len(ids)}


# ------------------------------------------------------------------ agents overview

AGENTS = [
    {"id": "job-scout", "name": "Job Scout", "icon": "search", "schedule": "Weekdays 8am",
     "what": "Reads your job-alert emails and adds new roles to the queue."},
    {"id": "cv-tailor", "name": "CV Tailor", "icon": "doc", "schedule": "After every import",
     "what": "Scores each role and builds a tailored CV and cover note."},
    {"id": "approvals", "name": "Approvals", "icon": "check", "schedule": "Waits for you",
     "what": "Roles that need your yes or no before applying."},
    {"id": "browser", "name": "Browser", "icon": "globe", "schedule": "When you say \"start applying\"",
     "what": "Fills employer application forms in Chrome. You click Submit."},
    {"id": "researcher", "name": "Researcher", "icon": "chart", "schedule": "Mondays 9am",
     "what": "Researches 3 new stock ideas and adds research cards."},
    {"id": "watcher", "name": "Watcher", "icon": "eye", "schedule": "Mondays 9am",
     "what": "Checks your YES watchlist for major news."},
    {"id": "voice", "name": "Voice", "icon": "mic", "schedule": "When you talk",
     "what": "Answers and acts on what you say to M.A.R.C."},
]


def next_run(agent_id):
    from datetime import datetime
    now = datetime.now()
    if agent_id == "job-scout":
        d = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if d <= now:
            d += timedelta(days=1)
        while d.weekday() > 4:
            d += timedelta(days=1)
        return d.isoformat(timespec="minutes")
    if agent_id in ("researcher", "watcher"):
        d = now.replace(hour=9, minute=0, second=0, microsecond=0)
        while d.weekday() != 0 or d <= now:
            d += timedelta(days=1)
        return d.isoformat(timespec="minutes")
    return None


def agents_view():
    from datetime import datetime
    store = jobs.load_json(jobs.STORE, [])
    cards = jobs.load_json(invest.CARDS, [])
    log = jobs.read_activity(400)
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

    def per_day(dates):
        return [sum(1 for x in dates if x and x[:10] == d) for d in days]

    def last(agent):
        rows = [e for e in log if e["agent"] == agent]
        return rows[-1] if rows else None

    assisted = [j for j in store if j.get("applied") and "assisted apply" in (j.get("notes") or "").lower()]
    found = [j.get("found") for j in store if j.get("source") not in ("Manual", "Voice")]
    queued_at = [j.get("queued") for j in store if j.get("queued")]
    voice_rows = [e for e in log if e["agent"] == "voice"]
    queued = [j for j in store if j["status"] == "queued"]
    new_cards = [c for c in cards if c["status"] == "new"]
    out = []
    for a in AGENTS:
        aid, rec = a["id"], last(a["id"])
        v = {**a, "next_run": next_run(aid), "last": rec, "status": "idle", "metric": "", "week": [0] * 7}
        if aid == "job-scout":
            v["week"] = per_day(found)
            v["metric"] = f"{sum(v['week'])} roles found this week"
            due = datetime.now().weekday() < 5 and datetime.now().hour >= 9
            if rec and rec["status"] == "error":
                v["status"] = "error"
            elif due and not any(e["agent"] == "job-scout" and e["ts"][:10] == today.isoformat() for e in log):
                v["status"] = "attention"
                v["note"] = "No import logged today. Is the Claude app open? Run it from Scheduled in the Claude sidebar."
        elif aid == "cv-tailor":
            v["week"] = per_day(queued_at)
            v["metric"] = f"{sum(v['week'])} CVs and notes this week"
        elif aid == "approvals":
            v["week"] = per_day([j.get("approved") for j in store])
            v["metric"] = f"{len(queued)} waiting · {sum(v['week'])} approved this week"
            v["status"] = "waiting" if queued else "idle"
            if queued:
                oldest = min(j.get("found", "") for j in queued)
                v["note"] = f"Oldest has waited since {oldest}."
        elif aid == "browser":
            v["week"] = per_day([j["applied"] for j in assisted])
            v["metric"] = f"{len(assisted)} filled in total · {sum(v['week'])} this week"
            if assisted and not rec:
                lastj = max(assisted, key=lambda j: j["applied"])
                v["last"] = {"ts": lastj["applied"], "summary": f"{lastj['title']} at {lastj['company'].split('(')[0].strip()}", "status": "ok"}
        elif aid == "researcher":
            v["week"] = per_day([c.get("created") for c in cards])
            v["metric"] = f"{len(new_cards)} cards to review · {len(cards)} researched"
            v["status"] = "waiting" if new_cards else "idle"
            if not rec and cards:
                v["last"] = {"ts": max(c["created"] for c in cards), "summary": f"{len(cards)} cards so far", "status": "ok"}
        elif aid == "watcher":
            yes = [c["name"] for c in cards if c["status"] == "yes"]
            v["metric"] = f"Watching {len(yes)} on your YES list"
            v["week"] = per_day([e["ts"] for e in log if e["agent"] == "watcher"])
        elif aid == "voice":
            v["week"] = per_day([e["ts"] for e in voice_rows])
            v["metric"] = f"{v['week'][-1]} requests today · {sum(v['week'])} this week"
            if VOICE_LOCK.locked():
                v["status"] = "working"
        if rec and rec.get("status") == "error" and v["status"] == "idle":
            v["status"] = "error"
        out.append(v)
    return {"agents": out, "days": days, "log": list(reversed(log[-40:]))}


if __name__ == "__main__":
    print(f"M.A.R.C running at http://127.0.0.1:{PORT}/MARC/  (close this window to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
