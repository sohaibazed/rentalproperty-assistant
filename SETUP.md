# Rental Agent: setup

A private web app that helps run a small rental-property LLC:

- **Chat** (home page): ask questions, attach receipts to record expenses, import bank or card
  statements to record rent and expenses, import new leases and LLC documents, ask about your
  leases or local rental rules, and start jobs. Every change waits for you to click a confirm
  card.
- **Review**: rent payments and expenses the email sync or a statement import wasn't sure
  about. Fix a row, confirm it, or remove it, here or by telling the chat what is wrong.
  Rent and Expenses have an **Edit** link on every row. A removed row is left out of totals
  and reports and can be restored; nothing is deleted.
- **Jobs**: `sync-email` imports rent and expense emails from Gmail (automatically on the
  1st, 11th and 21st), `research-rent` recommends next year's rent from comparable listings,
  `generate-lease` writes the yearly lease extension as one PDF (the filled-in extension form
  followed by the original lease), and `tax-report` builds the
  year-end Schedule E summary.
- **Memory**: `memory/memory.md` holds your standing rules for every chat and job.

It runs in Docker on your Mac at <http://localhost:8080>. Data lives in Supabase (Postgres),
and Claude does the reading, research and extraction.

## Contents

1. [What you need](#1-what-you-need)
2. [Start the app](#2-start-the-app)
3. [Supabase (database)](#3-supabase-database)
4. [Anthropic / Claude credentials](#4-anthropic--claude-credentials)
5. [Gmail](#5-gmail)
6. [Add your LLC, property and leases](#6-add-your-llc-property-and-leases)
7. [Using it for your own business](#7-using-it-for-your-own-business)
8. [Where things are stored](#8-where-things-are-stored)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. What you need

- A Mac with [Docker Desktop](https://www.docker.com/products/docker-desktop/) running.
- [`uv`](https://docs.astral.sh/uv/) (used once by `make init-keys`): `brew install uv`.
- Accounts: [Supabase](https://supabase.com) (free plan is fine),
  [Google Cloud](https://console.cloud.google.com) (free), and either an
  [Anthropic API key](https://console.anthropic.com) or a Claude subscription with
  [Claude Code](https://docs.claude.com/en/docs/claude-code) installed.

## 2. Start the app

From the project folder:

```bash
make init-keys
```

This runs once. It stores three things in your macOS Keychain:

| Keychain item | What it is |
|---|---|
| `rental-agent-secret-key` | Random key that encrypts the secrets you enter in the app. Generated for you. |
| `rental-agent-ui-password` | The password you sign in with. You choose it. |
| `rental-agent-ui-username` | The email you sign in with. You type it. |

Then build and start:

```bash
make up
```

Open <http://localhost:8080> and sign in. The app only listens on your Mac (`127.0.0.1`).
Other commands: `make down` (stop), `make logs` (follow logs),
`make set-login-email EMAIL=you@example.com` (change the sign-in email, then `make up`).
Run `make up` again after pulling new code.

> Don't lose the Keychain item `rental-agent-secret-key`. Without it the app can't read the
> secrets you saved in Settings, and you'd have to enter them again.

The rest of the setup happens on the **Settings** page.

## 3. Supabase (database)

### 3.1 Create the project

1. Sign in at <https://supabase.com/dashboard> and click **New project**.
2. Pick a name, a strong database password (save it in your password manager), and the region
   closest to you (for Texas, a US region).

### 3.2 Create the tables

1. In the project, open **SQL Editor** and click **New query**.
2. Paste the whole contents of [`supabase/schema.sql`](supabase/schema.sql) and click **Run**.

This is the only SQL file you need. It creates every table, a restricted database role called
`rental_agent` (it can read, add and update rows, but never delete), and row-level security so
Supabase's public API keys see nothing. It's safe to run again: it skips anything that already
exists. After pulling a newer version of this repo, run it again.

### 3.3 Give the app its own login

In the SQL Editor, run this with a new, strong password (not your project password):

```sql
alter role rental_agent with login password 'CHOOSE-A-STRONG-PASSWORD';
```

### 3.4 Build the connection URL

1. Click **Connect** at the top of the project page.
2. Under **Session pooler**, copy the connection string. It looks like
   `postgresql://postgres.abcdefghijklmnop:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres`.
3. Change it to log in as the app's role. Replace the user `postgres` with `rental_agent`,
   **keeping the `.project-ref` part**, and put in the password from step 3.3:

   ```text
   postgresql://rental_agent.abcdefghijklmnop:CHOOSE-A-STRONG-PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```

   If the password contains special characters such as `@ : / ? #`, URL-encode them
   (for example `@` → `%40`), or pick a password made of letters and digits only.

Use the **Session pooler**, not the "Direct connection". The direct address only works over
IPv6, which Docker on a Mac usually can't reach.

### 3.5 Save it in the app

**Settings → Supabase database URL**: paste the URL, click **Save**, then **Test connection**.

### 3.6 Good to know

- **Pausing:** free Supabase projects pause after about a week without activity. The app pings
  the database once a day while it's running, so keep it running (Docker Desktop open), or
  open the Supabase dashboard now and then. A paused project can be restored from the dashboard.
- **Backups:** Supabase backs up paid projects automatically. On the free plan, export now and
  then (**Database → Backups**, or `pg_dump`), especially before tax season.
- **Asking Claude about your data:** the same project works with the Supabase connector in
  Claude chat or Duet, if you want to ask questions there too.

## 4. Anthropic / Claude credentials

The app uses Claude for the chat, reading documents, rent and expense emails, and rent
research. Set **one** of these on the **Settings** page.

**Option A: Anthropic API key** (pay as you go)

1. Go to <https://console.anthropic.com> → **Settings → API Keys** → **Create Key**.
2. Add a payment method and, ideally, a monthly spend limit (**Settings → Limits**).
3. **Settings → Anthropic API key**: paste the key (starts with `sk-ant-`) and **Save**.

**Option B: Claude subscription token** (uses your Claude plan)

1. On your Mac, with Claude Code installed, run:

   ```bash
   claude setup-token
   ```

2. Sign in when the browser opens, then copy the token it prints.
3. **Settings → Claude Code OAuth token**: paste it and **Save**.

If both are set, the API key is used. Pick a model per chat in the chat's model menu;
Sonnet 5.5 is the default for new chats; Opus 5.5 is the most capable, and Haiku 5.5 is the
fastest and cheapest. The jobs always use Opus 5.5.

## 5. Gmail

The app reads rent and expense emails with **read-only** access. It can't send, delete
or change mail.

### 5.1 Create a Google Cloud project and turn on the Gmail API

1. Open <https://console.cloud.google.com>, click the project picker → **New project**, and
   name it (for example "Rental Agent").
2. Go to **APIs & Services → Library**, search for **Gmail API**, and click **Enable**.

### 5.2 Set up the consent screen

1. Go to **APIs & Services → OAuth consent screen** (on newer consoles: **Google Auth
   Platform → Branding / Audience / Data access**).
2. User type **External**. Fill in the app name and your email.
3. Under **Data access** (scopes), add `https://www.googleapis.com/auth/gmail.readonly`.
4. Under **Audience**, add the Gmail address the rent and expense emails go to as a
   **test user**.

**Publishing status matters.** While the app is in **Testing**, Google expires the Gmail
sign-in after 7 days, so the automatic sync (every ~10 days) would stop. After it works once,
go to **Audience** and click **Publish app**. You don't need Google's verification for your own
account. When you sign in you'll see a "Google hasn't verified this app" screen; click
**Advanced → Go to Rental Agent (unsafe)** to continue (it's your own app). If you'd rather
stay in Testing, click **Reconnect Gmail** in Settings once a week.

### 5.3 Create the OAuth client

1. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Under **Authorized redirect URIs**, add exactly:

   ```text
   http://localhost:8080/oauth/gmail/callback
   ```

4. Click **Create**, then **Download JSON**.

### 5.4 Connect in the app

1. **Settings → Gmail OAuth client (JSON)**: paste the whole downloaded file and **Save**.
2. In the **Gmail** box, click **Connect Gmail**, sign in with the Gmail account from step 5.2,
   and allow read-only access. You're sent back to Settings with "Gmail connected".

### 5.5 Tell it which emails to read

Edit [`config/email_sources.toml`](config/email_sources.toml) on your Mac with your bank's
rent-alert sender and the vendors that email you receipts. The file is read at the start of
every sync, so no restart is needed. Run **sync-email** from **Run a job** with **Dry run**
checked first, and compare the result with your bank statement.

The **Dashboard** checklist should now show three green ticks: Database URL, Claude
credentials and Gmail connected.

## 6. Add your LLC, property and leases

The jobs need your property, tenants and current lease. You don't type them in: the
**import-document** skill reads your documents and fills the database.

1. Open the chat and attach your LLC's **certificate of formation** with the message
   "import this". Claude shows a card with what it found (name, filing number, members,
   managers, registered agent). Check it and click **Save**. Repeat for the **operating
   agreement** and the **EIN letter**.
2. Attach your current **lease** (and any **extension**) the same way. The card shows the
   property, landlords, tenants, term, rent and deposit. Saving adds the property, links it
   to your LLC, adds the tenants and the lease term, and stores the document's text so the
   chat can answer questions about it later.
3. A lease doesn't state the property's size, which rent research needs. In the Supabase SQL
   Editor, fill it in (find the id on the **Leases** page):

   ```sql
   update properties set property_type = 'townhouse', beds = 3, baths = 2.5, sqft = 1950
   where id = 1;
   ```

4. Open **Memory** and add your standing rules (for example, which vendors are personal).
5. To fill in past rent and expenses, attach a bank, card or Zelle/Venmo statement export
   (CSV, PDF, OFX or QFX) and say "save the rent and expenses". The **import-statement**
   skill lists the rental transactions in a table; untick anything you don't want and click
   **Save ticked rows**. Rows already recorded (imported before, or the same amount recorded
   by the email sync within 3 days) start unticked, and importing an overlapping statement
   never adds a row twice. Mortgage payments are left out: only the interest is deductible,
   so add the year's interest from your lender's Form 1098 as one `mortgage_interest` expense.

Import LLC documents before leases, so each property is linked to its LLC. Re-importing a
document updates its records instead of duplicating them. The same steps work for a second
property or a second LLC.

**From Claude Code:** in this folder, with the app running, ask Claude Code to import a
document. It follows the same skill
([`agent-plugin/skills/import-document/SKILL.md`](agent-plugin/skills/import-document/SKILL.md)),
previews the result with `make import-document FILE=... DATA=...`, and saves only after you
confirm.

**Optional: load a folder of documents at once.** Put your documents in `reference/` (for
example `reference/leases/` and `reference/llc/`), run `make reference-sql`, then paste the
generated `reference/supabase-documents.sql` into the SQL Editor. This stores only the
documents' text for questions; it does not create LLC, property or lease records. For a
scanned PDF with no text, put a transcription next to it as a `.md` file with the same name.
`reference/tenant-screening/` is never included, and SSNs are redacted.

## 7. Using it for your own business

The app works for any small rental LLC, but a few defaults are set for the original owner's
property in Plano, Texas. Check them before relying on the output:

| Default | Where | Change it if |
|---|---|---|
| Renewal notice: 45 days before the lease ends | `NOTICE_DAYS` in [`src/rental_agent/jobs/lease.py`](src/rental_agent/jobs/lease.py) | Your lease's notice clause says otherwise |
| Lease extension form | [`templates/lease_extension_template.pdf`](templates/lease_extension_template.pdf); where each value is printed: `FIELDS` in [`src/rental_agent/jobs/lease.py`](src/rental_agent/jobs/lease.py) | You're outside Texas or want different wording (have a lawyer review it) |
| Time zone `America/Chicago` (scheduler's "today") | `TZ` in [`docker-compose.yml`](docker-compose.yml) | You're in another time zone |
| Taxes on Schedule E (single-member LLC) | `tax-report` job | Your LLC files a partnership return (Form 1065); ask your CPA |
| Senders to read in Gmail | [`config/email_sources.toml`](config/email_sources.toml) | Always: set your bank and vendors |
| Agent instructions | [`prompts/agent-prompts.md`](prompts/agent-prompts.md) | You want the assistant to behave differently |

Restart with `make up` after changing code, templates or prompts.

## 8. Where things are stored

| What | Where |
|---|---|
| LLCs, properties, rent, expenses, leases, research, document text, chats, job runs | Supabase |
| `memory/` (your rules) | Folder on your Mac |
| `artifacts/` (reports, leases, tax files, receipts, chat uploads) | Folder on your Mac |
| `reference/` (your documents; read-only to the app) | Folder on your Mac |
| `config/` (`email_sources.toml`) | Folder on your Mac |
| Encrypted secrets, chat session files | Docker volume `rental-data` |
| Encryption key, login email and password | macOS Keychain |

`memory/`, `artifacts/`, `reference/` and `data/` are excluded from git and from the Docker image.

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `make up` says a variable isn't set | Run `make init-keys` first. |
| Can't sign in | `make set-login-email EMAIL=...` to fix the email. To reset the password, delete the Keychain item `rental-agent-ui-password` in Keychain Access, run `make init-keys`, then `make up`. |
| "Can't reach the database" | Use the **Session pooler** URL with user `rental_agent.<project-ref>` (section 3.4), URL-encode special characters, and check the project isn't paused. |
| "Tenant or user not found" | The username is missing its project ref: it must be `rental_agent.<project-ref>`, not just `rental_agent`. |
| "permission denied", "relation ... does not exist" or "The database is missing a newer column" | Run [`supabase/schema.sql`](supabase/schema.sql) again (section 3.2), for example after pulling a newer version. |
| "RENTAL_DB_URL is not set" | Save the database URL in Settings. |
| Chat says "Claude sign-in failed" | Check the API key or token in Settings (section 4). A Claude Code token can expire; run `claude setup-token` again. |
| An import card says "isn't in the database yet" | The lease names an LLC that hasn't been imported. Import the LLC's documents first, or ask the chat to leave the owner blank. |
| Gmail: "redirect_uri_mismatch" | The OAuth client must be a **Web application** with the exact redirect URI from section 5.3. |
| Gmail stops working after a week | The consent screen is in Testing; publish it (section 5.2) or reconnect. |
| Gmail stops working after you change your Google password | Google cancels Gmail access on a password change; click **Reconnect Gmail** in Settings. |
| Gmail: "access blocked" / "not a test user" | Add your Gmail address as a test user (section 5.2). |

---

For development: `make setup` installs dependencies locally, and `make test` runs the tests.
Database tests run when `TEST_DATABASE_URL` (a `rental_agent` login) and
`TEST_ADMIN_DATABASE_URL` (an admin login) point at a test Postgres with
[`supabase/schema.sql`](supabase/schema.sql) applied. Agent prompts are in
[`prompts/agent-prompts.md`](prompts/agent-prompts.md).
