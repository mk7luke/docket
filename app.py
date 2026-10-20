"""Docket: write the decision, and the date you will open it again.

Owner link edits. Share link reads. Public MIT.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
DB = DATA / "docket.db"
BIND_HOST = os.environ.get("DOCKET_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("DOCKET_PORT", "8886"))

RATE_LIMIT = 12
RATE_WINDOW = 3600
MAX_DECISIONS = 80
ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OUTCOMES = {
    "kept": "Kept it",
    "changed": "Changed it",
    "unsure": "Still unsure",
}

_hits: dict[str, list[float]] = {}
_page_cache: dict[str, tuple[float, str]] = {}


class DocketError(ValueError):
    pass


def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()[:64]
    if request.client and request.client.host:
        return request.client.host
    return "local"


def _rate_ok(ip: str) -> bool:
    now = time.time()
    window = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    if len(window) >= RATE_LIMIT:
        _hits[ip] = window
        return False
    window.append(now)
    _hits[ip] = window
    return True


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def connect() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS dockets (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              owner_hash TEXT NOT NULL,
              share_token TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
              id TEXT PRIMARY KEY,
              docket_id TEXT NOT NULL REFERENCES dockets(id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              choice TEXT NOT NULL,
              alternative TEXT NOT NULL DEFAULT '',
              why TEXT NOT NULL DEFAULT '',
              revisit_on TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT '',
              outcome_note TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              closed_at INTEGER
            );
            """
        )


def _check_id(value: str) -> str:
    if not ID_RE.match(value or ""):
        raise DocketError("That link is not valid.")
    return value


def today_for(request: Request) -> date:
    raw = (request.cookies.get("docket_day") or "").strip()
    if DAY_RE.match(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def parse_day(raw: str) -> str:
    text = (raw or "").strip()
    if not DAY_RE.match(text):
        raise DocketError("Pick a date.")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise DocketError("Pick a real date.") from exc
    if parsed.year < 2000 or parsed.year > 2100:
        raise DocketError("Pick a date between 2000 and 2100.")
    return text


def due_label(revisit_on: str, today: date, outcome: str) -> tuple[str, str]:
    if outcome:
        return OUTCOMES.get(outcome, "Closed"), "closed"
    try:
        revisit = date.fromisoformat(revisit_on)
    except ValueError:
        return "Date missing", "late"
    delta = (revisit - today).days
    if delta == 0:
        return "Open today", "due"
    if delta < 0:
        n = -delta
        unit = "day" if n == 1 else "days"
        return f"{n} {unit} late", "late"
    if delta == 1:
        return "Opens tomorrow", "wait"
    return f"Opens in {delta} days", "wait"


def require_owner(conn: sqlite3.Connection, docket_id: str, key: str) -> sqlite3.Row:
    _check_id(docket_id)
    if not key or len(key) > 80:
        raise DocketError("That owner link is not valid.")
    row = conn.execute("SELECT * FROM dockets WHERE id = ?", (docket_id,)).fetchone()
    if row is None or not secrets.compare_digest(row["owner_hash"], _hash_key(key)):
        raise DocketError("That owner link is not valid.")
    return row


def require_share(conn: sqlite3.Connection, docket_id: str, token: str) -> sqlite3.Row:
    _check_id(docket_id)
    if not token or len(token) > 80:
        raise DocketError("That share link is not valid.")
    row = conn.execute("SELECT * FROM dockets WHERE id = ?", (docket_id,)).fetchone()
    if row is None or len(token) != len(row["share_token"]) or not secrets.compare_digest(row["share_token"], token):
        raise DocketError("That share link is not valid.")
    return row


def _clean(text: str, limit: int, label: str, required: bool = False) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if required and not value:
        raise DocketError(f"{label} needs a few words.")
    if len(value) > limit:
        raise DocketError(f"{label} is too long.")
    return value


def _asset_version(*paths: Path) -> str:
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass
    return str(int(latest))


def _shell(name: str) -> str:
    html_path = STATIC / name
    css_path = STATIC / "style.css"
    js_path = STATIC / "home.js"
    stamp = max(
        html_path.stat().st_mtime,
        css_path.stat().st_mtime if css_path.exists() else 0.0,
        js_path.stat().st_mtime if js_path.exists() else 0.0,
    )
    cached = _page_cache.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    page = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    page = re.sub(
        r"""[ \t]*<link[^>]+href=["']/static/style\.css[^"']*["'][^>]*>\s*""",
        "",
        page,
        flags=re.I,
    )
    page = re.sub(r"</head>", f'<style id="docket-css">\n{css}\n</style>\n</head>', page, count=1, flags=re.I)
    version = _asset_version(js_path, css_path)
    page = re.sub(
        r"""src=(["'])/static/home\.js(?:\?[^"']*)?\1""",
        rf"src=\1/static/home.js?v={version}\1",
        page,
    )
    _page_cache[name] = (stamp, page)
    return page


def render(name: str, **values: str) -> str:
    page = _shell(name)
    for key, value in values.items():
        page = page.replace(f"<!--{key}-->", value)
    return page


def _status(text: str, kind: str = "") -> tuple[str, str]:
    return html.escape(text), f" {kind}" if kind else ""


def _fmt_day(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%b %-d, %Y")
    except ValueError:
        return iso


def _cards(rows: list[sqlite3.Row], today: date, href_base: str) -> str:
    if not rows:
        return '<p class="hint">Nothing here.</p>'
    bits = []
    for row in rows:
        label, kind = due_label(row["revisit_on"], today, row["outcome"])
        bits.append(
            f'<a class="card {kind}" href="{html.escape(href_base)}/n/{html.escape(row["id"])}">'
            f'<span class="stamp {kind}">{html.escape(label)}</span>'
            f'<strong>{html.escape(row["title"])}</strong>'
            f'<span class="picked">Picked {html.escape(row["choice"])}</span>'
            f'<span class="when">{html.escape(_fmt_day(row["revisit_on"]))}</span>'
            "</a>"
        )
    return "".join(bits)


app = FastAPI(title="Docket", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.middleware("http")
async def _headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )
    path = request.url.path
    if path.startswith("/d/") or path.startswith("/s/"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    ctype = (resp.media_type or resp.headers.get("content-type") or "").lower()
    if "html" in ctype:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "service": "docket"}


@app.get("/robots.txt")
def robots() -> Response:
    return Response((STATIC / "robots.txt").read_text(encoding="utf-8"), media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def home_page() -> HTMLResponse:
    return HTMLResponse(render("index.html", STATUS="", STATUS_CLASS="", NAME=""))


@app.post("/dockets")
def create_docket(request: Request, name: str = Form("")) -> Response:
    if not _rate_ok(_client_ip(request)):
        page = render("index.html", STATUS="Too many new dockets from this network. Try again later.", STATUS_CLASS=" bad", NAME=html.escape(name, quote=True))
        return HTMLResponse(page, status_code=429)
    title = _safe_name(name)
    if isinstance(title, Response):
        return title
    key = secrets.token_urlsafe(18)
    share = secrets.token_urlsafe(12)
    docket_id = secrets.token_urlsafe(6)
    with connect() as conn:
        conn.execute(
            "INSERT INTO dockets (id, name, owner_hash, share_token, created_at) VALUES (?, ?, ?, ?, ?)",
            (docket_id, title, _hash_key(key), share, int(time.time())),
        )
    return RedirectResponse(f"/d/{docket_id}/{key}", status_code=303)


def _safe_name(name: str) -> str | HTMLResponse:
    title = re.sub(r"\s+", " ", (name or "").strip())
    if not title or len(title) > 60:
        return HTMLResponse(
            render("index.html", STATUS="Name the docket. 60 characters or fewer.", STATUS_CLASS=" bad", NAME=html.escape(name, quote=True)),
            status_code=400,
        )
    return title


def _load_decisions(conn: sqlite3.Connection, docket_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM decisions WHERE docket_id = ? ORDER BY revisit_on, created_at",
        (docket_id,),
    ).fetchall()


def _board_html(
    docket: sqlite3.Row,
    rows: list[sqlite3.Row],
    today: date,
    *,
    owner_key: str = "",
    share: bool = False,
    due_only: bool = False,
    status: str = "",
    kind: str = "",
) -> str:
    if share:
        base = f"/s/{docket['id']}/{docket['share_token']}"
        mode = "Reading"
    else:
        base = f"/d/{docket['id']}/{owner_key}"
        mode = "Owner link"
    open_rows = [row for row in rows if not row["outcome"]]
    closed_rows = [row for row in rows if row["outcome"]]
    due_rows = [row for row in open_rows if date.fromisoformat(row["revisit_on"]) <= today]
    if due_only:
        shown_open = due_rows
        heading = "Open these"
        lede = "The date has arrived. Read what you wrote, then say what happened."
        closed_block = ""
    else:
        shown_open = sorted(open_rows, key=lambda row: (date.fromisoformat(row["revisit_on"]) > today, row["revisit_on"]))
        heading = docket["name"]
        if due_rows:
            n = len(due_rows)
            lede = f"{n} to open." if n > 1 else "1 to open."
        elif open_rows:
            nxt = min(open_rows, key=lambda row: row["revisit_on"])
            lede = f"Next open date is {_fmt_day(nxt['revisit_on'])}."
        else:
            lede = "No open decisions. Write the next one before the reason fades."
        closed_block = ("<h2>Already opened</h2>" + _cards(closed_rows, today, base)) if closed_rows else ""
    status_text, status_class = _status(status, kind)
    add_block = ""
    if not share and not due_only:
        default_day = (today + timedelta(days=14)).isoformat()
        add_block = (
            '<form class="paper" id="add" method="post" action="' + html.escape(base) + '/decisions">'
            "<h2>Write one</h2>"
            '<label class="field"><span>The question</span>'
            '<input id="title" name="title" maxlength="80" required placeholder="Take the offer?" /></label>'
            '<label class="field"><span>You picked</span>'
            '<input name="choice" maxlength="120" required placeholder="Stay" /></label>'
            '<label class="field"><span>You left</span>'
            '<input name="alternative" maxlength="120" placeholder="Leave in June" /></label>'
            '<label class="field"><span>Why, for later you</span>'
            '<textarea name="why" maxlength="600" rows="4" placeholder="The team is good. The money is not the point."></textarea></label>'
            '<label class="field"><span>Open again on</span>'
            f'<input name="revisit_on" type="date" required value="{default_day}" /></label>'
            '<button class="btn primary lg" type="submit">Seal it</button>'
            "</form>"
        )
    share_block = ""
    if not share:
        share_url = f"/s/{docket['id']}/{docket['share_token']}"
        share_block = (
            '<section class="paper">'
            "<h2>Share</h2>"
            '<p class="hint">This link can read. It cannot change the record.</p>'
            f'<p class="key mono" id="share">{html.escape(share_url)}</p>'
            '<button type="button" class="btn sm" id="copy-share">Copy share link</button>'
            "</section>"
        )
    owner_block = ""
    if not share:
        owner_block = (
            '<p class="key mono" id="owner">' + html.escape(f"/d/{docket['id']}/{owner_key}") + "</p>"
            '<button type="button" class="btn sm" id="copy-owner">Copy owner link</button>'
        )
    nav_due = "" if share else f'<a href="{html.escape(base)}/due">Due</a>'
    nav_board = f'<a href="{html.escape(base)}">Board</a>'
    empty = "Nothing to open. The next date is on the board." if due_only else "None waiting."
    open_html = _cards(shown_open, today, base) if shown_open else f'<p class="hint">{empty}</p>'
    return render(
        "board.html",
        TITLE=html.escape(heading),
        NAME=html.escape(docket["name"]),
        MODE=html.escape(mode),
        HEADING=html.escape(heading),
        LEDE=html.escape(lede),
        BASE=html.escape(base),
        OWNER_BLOCK=owner_block,
        OPEN=open_html,
        OPEN_HEAD="Open these" if due_only else "Waiting",
        CLOSED_BLOCK=closed_block,
        ADD=add_block,
        SHARE_BLOCK=share_block,
        NAV_DUE=nav_due,
        NAV_BOARD=nav_board,
        NAV_WRITE='<a href="#add">Write</a>' if add_block else "",
        STATUS=status_text,
        STATUS_CLASS=status_class,
        DUE_COUNT=str(len(due_rows)),
    )


@app.get("/d/{docket_id}/{key}", response_class=HTMLResponse)
def board_page(request: Request, docket_id: str, key: str) -> HTMLResponse:
    try:
        with connect() as conn:
            docket = require_owner(conn, docket_id, key)
            rows = _load_decisions(conn, docket_id)
    except DocketError as exc:
        return HTMLResponse(render("index.html", STATUS=html.escape(str(exc)), STATUS_CLASS=" bad", NAME=""), status_code=404)
    return HTMLResponse(_board_html(docket, rows, today_for(request), owner_key=key))


@app.get("/d/{docket_id}/{key}/due", response_class=HTMLResponse)
def due_page(request: Request, docket_id: str, key: str) -> HTMLResponse:
    try:
        with connect() as conn:
            docket = require_owner(conn, docket_id, key)
            rows = _load_decisions(conn, docket_id)
    except DocketError as exc:
        return HTMLResponse(render("index.html", STATUS=html.escape(str(exc)), STATUS_CLASS=" bad", NAME=""), status_code=404)
    return HTMLResponse(_board_html(docket, rows, today_for(request), owner_key=key, due_only=True))


@app.get("/s/{docket_id}/{token}", response_class=HTMLResponse)
def share_board(request: Request, docket_id: str, token: str) -> HTMLResponse:
    try:
        with connect() as conn:
            docket = require_share(conn, docket_id, token)
            rows = _load_decisions(conn, docket_id)
    except DocketError as exc:
        return HTMLResponse(render("index.html", STATUS=html.escape(str(exc)), STATUS_CLASS=" bad", NAME=""), status_code=404)
    return HTMLResponse(_board_html(docket, rows, today_for(request), share=True))


@app.post("/d/{docket_id}/{key}/decisions")
def add_decision(
    request: Request,
    docket_id: str,
    key: str,
    title: str = Form(""),
    choice: str = Form(""),
    alternative: str = Form(""),
    why: str = Form(""),
    revisit_on: str = Form(""),
) -> Response:
    try:
        name = _clean(title, 80, "The question", required=True)
        picked = _clean(choice, 120, "You picked", required=True)
        left = _clean(alternative, 120, "You left")
        reason = _clean(why, 600, "Why")
        day = parse_day(revisit_on)
        decision_id = secrets.token_urlsafe(6)
        with connect() as conn:
            require_owner(conn, docket_id, key)
            count = conn.execute("SELECT COUNT(*) AS n FROM decisions WHERE docket_id = ?", (docket_id,)).fetchone()["n"]
            if count >= MAX_DECISIONS:
                raise DocketError(f"This docket already has {MAX_DECISIONS} decisions.")
            conn.execute(
                """
                INSERT INTO decisions
                  (id, docket_id, title, choice, alternative, why, revisit_on, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, docket_id, name, picked, left, reason, day, int(time.time())),
            )
    except DocketError as exc:
        try:
            with connect() as conn:
                docket = require_owner(conn, docket_id, key)
                rows = _load_decisions(conn, docket_id)
        except DocketError:
            return HTMLResponse(render("index.html", STATUS=html.escape(str(exc)), STATUS_CLASS=" bad", NAME=""), status_code=404)
        page = _board_html(docket, rows, today_for(request), owner_key=key, status=str(exc), kind="bad")
        return HTMLResponse(page, status_code=400)
    return RedirectResponse(f"/d/{docket_id}/{key}/n/{decision_id}", status_code=303)


def _decision_html(
    docket: sqlite3.Row,
    row: sqlite3.Row,
    today: date,
    *,
    owner_key: str = "",
    share: bool = False,
    status: str = "",
    kind: str = "",
) -> str:
    label, stamp = due_label(row["revisit_on"], today, row["outcome"])
    if share:
        base = f"/s/{docket['id']}/{docket['share_token']}"
    else:
        base = f"/d/{docket['id']}/{owner_key}"
    left = row["alternative"] or "You did not write one."
    why = row["why"] or "You did not write why."
    outcome_block = ""
    if row["outcome"]:
        when = ""
        if row["closed_at"]:
            when = datetime.fromtimestamp(int(row["closed_at"]), timezone.utc).strftime("%b %-d, %Y")
        note = row["outcome_note"] or "No note."
        outcome_block = (
            '<section class="paper outcome">'
            f'<p class="stamp closed">{html.escape(OUTCOMES.get(row["outcome"], "Closed"))}</p>'
            f"<h2>What happened</h2>"
            f"<p>{html.escape(note)}</p>"
            f'<p class="hint">{html.escape(when)}</p>'
            "</section>"
        )
    elif not share:
        outcome_block = (
            f'<form class="paper" method="post" action="{html.escape(base)}/n/{html.escape(row["id"])}/outcome">'
            "<h2>What happened</h2>"
            '<label class="field"><span>A note for the record</span>'
            '<textarea name="note" maxlength="400" rows="3" placeholder="Still glad. Or not."></textarea></label>'
            '<div class="outcomes">'
            '<button class="btn primary" name="outcome" value="kept" type="submit">Kept it</button>'
            '<button class="btn" name="outcome" value="changed" type="submit">Changed it</button>'
            '<button class="btn" name="outcome" value="unsure" type="submit">Still unsure</button>'
            "</div>"
            '<p class="hint">You can write this before the date. The board only nags once the date arrives.</p>'
            "</form>"
        )
    elif not row["outcome"]:
        outcome_block = '<p class="hint">Not opened yet. This link cannot write the outcome.</p>'
    status_text, status_class = _status(status, kind)
    return render(
        "decision.html",
        TITLE=html.escape(row["title"]),
        NAME=html.escape(docket["name"]),
        BASE=html.escape(base),
        STAMP=html.escape(label),
        STAMP_CLASS=stamp,
        QUESTION=html.escape(row["title"]),
        CHOICE=html.escape(row["choice"]),
        LEFT=html.escape(left),
        WHY=html.escape(why),
        WHEN=html.escape(_fmt_day(row["revisit_on"])),
        OUTCOME=outcome_block,
        STATUS=status_text,
        STATUS_CLASS=status_class,
    )


def _load_decision(conn: sqlite3.Connection, docket_id: str, decision_id: str) -> sqlite3.Row:
    _check_id(decision_id)
    row = conn.execute(
        "SELECT * FROM decisions WHERE id = ? AND docket_id = ?",
        (decision_id, docket_id),
    ).fetchone()
    if row is None:
        raise DocketError("That decision is not in this docket.")
    return row


@app.get("/d/{docket_id}/{key}/n/{decision_id}", response_class=HTMLResponse)
def decision_page(request: Request, docket_id: str, key: str, decision_id: str) -> HTMLResponse:
    try:
        with connect() as conn:
            docket = require_owner(conn, docket_id, key)
            row = _load_decision(conn, docket_id, decision_id)
    except DocketError as exc:
        return HTMLResponse(render("index.html", STATUS=html.escape(str(exc)), STATUS_CLASS=" bad", NAME=""), status_code=404)
    return HTMLResponse(_decision_html(docket, row, today_for(request), owner_key=key))


@app.get("/s/{docket_id}/{token}/n/{decision_id}", response_class=HTMLResponse)
def share_decision(request: Request, docket_id: str, token: str, decision_id: str) -> HTMLResponse:
    try:
        with connect() as conn:
            docket = require_share(conn, docket_id, token)
            row = _load_decision(conn, docket_id, decision_id)
    except DocketError as exc:
        return HTMLResponse(render("index.html", STATUS=html.escape(str(exc)), STATUS_CLASS=" bad", NAME=""), status_code=404)
    return HTMLResponse(_decision_html(docket, row, today_for(request), share=True))


@app.post("/d/{docket_id}/{key}/n/{decision_id}/outcome")
def close_decision(
    docket_id: str,
    key: str,
    decision_id: str,
    outcome: str = Form(""),
    note: str = Form(""),
) -> Response:
    try:
        if outcome not in OUTCOMES:
            raise DocketError("Pick kept, changed, or still unsure.")
        text = _clean(note, 400, "The note")
        with connect() as conn:
            require_owner(conn, docket_id, key)
            row = _load_decision(conn, docket_id, decision_id)
            if row["outcome"]:
                raise DocketError("This one is already opened. The record stays.")
            conn.execute(
                "UPDATE decisions SET outcome = ?, outcome_note = ?, closed_at = ? WHERE id = ?",
                (outcome, text, int(time.time()), decision_id),
            )
    except DocketError as exc:
        return HTMLResponse(render("index.html", STATUS=html.escape(str(exc)), STATUS_CLASS=" bad", NAME=""), status_code=400)
    return RedirectResponse(f"/d/{docket_id}/{key}/n/{decision_id}", status_code=303)


init_db()
