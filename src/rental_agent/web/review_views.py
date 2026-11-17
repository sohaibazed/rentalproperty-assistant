"""Review pages: the list of flagged rent payments and expenses, and the page that edits,
confirms, removes or restores one row."""

from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from rental_agent import corrections, db
from rental_agent.extract import SCHEDULE_E_CATEGORIES

SECTIONS = {"rent": "/rent", "expense": "/expenses"}


def register(app: FastAPI, *, page, flash, connect) -> None:

    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request):
        conn, db_error = connect()
        rent, expenses = [], []
        if conn:
            with conn:
                rent, expenses = db.rent_payments_to_review(conn), db.expenses_to_review(conn)
        return page(request, "review.html", rent=rent, expenses=expenses, db_error=db_error)

    def row_routes(kind: str, base: str) -> None:
        def back_url(row: dict, back: str) -> str:
            """Where to go after the row page: the review list, or the row's year."""
            if back == "review":
                return "/review"
            on = row["paid_on"] if kind == "rent" else row["spent_on"]
            return f"{base}?year={on.year}"

        @app.get(base + "/{row_id}", response_class=HTMLResponse)
        async def row_page(request: Request, row_id: int, back: str = ""):
            conn, db_error = connect()
            if not conn:
                flash(request, db_error, "error")
                return RedirectResponse(base, status_code=303)
            with conn:
                row = corrections.load(conn, kind, row_id)
                properties = db.properties_list(conn)
            if not row:
                raise HTTPException(404)
            return page(request, "row_edit.html", kind=kind, row=row,
                        v=corrections.values(kind, row), properties=properties,
                        categories=SCHEDULE_E_CATEGORIES, back=back,
                        back_url=back_url(row, back), today=date.today())

        @app.post(base + "/{row_id}")
        async def act_on_row(request: Request, row_id: int):
            form = dict(await request.form())
            action, back = form.get("action"), str(form.get("back") or "")
            here = f"{base}/{row_id}" + ("?back=review" if back == "review" else "")
            conn, db_error = connect()
            if not conn:
                flash(request, db_error, "error")
                return RedirectResponse(here, status_code=303)
            with conn:
                if not corrections.load(conn, kind, row_id):
                    raise HTTPException(404)
                try:
                    if action == "save":
                        result = corrections.save(conn, kind, row_id, form, date.today())
                    elif action == "checked":
                        result = corrections.mark_checked(conn, kind, row_id, date.today())
                    elif action in ("remove", "restore"):
                        result = corrections.set_removed(conn, kind, row_id, action == "remove")
                    else:
                        raise ValueError("unknown action")
                except ValueError as exc:
                    flash(request, f"Not saved: {exc}", "error")
                    return RedirectResponse(here, status_code=303)
                conn.commit()
                row = corrections.load(conn, kind, row_id)
            flash(request, result)
            return RedirectResponse(back_url(row, back), status_code=303)

    for kind, base in SECTIONS.items():
        row_routes(kind, base)
