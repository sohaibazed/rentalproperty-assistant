# Backlog

Features that are planned but not built. Last checked against `main` at `e2d33bc` on 2026-11-23.
When a feature ships, delete its entry.

## Planned, not started

1. **Prompt tests.** A small saved set of emails and statements with expected results, run by
   `make eval`, to catch a bad edit made on the Prompts page. Today the only check is that each
   prompt loads.
2. **Push alerts.** The Needs attention list shows only inside the app. Send the same items by
   email or webhook. Gmail access is read-only by design, so this needs a separate channel.
3. **Capital improvements register.** An expense is either deducted or not; a new roof or HVAC
   unit cannot be marked as an improvement that is depreciated over years. Add a table of
   improvements (date, cost, description) and a section in the tax report, so the CPA has the
   basis additions and the records exist when the property is sold.
4. **Recurring bill and policy reminders.** Insurance renewal, HOA dues, property tax instalments
   and the LLC's yearly Texas franchise filing have dates; only the tax protest is tracked. Add a
   list of dated obligations that feeds Needs attention.
5. **Mileage log.** Trips to the property are deductible only with a log of date, miles and
   purpose. Add a chat card to record a trip and a mileage line (Auto and travel) in the tax
   report. The IRS rate changes each year, so it must be a setting per tax year.
## Offered, not decided

- **Connector for Claude chat or Duet.** An MCP server that exposes the jobs as tools, so they can
  be started from outside the app. A remote connector must be reachable over HTTPS and needs its
  own sign-in.
- **Lease extension with the LLC as landlord.** Only worth doing once the LLC holds the deed.

## Small fixes

- **Evening-only test failure.** `test_sync_inserts_then_dedupes` in `tests/test_email_sync_db.py`
  compares against the local date, so it fails when the UTC date is a day ahead.
- **Missing Renewal link.** The shortcut row under the chat box in
  `src/rental_agent/web/templates/chat.html` has no Renewal link.

## Decided against

A tenant portal, rent collection and multi-user access: too much for one property.
