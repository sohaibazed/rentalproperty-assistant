"""Web UI: run jobs, browse results, manage secrets. Start with `make up` (Docker).

Security: password login, POSTs must come from this site (Origin check), no JavaScript
(strict CSP), secrets never rendered back, file downloads limited to artifacts/.
Bind to localhost only; docker-compose publishes 127.0.0.1:8080.
"""

import asyncio
import contextlib
import hashlib
import hmac
import html
import os
import secrets as pysecrets
import time
from datetime import date
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
from rental_agent.config import artifacts_dir, env_or_file
from rental_agent.web.jobs import JobManager, next_sync_date, scheduler
from rental_agent.web.secrets import GMAIL_TOKEN, SECRETS, SecretStore

HERE = Path(__file__).parent
PUBLIC_PATHS = {"/login", "/static/style.css"}
OAUTH_TTL_SECONDS = 600


def create_app(store: SecretStore | None = None, manager: JobManager | None = None) -> FastAPI:
    store = store or SecretStore.from_env()
    manager = manager or JobManager(store)
    password = env_or_file("RENTAL_UI_PASSWORD")
    if not password:
        raise RuntimeError("RENTAL_UI_PASSWORD is not set; run `make init-keys`, then `make up`")
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
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self' "
            "https://accounts.google.com; frame-ancestors 'none'; base-uri 'none'")
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
    async def login(request: Request, password_in: str = Form(alias="password")):
        if hmac.compare_digest(password_in.encode(), password.encode()):
            request.session["auth"] = True
            return RedirectResponse("/", status_code=303)
        await asyncio.sleep(1)  # slow down guessing
        flash(request, "Wrong password.", "error")
        return RedirectResponse("/login", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- Dashboard ----------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
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
        totals, runs = None, []
        if conn:
            with conn:
                totals, runs = db.year_totals(conn, year), db.job_runs(conn, limit=8)
        return page(request, "dashboard.html", checklist=checklist, db_error=db_error,
                    totals=totals, runs=runs, year=year,
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

    # --- Results ------------------------------------------------------------------------------

    @app.get("/rent", response_class=HTMLResponse)
    async def rent_page(request: Request, year: int | None = None):
        year = year or date.today().year
        conn, db_error = connect()
        rows, total = [], 0
        if conn:
            with conn:
                rows = db.rent_payments_for_year(conn, year)
            total = sum(r["amount"] for r in rows)
        return page(request, "rent.html", rows=rows, total=total, year=year, db_error=db_error)

    @app.get("/expenses", response_class=HTMLResponse)
    async def expenses_page(request: Request, year: int | None = None):
        year = year or date.today().year
        conn, db_error = connect()
        rows, by_category = [], {}
        if conn:
            with conn:
                rows = db.expenses_list(conn, year)
            for r in rows:
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
        # Escape first: reports quote text from web pages, which must not become HTML.
        body = markdown.markdown(html.escape(file.read_text()), extensions=["tables"])
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
        return FileResponse(_artifact(path), filename=Path(path).name)

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


def _same_origin(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urlparse(source).netloc == request.headers.get("host")


def _artifact(path: str) -> Path:
    base = artifacts_dir().resolve()
    file = (base / path).resolve()
    if not file.is_relative_to(base) or not file.is_file():
        raise HTTPException(404)
    return file
