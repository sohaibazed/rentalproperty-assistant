"""Chat home page: conversations, attachments, live replies, confirm cards, memory and
prompts pages."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from rental_agent import (
    chat,
    corrections,
    db,
    doc_import,
    memory,
    prompts,
    runner,
    statement_import,
)
from rental_agent.config import artifacts_dir, load_settings
from rental_agent.extract import SCHEDULE_E_CATEGORIES
from rental_agent.web.jobs import JobManager
from rental_agent.web.secrets import SecretStore

MAX_FILES = 5
HISTORY_MESSAGES = 20


@dataclass
class LiveTurn:
    text: str = ""
    status: str = "Thinking"
    done: bool = False


class ChatManager:
    """Runs chat turns in the background and keeps their live text for streaming."""

    def __init__(self, store: SecretStore, turn_fn=chat.run_turn):
        self.store = store
        self.turn_fn = turn_fn
        self.live: dict[int, LiveTurn] = {}
        self.tasks: set[asyncio.Task] = set()

    def busy(self, chat_id: int) -> bool:
        return chat_id in self.live

    async def start(self, chat_id: int, *, model: str, text: str, files: list[tuple[str, Path]],
                    session_id: str | None, history: str) -> None:
        if self.busy(chat_id):
            raise RuntimeError("Still answering the last message in this chat.")
        live = LiveTurn()
        self.live[chat_id] = live
        task = asyncio.create_task(self._run(chat_id, live, model, text, files, session_id, history))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, chat_id, live, model, text, files, session_id, history) -> None:
        settings = load_settings(self.store.load())
        try:
            # Own thread + loop, like jobs: tool calls do blocking database work.
            result = await asyncio.to_thread(asyncio.run, self.turn_fn(
                settings, model=model, text=text, files=files, session_id=session_id,
                history=history,
                on_text=lambda t: setattr(live, "text", t),
                on_status=lambda s: setattr(live, "status", s)))
        except Exception as exc:  # noqa: BLE001 — shown in the chat instead of a server error
            result = chat.TurnResult("", [], None, error=f"{type(exc).__name__}: {exc}")
        content = result.text
        if result.error:
            content = (content + "\n\n" if content else "") + f"⚠️ {_friendly(result.error)}"
        try:
            with db.connect(settings.db_url) as conn:
                db.add_chat_message(conn, chat_id, "assistant", content or "(no reply)",
                                    proposals_json=json.dumps(result.proposals), model=model)
                db.update_chat(conn, chat_id, model=model, sdk_session_id=result.session_id)
        finally:
            live.done = True
            self.live.pop(chat_id, None)


def _friendly(error: str) -> str:
    low = error.lower()
    if any(s in low for s in ("authenticat", "api key", "api-key", "api_key", "oauth",
                              "not logged in", "/login")):
        return "Claude sign-in failed. Check the Claude credentials on the Settings page."
    return f"Something went wrong: {error[:300]}"


def _history(messages: list[dict]) -> str:
    lines = []
    for m in messages[-HISTORY_MESSAGES:]:
        who = "Owner" if m["role"] == "user" else "Assistant"
        lines.append(f"{who}: {m['content'][:2000]}")
    return "\n\n".join(lines)


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-.")[:60] or "file"
    return stem + Path(name).suffix.lower()


def register(app: FastAPI, *, store: SecretStore, jobs: JobManager, chats: ChatManager,
             page, flash, connect, render_markdown) -> None:

    def sidebar(conn, search: str | None) -> list[dict]:
        return db.chats_list(conn, search or None) if conn else []

    def chat_page(request: Request, chat_id: int | None, search: str | None) -> HTMLResponse:
        conn, db_error = connect()
        current, messages, chat_list, properties = None, [], [], []
        if conn:
            with conn:
                chat_list = sidebar(conn, search)
                properties = db.properties_list(conn)
                if chat_id is not None:
                    current = db.chat(conn, chat_id)
                    if not current:
                        raise HTTPException(404)
                    messages = db.chat_messages(conn, chat_id)
        for m in messages:
            m["html"] = render_markdown(m["content"]) if m["role"] == "assistant" else None
        memory_rules = sum(1 for line in memory.read().splitlines() if line.lstrip().startswith("- "))
        return page(request, "chat.html", chat=current, messages=messages, chats=chat_list,
                    search=search or "", db_error=db_error, models=chat.MODELS,
                    model=(current or {}).get("model") or chat.DEFAULT_MODEL,
                    live=chats.busy(chat_id) if chat_id else False, properties=properties,
                    categories=SCHEDULE_E_CATEGORIES, memory_rules=memory_rules,
                    accept=",".join(sorted(chat.UPLOAD_TYPES)))

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, q: str | None = None):
        return chat_page(request, None, q)

    @app.get("/c/{chat_id}", response_class=HTMLResponse)
    async def open_chat(request: Request, chat_id: int, q: str | None = None):
        return chat_page(request, chat_id, q)

    @app.post("/chat/send")
    async def send(request: Request):
        form = await request.form()
        text = str(form.get("message") or "").strip()
        model = str(form.get("model") or chat.DEFAULT_MODEL)
        chat_id = int(form["chat_id"]) if form.get("chat_id") else None
        uploads = [f for f in form.getlist("files") if getattr(f, "filename", "")]
        back = f"/c/{chat_id}" if chat_id else "/"
        if not text and not uploads:
            return RedirectResponse(back, status_code=303)
        if model not in chat.MODELS:
            flash(request, "Unknown model.", "error")
            return RedirectResponse(back, status_code=303)
        if len(uploads) > MAX_FILES:
            flash(request, f"Attach at most {MAX_FILES} files.", "error")
            return RedirectResponse(back, status_code=303)
        blobs = []
        try:
            for f in uploads:
                data = await f.read()
                chat.check_upload(f.filename, data)
                blobs.append((f.filename, data))
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(back, status_code=303)
        if chat_id is not None and chats.busy(chat_id):
            flash(request, "Still answering the last message.", "error")
            return RedirectResponse(back, status_code=303)

        conn, db_error = connect()
        if not conn:
            flash(request, db_error, "error")
            return RedirectResponse(back, status_code=303)
        with conn:
            if chat_id is None:
                title = (text or blobs[0][0]).splitlines()[0][:70]
                chat_id = db.create_chat(conn, title, model)
                current = {"sdk_session_id": None}
                earlier: list[dict] = []
            else:
                current = db.chat(conn, chat_id)
                if not current:
                    raise HTTPException(404)
                earlier = db.chat_messages(conn, chat_id)
            folder = artifacts_dir() / "chat-uploads" / str(chat_id)
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            files: list[tuple[str, Path]] = []
            for name, data in blobs:
                path = folder / f"{stamp}-{_safe_name(name)}"
                path.write_bytes(data)
                files.append((str(path.relative_to(artifacts_dir())), path))
            db.add_chat_message(conn, chat_id, "user", text, attachments=[r for r, _ in files])
            conn.commit()
        await chats.start(chat_id, model=model, text=text, files=files,
                          session_id=current.get("sdk_session_id"), history=_history(earlier))
        return RedirectResponse(f"/c/{chat_id}", status_code=303)

    @app.get("/c/{chat_id}/events")
    async def events(chat_id: int):
        async def stream():
            last = None
            for _ in range(2400):                        # give up after ~10 minutes
                live = chats.live.get(chat_id)
                if live is None or live.done:
                    yield "event: done\ndata: {}\n\n"
                    return
                state = (live.text, live.status)
                if state != last:
                    last = state
                    yield f"data: {json.dumps({'text': live.text, 'status': live.status})}\n\n"
                await asyncio.sleep(0.25)
            yield "event: done\ndata: {}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    @app.post("/c/{chat_id}/m/{msg_id}/p/{pid}")
    async def act_on_proposal(request: Request, chat_id: int, msg_id: int, pid: str):
        raw_form = await request.form()
        form = dict(raw_form)
        action = form.get("action")
        back = f"/c/{chat_id}#m{msg_id}"
        conn, db_error = connect()
        if not conn:
            flash(request, db_error, "error")
            return RedirectResponse(back, status_code=303)
        with conn:
            msg = db.chat_message(conn, chat_id, msg_id)
            proposals: list[dict[str, Any]] = (msg or {}).get("proposals") or []
            prop = next((p for p in proposals if p["id"] == pid), None)
            if not prop or prop["status"] != "pending":
                flash(request, "That card was already handled.", "error")
                return RedirectResponse(back, status_code=303)
            try:
                if action == "dismiss":
                    prop["status"], prop["result"] = "dismissed", "Dismissed."
                elif prop["kind"] == "expense":
                    prop["result"] = _save_expense(conn, prop, form)
                    prop["status"] = "done"
                elif prop["kind"] == "memory":
                    rule = " ".join(str(form.get("rule") or prop["data"]["rule"]).split())
                    memory.append_rule(rule)
                    prop["data"]["rule"] = rule
                    prop["status"], prop["result"] = "done", "Added to memory.md."
                elif prop["kind"] == "document":
                    source = (artifacts_dir() / prop["data"]["attachment"]).resolve()
                    uploads = (artifacts_dir() / "chat-uploads").resolve()
                    if not source.is_relative_to(uploads) or not source.is_file():
                        raise ValueError("the attached file is missing")
                    lines = doc_import.apply(conn, prop["data"], source)
                    prop["status"], prop["result"] = "done", "Saved. " + " ".join(lines)
                elif prop["kind"] == "statement":
                    selected = set(raw_form.getlist("row"))
                    if not selected:
                        raise ValueError("tick at least one row, or dismiss the card")
                    counts = statement_import.save(conn, prop["data"]["rows"], selected)
                    for row in prop["data"]["rows"]:
                        row["saved"] = row["id"] in selected
                    prop["status"] = "done"
                    prop["result"] = (f"Saved {counts['rent']} rent payment(s) and "
                                      f"{counts['expense']} expense(s)"
                                      + (f"; {counts['skipped']} were already recorded."
                                         if counts["skipped"] else "."))
                elif prop["kind"] == "correction":
                    prop["result"] = corrections.apply(conn, prop["data"], datetime.now().date())
                    prop["status"] = "done"
                elif prop["kind"] == "job":
                    options = {k: ("on" if v is True else "" if v in (None, False) else str(v))
                               for k, v in prop["data"]["options"].items()}
                    params = runner.parse_params(prop["data"]["job"], options)
                    run_id = await jobs.start(prop["data"]["job"], params)
                    prop["status"], prop["result"] = "done", f"Started run #{run_id}."
                    prop["run_id"] = run_id
                else:
                    raise ValueError("Unknown card")
            except (ValueError, RuntimeError, psycopg.Error) as exc:
                flash(request, f"Not done: {exc}", "error")
                return RedirectResponse(back, status_code=303)
            db.set_message_proposals(conn, msg_id, json.dumps(proposals))
            conn.commit()
        flash(request, prop["result"])
        return RedirectResponse(back, status_code=303)

    def _save_expense(conn, prop: dict[str, Any], form: dict[str, Any]) -> str:
        """The owner may edit the card before saving; re-validate whatever they submit."""
        receipt = prop["data"].get("receipt")
        prop_id = form.get("property_id")
        data = chat.validate_expense(
            {"date": form.get("date"), "amount": form.get("amount"), "vendor": form.get("vendor"),
             "category": form.get("category"), "description": form.get("description"),
             "property_id": int(prop_id) if prop_id else None, "receipt": receipt},
            {p["id"] for p in db.properties_list(conn)}, {receipt} if receipt else set(),
            datetime.now().date())
        db.insert_expense(conn, property_id=data["property_id"], amount=data["amount"],
                          spent_on=data["date"], vendor=data["vendor"],
                          description=data["description"], category=data["category"],
                          receipt_path=receipt, message_id=None, needs_review=False)
        prop["data"] = data
        return f"Saved: ${float(data['amount']):,.2f} {data['category'].replace('_', ' ')}."

    @app.get("/memory", response_class=HTMLResponse)
    async def memory_page(request: Request):
        text = memory.read()
        return page(request, "memory.html", text=text, path=memory.path(),
                    too_long=len(text) > memory.MAX_CHARS, max_chars=memory.MAX_CHARS)

    @app.post("/memory")
    async def save_memory(request: Request, text: str = Form("")):
        memory.write(text)
        flash(request, "Memory saved. New chats and job runs will use it.")
        return RedirectResponse("/memory", status_code=303)

    @app.get("/prompts", response_class=HTMLResponse)
    async def prompts_page(request: Request):
        return page(request, "prompts.html", text=prompts.read(),
                    names=list(prompts.parse(prompts.read())))

    @app.post("/prompts")
    async def save_prompts(request: Request, text: str = Form("")):
        try:
            prompts.write(text)
        except ValueError as e:
            # Keep the owner's edits on screen so nothing typed is lost.
            return page(request, "prompts.html", text=text, error=str(e),
                        names=list(prompts.parse(prompts.read())))
        flash(request, "Prompts saved. New chats and job runs will use them.")
        return RedirectResponse("/prompts", status_code=303)
