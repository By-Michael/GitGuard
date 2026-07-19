# GitGuard — AI-Powered Commit Review Bot

**By-[Michael](https://github.com/By-Michael)**

GitGuard is a self-hosted GitHub commit guardian that sends every push on your repository straight to your Telegram, lets an AI analyze the risk, and gives you a one-tap Accept or Rollback before the code ever becomes a problem.

No dashboards to open. No emails to ignore. Just a message in your pocket while you're having coffee.

---

## What it actually does

Every time someone pushes a commit to your watched branch, GitGuard:

1. Catches the GitHub webhook, verifies it's legitimate, and deduplicates it
2. Queues the commit as a durable job (so a redeploy or crash mid-analysis doesn't lose it) instead of a fire-and-forget task
3. Pulls the full diff and commit metadata from the GitHub API, plus repo file tree and key files for context
4. Sends everything to **Llama 3.3 70B via Groq** for a risk assessment, backed by deterministic guardrail checks on the raw diff
5. Sends you a Telegram message with the verdict — risk level, plain-English summary, concerns, positives, and a recommended action
6. Waits for you to tap **✅ Accept**, **❌ Decline** (auto-rollback), or **📊 Report** for a full `.docx` analysis

If you don't respond within your timeout, it auto-accepts or auto-rolls-back — your choice. GitGuard also recognizes its own revert commits and skips reviewing them.

It supports **multiple users** with completely isolated setups — own webhook URL, own GitHub token, own repo per person. The server only needs a small set of shared secrets.

---

## Features

- **AI risk scoring** — safety score (0-100) locked to a risk level (low/medium/high/critical), plus concerns, positives, and recommendations. Pattern-based guardrails on the raw diff can escalate risk (never soften it) — a `critical` finding forces `decline` outright, regardless of what the model said.
- **One-tap rollback** — `revert` (safe, auditable) or `force_push` (rewrites history), your choice
- **Full Code Analysis** — on-demand, multi-file AI sweep for architectural flaws, security issues, and code debt, with the same secret-detection guardrails applied to whole files
- **Author Performance** — per-developer commit quality and approval-rate reports; numeric stats are computed from the database, not trusted from the model's own math
- **Active Reviews Dashboard**, **Commit History Log**, **configurable auto-timeout**
- **Token reconnect flow** — `/reconnect` swaps an expired GitHub token without wiping your repo/branch/timeout settings
- **Downloadable `.docx` reports** for any review
- **Crash-safe job queue** — commit analysis runs off a durable `jobs` table instead of a bare `asyncio.create_task`
- **Groq API key fallback** — configure multiple keys; GitGuard tries each in order before surfacing an error
- **Resilient outbound HTTP** — shared retry/backoff layer (with `Retry-After` support) for GitHub, Groq, and Telegram calls
- **Per-push and per-user caps** — big pushes cap to the head commit; each user caps at 5 pending reviews
- **Race-condition safe** — atomic dedup + per-commit locks prevent double reviews/rollbacks
- **Auto-registers its own webhook** on every startup; **graceful shutdown** drains in-flight jobs
- **Admin alerting** on repeated failures, with optional structured JSON logging

---

## Tech stack

| Layer | What's used |
|---|---|
| Web server | FastAPI + Uvicorn |
| AI analysis | Groq API — Llama 3.3 70B Versatile, with multi-key fallback |
| Database | Postgres (via `asyncpg`, connection-pooled) |
| HTTP client | httpx (async), shared retry/backoff layer |
| Report generation | python-docx |
| Notifications | Telegram Bot API |
| Config | python-dotenv |

---

## Getting started

### Prerequisites

- Python 3.10+
- A Postgres database (Docker for dev, any managed provider for prod)
- A publicly reachable server URL (Render, Railway, a VPS, etc.)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A [Groq API key](https://console.groq.com)

### 1. Clone and install

```bash
git clone https://github.com/By-Michael/GitGuard.git
cd gitguard
pip install -r requirements.txt
```

### 2. Start a local database

```bash
docker compose up -d
```

Spins up a throwaway Postgres container (`postgresql://gitguard:gitguard@localhost:5432/gitguard`). Don't use this in production — see [Deploying to Render](#deploying-to-render).

### 3. Configure environment

```bash
cp .env.example .env
```

Set at minimum:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
GROQ_API_KEY=your_groq_api_key_here
PUBLIC_URL=https://your-server-url.com
DATABASE_URL=postgresql://gitguard:gitguard@localhost:5432/gitguard
```

Everything else has a sensible default — see [Configuration reference](#configuration-reference).

### 4. Run it

```bash
uvicorn app.webhook_server:app --host 0.0.0.0 --port 8000
```

GitGuard initializes the schema, registers itself with Telegram, and starts the job workers. Check `/health` — it round-trips a real query against the database, not just "did the process start."

### 5. Onboard via Telegram

Send `/start` to your bot and follow the 5-step wizard: repo (`owner/repo`), branch, a GitHub PAT with `repo` scope, timeout hours, and timeout action. You'll get your personal webhook URL at the end. (If you have pending reviews and re-run `/start`, GitGuard warns you first — it'll clear your token.)

### 6. Add the webhook to GitHub

**Settings → Webhooks → Add webhook**:

- **Payload URL**: the URL the bot gave you (`https://your-server.com/webhook/github/YOUR_CHAT_ID`)
- **Content type**: `application/json`
- **Secret**: from `/settings`
- **Events**: just push events

Push a commit and watch your Telegram.

---

## Configuration reference

Only the top four are required — everything else has a default.

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from @BotFather |
| `GROQ_API_KEY` | Your primary Groq API key |
| `PUBLIC_URL` | Public server URL used to build webhook links (bare hostname accepted, `https://` assumed) |
| `DATABASE_URL` | Postgres connection string, passed to `asyncpg` as-is |

### Groq / AI

| Variable | Default | Description |
|---|---|---|
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Model used for analysis |
| `GROQ_MAX_TOKENS` | `4096` | Max tokens in the AI response |
| `GROQ_TEMPERATURE` | `0.3` | Lower = more consistent |
| `GROQ_TPM_LIMIT` | `12000` | Your account's tokens-per-minute cap, used to size prompts and avoid HTTP 413 |
| `GROQ_API_KEYS` / `GROQ_API_KEY_2`, `_3`... | — | Extra keys to fall back to on rate limit/failure |

### Server & analysis

| Variable | Default | Description |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `8000` | Bind address |
| `ROLLBACK_STRATEGY` | `revert` | `revert` (safe) or `force_push` (destructive) |
| `MAX_CONTEXT_FILES` | `50` | Max repo files sent to the AI |
| `MAX_FILE_SIZE_BYTES` | `50000` | Max size per file sent to the AI |
| `EXCLUDED_PATTERNS` | *(see `.env.example`)* | Paths/patterns excluded from AI context |
| `TELEGRAM_WEBHOOK_SECRET` | — | Verifies updates actually came from Telegram |

### Database & jobs

| Variable | Default | Description |
|---|---|---|
| `DB_POOL_MIN` / `DB_POOL_MAX` | `2` / `10` | Pooled connection range |
| `DB_COMMAND_TIMEOUT_SECONDS` / `DB_CONNECT_TIMEOUT_SECONDS` | `10` / `10` | Query / connect timeouts |
| `JOB_POLL_INTERVAL_SECONDS` | `3` | How often idle workers poll for jobs |
| `JOB_WORKER_COUNT` | `3` | Concurrent in-process job workers |
| `JOB_MAX_ATTEMPTS` | `3` | Retries before a job is marked failed |

### Logging & admin alerting

| Variable | Default | Description |
|---|---|---|
| `LOG_FORMAT` | `text` | `json` for structured logs (recommended in prod) |
| `ADMIN_CHAT_ID` | — | Chat that gets alerted on repeated failures (unset = just logs a warning) |
| `ALERT_FAILURE_THRESHOLD` / `ALERT_WINDOW_SECONDS` / `ALERT_COOLDOWN_SECONDS` | `5` / `600` / `1800` | When an alert fires and how often |

---

## Telegram bot commands

| Command | What it does |
|---|---|
| `/start` | Onboarding wizard, or reconfigure (warns if you have pending reviews) |
| `/reconnect` | Refresh an expired GitHub token, keeping repo/branch/timeout settings |
| `/status` | Repo, branch, timeout, and pending reviews |
| `/settings` | Your webhook URL and secret |
| `/menu` / `/hidemenu` | Show or hide the reply-keyboard menu |
| `/cancel` / `/stop` | Escape hatch during onboarding |
| `/help` | Full command and menu reference |

**Menu buttons:** 👤 My Profile · 📜 Commit History · 📊 Active Reviews · ⚙️ Settings · 🔍 Full Code Analysis · 👥 Author Performance · 📞 Contact Support · 🙈 Hide Menu

---

## How the review flow works

```
GitHub push → verify signature → filter to watched branch → dedupe → cap checks
       │
       ▼
Row inserted into `jobs` table (survives a crash/redeploy)
       │
       ▼
Job worker claims it (FOR UPDATE SKIP LOCKED) and runs the pipeline:
  fetch commit + diff → skip if it's GitGuard's own revert → fetch repo context
  → Groq risk analysis (with key fallback) → guardrail checks escalate/force-decline
  → save review to Postgres
       │
       ▼
Telegram review card sent: risk level, summary, concerns, ✅ Accept | ❌ Decline | 📊 Report
       │
       ▼
User taps a button, or the timeout worker auto-acts if they don't
```

Job failures retry up to `JOB_MAX_ATTEMPTS` times, then notify the user and count toward admin alerting.

---

## How scoring works

**AI Confidence** and **Safety Score** are different numbers. Confidence is how sure the model is in its own verdict — showing that alone used to produce misleading reads (a tiny diff that corrupts a `<?php` tag could score ~80/100 because the model was "confident" it was harmless).

**Safety Score (0-100)** is the one to trust: its band is locked to the risk level (`critical` 0-24, `high` 25-54, `medium` 55-79, `low` 80-100), so it can never contradict the label next to it — confidence only decides where in that band it lands.

Before the score is computed, `app/risk_guardrails.py` scans the **raw diff** for security-critical paths, hardcoded credentials, disabled security controls, and structural corruption (unbalanced tags/brackets). These checks only push risk **up**, never down, and a `critical` finding forces the decision to `decline` regardless of the model's call. The same guardrails also scan whole files during Full Code Analysis, and report stats (commit counts, decline rates) are computed locally rather than trusted from the model.

---

## Reliability

- **Resilient HTTP** — GitHub/Groq/Telegram calls share one retry layer: 429/5xx/connection errors get exponential backoff with jitter, `Retry-After` is honored, permanent errors (404/401) are never retried.
- **Groq key fallback** — a rate-limited key is skipped for the next one; the user only sees an error once every key fails.
- **Crash-safe jobs** — analysis runs off a `jobs` table claimed atomically, so a crash mid-analysis resumes from the top on next startup instead of vanishing.
- **Shared exception hierarchy** (`app/exceptions.py`) — every intentional error carries a `category` and `retryable` flag for consistent logging/retry decisions.
- **Structured logging** — `LOG_FORMAT=json` for one JSON object per log line, machine-parseable.
- **Admin alerting** (`app/alerting.py`) — pings `ADMIN_CHAT_ID` when a failure category repeats past threshold, with a cooldown so an outage doesn't spam the chat.

---

## Database

Postgres via `asyncpg`, connection-pooled. Four tables: `users` (credentials, repo, timeout prefs), `active_reviews` (commit + AI decision JSON), `queued_shas` (atomic dedup gate), `jobs` (crash-safe queue). `docker-compose up -d` gives you a throwaway local instance.

---

## Deploying to Render

`render.yaml` targets Render's **free** web-service tier. Push your repo, then **New +** → **Blueprint** in the Render dashboard.

Render no longer has a free managed-Postgres tier, so this blueprint doesn't provision a database — you'll need to:

1. Provision Postgres on a free external provider (Neon, Supabase, Aiven, etc.)
2. Set `DATABASE_URL` to that connection string (usually needs `?sslmode=require`)
3. Set `TELEGRAM_BOT_TOKEN` and `GROQ_API_KEY`
4. Optionally set `ADMIN_CHAT_ID` for admin alerts

`PUBLIC_URL` is wired automatically from Render's hostname.

**Cost note:** the free plan spins down after ~15 min idle, which pauses the timeout/job workers until the next request wakes it up. For on-time timeouts, ping `/health` periodically.

---

## A note on rollback strategies

**`revert` (default):** creates an undo commit — safe, auditable, preserves history. **`force_push`:** removes the commit entirely by rewriting history — cleaner-looking but can break anyone who already pulled the branch. Only use this if your team knows what's happening.

---

## Author

Built by **[Michael](https://github.com/By-Michael)**. Started as a small script and just kept growing  one feature led to another until it turned into an actual multi-user service with a job queue, retries, and all the reliability stuff that wasn't part of the original plan.

Bug reports, ideas, or "why did you do it this way" questions — open an issue or ping me on GitHub.

---
## License

[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) — Copyright © 2026 Michael Defaru.

Free to use, modify, and share for any **noncommercial** purpose — personal projects, learning, research, portfolio review, nonprofits, education, and the like. Commercial use requires a separate license from the author. See [`LICENSE`](./LICENSE) for the full terms.

