# Backlog

Features that are planned but not built. Last checked against `main` at `b841596` on 2026-11-17.
When a feature ships, delete its entry.

## Planned, not started

1. **Prompt tests.** A small saved set of emails and statements with expected results, run by
   `make eval`, to catch a bad edit made on the Prompts page. Today the only check is that each
   prompt loads.
2. **Cost per job run.** The monthly cost card exists, but cost is logged by source (chat, email
   extraction, research) in `claude_usage` and is not tied to a row in `job_runs`. Jobs default to
   Opus 5.5 (`RENTAL_AGENT_MODEL`).
3. **Push alerts.** The Needs attention list shows only inside the app. Send the same items by
   email or webhook. Gmail access is read-only by design, so this needs a separate channel.
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
