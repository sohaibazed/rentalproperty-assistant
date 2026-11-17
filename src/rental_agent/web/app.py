"""Web UI: run jobs, browse results, manage secrets. Start with `make up` (Docker).

Security: email + password login, POSTs must come from this site (Origin check), scripts only from
this app's own static files (strict CSP, no inline script), model output rendered as escaped
Markdown with http(s)/mailto links only, secrets never rendered back, file downloads limited
to artifacts/.
Bind to localhost only; docker-compose publishes 127.0.0.1:8080.
"""

import asyncio
import contextlib
import hashlib
import hmac
import html
import os
import re
import secrets as pysecrets
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import markdown
import psycopg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from rental_agent import db, gmail, runner
from rental_agent.config import artifacts_dir, env_or_file, reference_dir
from rental_agent.jobs import rent_research
from rental_agent.web import chat_views, review_views
from rental_agent.web.jobs import JobManager, next_sync_date, scheduler
from rental_agent.web.secrets import GMAIL_TOKEN, SECRETS, SecretStore

HERE = Path(__file__).parent
PUBLIC_PATHS = {"/login", "/static/style.css"}
OAUTH_TTL_SECONDS = 600
INLINE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}


def create_app(store: SecretStore | None = None, manager: JobManager | None = None,
               chats: chat_views.ChatManager | None = None) -> FastAPI:
    store = store or SecretStore.from_env()
    manager = manager or JobManager(store)
    chats = chats or chat_views.ChatManager(store)
    password = env_or_file("RENTAL_UI_PASSWORD")
    username = (env_or_file("RENTAL_UI_USERNAME") or "").strip().lower()
    if not password or not username:
        raise RuntimeError("RENTAL_UI_USERNAME and RENTAL_UI_PASSWORD must be set; "
                           "run `make init-keys`, then `make up`")
    public_url = os.environ.get("RENTAL_PUBLIC_URL", "http://localhost:8080").rstrip("/")
    if urlparse(public_url).hostname in ("localhost", "127.0.0.1"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"   # Google allows http only on loopback

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The DB may not be configured yet; then there's nothing to clean up.
        with contextlib.suppress(Exception), db.connect(manager.settings().db_url) as conn:
            db.fail_stale_job_runs(conn)
        task = asyncio.create_task(scheduler(manager))
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Session cookies are signed with a key derived from the secrets key, so logins survive
    # restarts; a random key is used if none is configured (tests).
    secret_key = env_or_file("RENTAL_SECRET_KEY")
    session_key = hashlib.sha256(b"session:" + secret_key.encode()).hexdigest() if secret_key \
        else pysecrets.token_hex(32)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = lambda v: "-" if v is None else f"${v:,.2f}"
    oauth_flows: dict[str, tuple[float, object]] = {}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method == "POST" and not _same_origin(request):
            return Response("Cross-site request blocked", status_code=403)
        if path not in PUBLIC_PATHS and not request.session.get("auth"):
            return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        # A file shown in the browser's viewer sets its own policy (see download()).
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
            "img-src 'self'; form-action 'self' "
            "https://accounts.google.com; frame-ancestors 'none'; base-uri 'none'"))
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    # Added after `guard`, so it runs first and request.session is ready inside guard.
    app.add_middleware(SessionMiddleware, secret_key=session_key, same_site="strict",
                       max_age=12 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(request, name, {"flash": flash, "busy": manager.busy,
                                                          "nav": name, **ctx})

    def flash(request: Request, message: str, kind: str = "ok") -> None:
        request.session["flash"] = {"message": message, "kind": kind}

    def connect():
        """Open the database, or return (None, reason) so pages can explain what's missing."""
        try:
            return db.connect(manager.settings().db_url), None
        except RuntimeError as exc:
            return None, str(exc)
        except psycopg.Error as exc:
            return None, f"Can't reach the database: {exc.__class__.__name__}"

    # --- Login ------------------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return page(request, "login.html")

    @app.post("/login")
    async def login(request: Request, email: str = Form(""), password_in: str = Form("", alias="password")):
        # Compare both every time and report them together, so the page doesn't reveal
        # whether the email or the password was wrong.
        email_ok = hmac.compare_digest(email.strip().lower().encode(), username.encode())
        password_ok = hmac.compare_digest(password_in.encode(), password.encode())
        if email_ok and password_ok:
            request.session["auth"] = True
            request.session["user"] = username
            return RedirectResponse("/", status_code=303)
        await asyncio.sleep(1)  # slow down guessing
        flash(request, "Wrong email or password.", "error")
        return RedirectResponse("/login", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(psycopg.errors.UndefinedColumn)
    async def schema_out_of_date(request: Request, _exc: Exception):
        flash(request, "The database is missing a newer column. Run supabase/schema.sql again "
                       "in the Supabase SQL Editor (README section 3.2).", "error")
        return RedirectResponse("/settings", status_code=303)

    chat_views.register(app, store=store, jobs=manager, chats=chats, page=page, flash=flash,
                        connect=connect, render_markdown=render_markdown)
    review_views.register(app, page=page, flash=flash, connect=connect)

    # --- Dashboard ----------------------------------------------------------------------------

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        status = store.status()
        checklist = [
            ("Database URL", bool(status["RENTAL_DB_URL"])),
            ("Claude credentials", bool(status["ANTHROPIC_API_KEY"] or
                                        status["CLAUDE_CODE_OAUTH_TOKEN"])),
            ("Gmail connected", bool(status[GMAIL_TOKEN])),
        ]
        year = date.today().year
        conn, db_error = connect()
        totals, runs, claude_cost = None, [], None
        if conn:
            with conn:
                totals, runs = db.year_totals(conn, year), db.job_runs(conn, limit=8)
                claude_cost = db.month_claude_cost(conn, date.today())
        return page(request, "dashboard.html", checklist=checklist, db_error=db_error,
                    totals=totals, runs=runs, year=year, claude_cost=claude_cost,
                    claude_budget=_claude_budget(), today=date.today(),
                    next_sync=next_sync_date(date.today()))

    # --- Jobs and runs --------------------------------------------------------------------------

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request):
        return page(request, "jobs.html", jobs=runner.JOBS)

    @app.post("/jobs/{job}")
    async def start_job(request: Request, job: str):
        if job not in runner.JOBS:
            raise HTTPException(404)
        form = await request.form()
        try:
            params = runner.parse_params(job, dict(form))
            run_id = await manager.start(job, params)
        except (ValueError, RuntimeError) as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/jobs", status_code=303)
        except psycopg.Error as exc:
            flash(request, f"Database error: {exc.__class__.__name__}", "error")
            return RedirectResponse("/jobs", status_code=303)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request, job: str | None = None):
        conn, db_error = connect()
        runs = []
        if conn:
            with conn:
                runs = db.job_runs(conn, limit=200, job=job or None)
        return page(request, "runs.html", runs=runs, db_error=db_error, job=job, jobs=runner.JOBS)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: int):
        conn, db_error = connect()
        if not conn:
            return page(request, "runs.html", runs=[], db_error=db_error, job=None, jobs=runner.JOBS)
        with conn:
            run = db.job_run(conn, run_id)
        if not run:
            raise HTTPException(404)
        return page(request, "run.html", run=run)

    @app.post("/runs/{run_id}/save-research")
    async def save_dry_run_research(request: Request, run_id: int):
        """Remove the dry-run tag from a research-rent run: save its recommendation."""
        back = f"/runs/{run_id}"
        conn, db_error = connect()
        if not conn:
            flash(request, db_error, "error")
            return RedirectResponse(back, status_code=303)
        with conn:
            run = db.job_run(conn, run_id)
            if not run:
                raise HTTPException(404)
            reports = [p for p in run["outputs"] or []
                       if p.endswith(rent_research.DRY_RUN_SUFFIX + ".md")]
            if (run["job"] != "research-rent" or run["status"] != "success"
                    or not run["params"].get("dry_run") or not reports):
                flash(request, "Only a successful dry run of research-rent can be saved.", "error")
                return RedirectResponse(back, status_code=303)
            data_file = artifacts_dir() / Path(reports[0]).with_suffix(".json")
            # A later dry run for the same property and year writes the same files.
            if data_file.is_file() and datetime.fromtimestamp(data_file.stat().st_mtime, UTC) \
                    > run["finished_at"] + timedelta(seconds=5):
                flash(request, "A newer dry run replaced this report. Save from that run instead.",
                      "error")
                return RedirectResponse(back, status_code=303)
            try:
                final = rent_research.save_dry_run(manager.settings(), reports[0])
            except (ValueError, RuntimeError, psycopg.Error) as exc:
                flash(request, f"Not saved: {exc}", "error")
                return RedirectResponse(back, status_code=303)
            db.mark_research_saved(
                conn, run_id, final,
                f"Saved from this dry run on {date.today():%b %-d, %Y}; generate-lease will use "
                "this rent by default.")
            conn.commit()
        flash(request, "Recommendation saved. The report is no longer marked as a dry run.")
        return RedirectResponse(back, status_code=303)

    # --- Results ------------------------------------------------------------------------------

    @app.get("/rent", response_class=HTMLResponse)
    async def rent_page(request: Request, year: int | None = None):
        year = year or date.today().year
        conn, db_error = connect()
        rows = []
        if conn:
            with conn:
                rows = db.rent_payments_for_year(conn, year, include_removed=True)
        counted = [r["amount"] for r in rows if not r["removed_at"]]
        return page(request, "rent.html", rows=rows, total=sum(counted), count=len(counted),
                    year=year, db_error=db_error)

    @app.get("/expenses", response_class=HTMLResponse)
    async def expenses_page(request: Request, year: int | None = None):
        year = year or date.today().year
        conn, db_error = connect()
        rows, by_category = [], {}
        if conn:
            with conn:
                rows = db.expenses_list(conn, year, include_removed=True)
            for r in rows:
                if r["removed_at"]:
                    continue
                by_category[r["category"]] = by_category.get(r["category"], 0) + r["amount"]
        return page(request, "expenses.html", rows=rows, year=year, db_error=db_error,
                    by_category=sorted(by_category.items(), key=lambda kv: -kv[1]),
                    total=sum(by_category.values()))

    @app.get("/research", response_class=HTMLResponse)
    async def research_page(request: Request):
        conn, db_error = connect()
        rows = []
        if conn:
            with conn:
                rows = db.rent_research_list(conn)
        return page(request, "research.html", rows=rows, db_error=db_error)

    @app.get("/report/{path:path}", response_class=HTMLResponse)
    async def report_page(request: Request, path: str):
        file = _artifact(path)
        if file.suffix != ".md":
            raise HTTPException(404)
        body = render_markdown(file.read_text())
        return page(request, "report.html", title=file.stem, body=body, path=path)

    @app.get("/leases", response_class=HTMLResponse)
    async def leases_page(request: Request):
        conn, db_error = connect()
        rows, props = [], []
        if conn:
            with conn:
                rows, props = db.leases_list(conn), db.properties_list(conn)
        return page(request, "leases.html", rows=rows, props=props, db_error=db_error)

    @app.get("/files/{path:path}")
    async def download(path: str):
        file = _artifact(path)
        # PDFs and images open in the browser's viewer; anything else downloads. The pages'
        # default-src 'none' policy would stop Chrome's PDF viewer, so these files only forbid
        # being embedded in other sites.
        if file.suffix.lower() in INLINE_SUFFIXES:
            return FileResponse(file, filename=file.name, content_disposition_type="inline",
                                headers={"Content-Security-Policy": "frame-ancestors 'none'"})
        return FileResponse(file, filename=file.name)

    # --- Settings -----------------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return page(request, "settings.html", specs=SECRETS, status=store.status(),
                    redirect_uri=f"{public_url}/oauth/gmail/callback")

    @app.post("/settings/secret")
    async def save_secret(request: Request, name: str = Form(), value: str = Form()):
        try:
            if not value.strip():
                raise ValueError("Enter a value (use Remove to clear it).")
            store.set(name, value)
            flash(request, f"Saved {name}.")
        except ValueError as exc:
            flash(request, f"Not saved: {exc}", "error")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/secret/delete")
    async def delete_secret(request: Request, name: str = Form()):
        store.delete(name)
        flash(request, f"Removed {name}.")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/test-db")
    async def test_db(request: Request):
        conn, db_error = connect()
        if conn:
            with conn:
                db.ping(conn)
            flash(request, "Database connection works.")
        else:
            flash(request, db_error, "error")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/oauth/gmail/start")
    async def gmail_start(request: Request):
        client = store.load().get("GMAIL_CLIENT_CONFIG")
        if not client:
            flash(request, "Paste the Gmail OAuth client JSON first.", "error")
            return RedirectResponse("/settings", status_code=303)
        url, state, flow = gmail.start_web_flow(client, f"{public_url}/oauth/gmail/callback")
        now = time.time()
        for key in [k for k, (t, _) in oauth_flows.items() if now - t > OAUTH_TTL_SECONDS]:
            oauth_flows.pop(key)
        oauth_flows[state] = (now, flow)
        return RedirectResponse(url, status_code=303)

    @app.get("/oauth/gmail/callback")
    async def gmail_callback(request: Request, state: str = "", error: str | None = None):
        entry = oauth_flows.pop(state, None)
        if error or not entry or time.time() - entry[0] > OAUTH_TTL_SECONDS:
            flash(request, f"Gmail not connected: {error or 'sign-in expired, try again'}", "error")
            return RedirectResponse("/settings", status_code=303)
        try:
            token = gmail.finish_web_flow(entry[1], str(request.url))
            store.set(GMAIL_TOKEN, token)
            flash(request, "Gmail connected (read-only).")
        except Exception as exc:  # noqa: BLE001 — show the reason, keep secrets out of it
            flash(request, f"Gmail not connected: {exc.__class__.__name__}", "error")
        return RedirectResponse("/settings", status_code=303)

    return app


_HREF = re.compile(r'href="([^"]*)"')


def render_markdown(text: str) -> str:
    """Markdown for model-written or web-sourced text. Escapes HTML first so nothing in the
    text becomes markup, then keeps only http(s), mailto and in-app links."""
    out = markdown.markdown(html.escape(text, quote=False),
                            extensions=["tables", "fenced_code", "sane_lists"])

    def link(m: re.Match) -> str:
        url = html.unescape(m.group(1))
        if re.match(r"(https?:|mailto:|/(?!/))", url):
            return f'href="{m.group(1)}" rel="noopener noreferrer" target="_blank"'
        return 'href="#"'

    return _HREF.sub(link, out)


def _claude_budget() -> Decimal | None:
    """Optional monthly Claude budget in USD (RENTAL_CLAUDE_MONTHLY_BUDGET) for the cost meter."""
    raw = os.environ.get("RENTAL_CLAUDE_MONTHLY_BUDGET", "").strip()
    try:
        budget = Decimal(raw) if raw else None
    except InvalidOperation:
        return None
    return budget if budget and budget > 0 else None


def _same_origin(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urlparse(source).netloc == request.headers.get("host")


def _artifact(path: str) -> Path:
    """A file under artifacts/ or, like an original lease's pdf_path, under reference/."""
    for base in (artifacts_dir().resolve(), reference_dir().resolve()):
        file = (base / path).resolve()
        if file.is_relative_to(base) and file.is_file():
            return file
    raise HTTPException(404)
