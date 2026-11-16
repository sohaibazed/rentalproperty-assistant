# Rental Agent

A private web app that helps run a small rental-property LLC:

- **Chat** (home page): ask questions, attach receipts to record expenses, research local
  rental rules, and start jobs. Every change waits for you to click a confirm card.
- **Jobs**: `sync-email` imports rent and expense emails from Gmail (automatically on the
  1st, 11th and 21st), `research-rent` recommends next year's rent from comparable listings,
  `generate-lease` writes the yearly lease extension (.docx + .pdf), and `tax-report` builds the
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
6. [Finish setup](#6-finish-setup)
7. [Where things are stored](#7-where-things-are-stored)
8. [Troubleshooting](#8-troubleshooting)

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
2. Paste the whole contents of [`supabase/migrations/0001_init.sql`](supabase/migrations/0001_init.sql)
   and click **Run**. It's safe to run again; it skips anything that already exists.

This creates the tables, a restricted database role called `rental_agent` (it can read, insert and
update, but never delete), and row-level security so Supabase's public API keys see nothing.

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

### 3.6 Add your property and current lease

The jobs need your property, tenants and current lease. In the Supabase SQL Editor, fill in
your real values and run:

```sql
insert into properties (address, city, county, state, zip, property_type, beds, baths, sqft,
                        landlord_names)
values ('123 Example St', 'Plano', 'Collin', 'TX', '75024', 'townhouse', 3, 2.5, 1950,
        array['Landlord One', 'Landlord Two']);

insert into tenants (full_name) values ('Tenant One'), ('Tenant Two');

-- The lease term in effect now. For a yearly extension, set original_lease_date to the date
-- of the first lease it extends.
insert into leases (property_id, start_date, end_date, monthly_rent, deposit, original_lease_date)
values (1, '2026-01-01', '2026-12-31', 2500, 2500, '2024-11-06');

insert into lease_tenants (lease_id, tenant_id) values (1, 1), (1, 2);
```

Check the result on the **Leases** page in the app.

### 3.7 Load your documents (optional)

The chat can answer questions from your leases, LLC papers and the `.md` notes in `reference/`
once their text is in Supabase. `reference/tenant-screening/` is never included, and SSNs are
redacted.

1. In the SQL Editor, run [`supabase/migrations/0002_reference_documents.sql`](supabase/migrations/0002_reference_documents.sql).
2. On your Mac, run `make reference-sql`. It writes `reference/supabase-documents.sql`.
   For a scanned PDF with no text, put a transcription next to it as a `.md` file with the
   same name; the `.md` is used instead of the PDF.
3. Paste that file into the SQL Editor and click **Run**. Re-run steps 2-3 whenever the
   documents change.

### 3.8 Importing new leases and LLC documents

Run [`supabase/migrations/0003_document_import.sql`](supabase/migrations/0003_document_import.sql)
once in the SQL Editor. It adds an `llcs` table, links each property to its LLC, and lets the
app save imported documents.

After that, the **import-document** skill handles each new lease, lease extension, certificate
of formation, operating agreement or EIN letter, for this property or a new one:

- **In the chat:** attach the PDF and say "import this". Claude reads it and shows a card with
  the LLC, property, tenants and lease term it found. Nothing is saved until you click **Save**.
  Import LLC documents before the leases of the properties the LLC owns.
- **In Claude Code** (in this folder, with the app running): ask it to import the document.
  It previews with `make import-document FILE=... DATA=...` and saves with `APPLY=1` only after
  you confirm.

Re-importing the same document updates the records instead of duplicating them. A new
property gets only what a lease states; add beds, baths and square feet in Supabase so rent
research works. The skill's instructions are in
[`agent-plugin/skills/import-document/SKILL.md`](agent-plugin/skills/import-document/SKILL.md).

### 3.9 Good to know

- **Pausing:** free Supabase projects pause after about a week without activity. The app pings
  the database once a day while it's running, so keep it running (Docker Desktop open), or
  open the Supabase dashboard now and then. A paused project can be restored from the dashboard.
- **Backups:** Supabase backs up paid projects automatically. On the free plan, export now and
  then (**Database → Backups**, or `pg_dump`), especially before tax season.
- **Asking Claude about your data:** the same project works with the Supabase connector in
  Claude chat or Duet, if you want to ask questions there too.

## 4. Anthropic / Claude credentials

The app uses Claude for the chat, reading rent and expense emails, and rent research.
Set **one** of these on the **Settings** page.

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
Opus 5.5 is the default, and Sonnet 5.5 and Haiku 5.5 are faster and cheaper.

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
rent-alert sender and the vendors that email you receipts, then restart with `make up`.
Run **sync-email** from **Run a job** with **Dry run** checked first, and compare the result
with your bank statement.

## 6. Finish setup

The **Dashboard** checklist should show three green ticks: Database URL, Claude credentials
and Gmail connected. Then:

- Open **Memory** and add your standing rules (for example, which vendors are personal).
- Try the chat: attach a receipt and ask it to add the expense.

## 7. Where things are stored

| What | Where |
|---|---|
| Rent, expenses, leases, research, chats, job runs | Supabase |
| `memory/` (your rules) | Folder on your Mac |
| `artifacts/` (reports, leases, tax files, receipts, chat uploads) | Folder on your Mac |
| `reference/` (your documents; read-only to the app) | Folder on your Mac |
| `config/` (`email_sources.toml`) | Folder on your Mac |
| Encrypted secrets, chat session files | Docker volume `rental-data` |
| Encryption key, login email and password | macOS Keychain |

`memory/`, `artifacts/`, `reference/` and `data/` are excluded from git and from the Docker image.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `make up` says a variable isn't set | Run `make init-keys` first. |
| Can't sign in | `make set-login-email EMAIL=...` to fix the email. To reset the password, delete the Keychain item `rental-agent-ui-password` in Keychain Access, run `make init-keys`, then `make up`. |
| "Can't reach the database" | Use the **Session pooler** URL with user `rental_agent.<project-ref>` (section 3.4), URL-encode special characters, and check the project isn't paused. |
| "Tenant or user not found" | The username is missing its project ref: it must be `rental_agent.<project-ref>`, not just `rental_agent`. |
| "RENTAL_DB_URL is not set" | Save the database URL in Settings. |
| Chat says "Claude sign-in failed" | Check the API key or token in Settings (section 4). A Claude Code token can expire; run `claude setup-token` again. |
| Gmail: "redirect_uri_mismatch" | The OAuth client must be a **Web application** with the exact redirect URI from section 5.3. |
| Gmail stops working after a week | The consent screen is in Testing; publish it (section 5.2) or reconnect. |
| Gmail stops working after you change your Google password | Google cancels Gmail access on a password change; click **Reconnect Gmail** in Settings. |
| Gmail: "access blocked" / "not a test user" | Add your Gmail address as a test user (section 5.2). |

---

For development: `make setup` installs dependencies locally, and `make test` runs the tests
(database tests run when `TEST_DATABASE_URL` and `TEST_ADMIN_DATABASE_URL` point at a test
Postgres). Agent prompts are in [`prompts/agent-prompts.md`](prompts/agent-prompts.md).
