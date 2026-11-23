"""The Leases page's move-out forms: record a move-out, list the deposit deductions, write the
itemized letter and record the day it was sent. Nothing here sends anything."""

from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from rental_agent import db, move_out
from rental_agent.config import artifacts_dir

BACK = "/leases#move-out"


def register(app: FastAPI, *, flash, connect) -> None:

    async def act(request: Request, action) -> RedirectResponse:
        """Run one form's `action(conn, form, today)` and go back to the Leases page with its
        message. A ValueError is shown to the owner and nothing is saved."""
        back = RedirectResponse(BACK, status_code=303)
        form = dict(await request.form())
        conn, db_error = connect()
        if not conn:
            flash(request, db_error, "error")
            return back
        with conn:
            try:
                message = action(conn, form, date.today())
            except ValueError as exc:
                conn.rollback()
                flash(request, f"Not saved: {exc}.", "error")
                return back
            conn.commit()
        flash(request, message)
        return back

    def recorded(conn, lease_id: int, today: date, *, open_only: bool = True) -> move_out.MoveOut:
        found = move_out.get(conn, lease_id, today)
        if found is None:
            raise HTTPException(404)
        if open_only and found.sent_on:
            raise ValueError("the letter is recorded as sent; clear the sent date first")
        return found

    @app.post("/leases/{lease_id}/move-out")
    async def save_move_out(request: Request, lease_id: int):
        """Record a move-out for a property's latest signed term, or change its details."""
        def action(conn, form, today):
            existing = move_out.get(conn, lease_id, today)
            if existing is None:
                if lease_id not in {c.lease_id for c in move_out.candidates(conn)}:
                    raise HTTPException(404)
            elif existing.sent_on:
                raise ValueError("the letter is recorded as sent; clear the sent date first")
            start = next(row["start_date"] for row in db.leases_list(conn)
                         if row["id"] == lease_id)
            db.save_move_out(conn, lease_id, **move_out.validate_details(form, start, today))
            return "Move-out saved." + (" Write the letter again." if existing
                                        and existing.letter_path else "")
        return await act(request, action)

    @app.post("/leases/{lease_id}/move-out/cancel")
    async def cancel_move_out(request: Request, lease_id: int):
        def action(conn, form, today):
            recorded(conn, lease_id, today)
            db.cancel_move_out(conn, lease_id)
            return "Move-out cancelled. The lease's renewal and rent reminders are back on."
        return await act(request, action)

    @app.post("/leases/{lease_id}/move-out/deductions")
    async def add_deduction(request: Request, lease_id: int):
        def action(conn, form, today):
            recorded(conn, lease_id, today)
            db.add_deposit_deduction(conn, lease_id, *move_out.validate_line(
                form.get("description"), form.get("amount")))
            return "Deduction added."
        return await act(request, action)

    @app.post("/leases/{lease_id}/move-out/deductions/{deduction_id}/remove")
    async def remove_deduction(request: Request, lease_id: int, deduction_id: int):
        def action(conn, form, today):
            recorded(conn, lease_id, today)
            if not db.remove_deposit_deduction(conn, lease_id, deduction_id):
                raise HTTPException(404)
            return "Deduction removed."
        return await act(request, action)

    @app.post("/leases/{lease_id}/move-out/letter")
    async def write_letter(request: Request, lease_id: int):
        def action(conn, form, today):
            found = recorded(conn, lease_id, today)
            move_out.write_letter(conn, found, artifacts_dir(), today)
            return "Letter written. Check it, sign it, and send it with the refund."
        return await act(request, action)

    @app.post("/leases/{lease_id}/move-out/sent")
    async def record_sent(request: Request, lease_id: int):
        """Save the day the letter and the refund were sent; an empty date clears it."""
        def action(conn, form, today):
            found = recorded(conn, lease_id, today, open_only=False)
            text = str(form.get("sent_on") or "").strip()
            try:
                sent_on = date.fromisoformat(text) if text else None
            except ValueError:
                raise ValueError("that is not a date") from None
            if sent_on and not found.letter_path:
                raise ValueError("write the letter first")
            if sent_on and not found.move_out_date <= sent_on <= today:
                raise ValueError("the sent date must be between the move-out date and today")
            db.set_move_out_sent(conn, lease_id, sent_on)
            return "Saved."
        return await act(request, action)
