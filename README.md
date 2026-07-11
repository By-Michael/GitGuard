# GitGuard — AI-Powered Commit Review Bot

GitGuard is a self-hosted GitHub commit guardian that sends every push on your repository straight to your Telegram, lets an AI analyze the risk, and gives you a one-tap Accept or Rollback before the code ever becomes a problem.

No dashboards to open. No emails to ignore. Just a message in your pocket while you're having coffee.

---

## What it actually does

Every time someone pushes a commit to your watched branch, GitGuard:

1. Catches the GitHub webhook and verifies it's legitimate
2. Pulls the full diff and commit metadata from the GitHub API
3. Grabs your repo's file tree and key files so the AI has real context
4. Sends everything to **Llama 3.3 70B via Groq** for a risk assessment
5. Sends you a Telegram message with the verdict — risk level, a plain-English summary, what looks good, what looks concerning, and a recommended action
6. Waits for you to tap **✅ Accept**, **❌ Decline** (which triggers an automatic rollback), or **📊 Report** to get a full `.docx` analysis report

If you don't respond within your configured timeout, it automatically accepts or rolls back — your choice.

It supports **multiple users** with completely isolated setups. Each person gets their own webhook URL, stores their own GitHub token, and watches their own repo. The server only needs three secrets.

---

## Features

- **AI risk scoring** — every commit gets a deterministic-anchored *safety score*, a risk level (low / medium / high / critical), a list of concerns, positive aspects, and specific recommendations, all generated with full awareness of your codebase. The safety score's band is locked to the risk level (see "How scoring works" below) and can be escalated — never softened — by pattern-based guardrail checks that run independently of the AI on the raw diff.
- **One-tap rollback** — decline a commit and GitGuard immediately creates a safe revert commit on GitHub, or force-pushes the parent SHA if you prefer that approach
-**Full Code Analysis** — Trigger deep, multi-file code sweeps on-demand via the Telegram menu to identify architectural flaws, security vulnerabilities, and code debt outside of standard commit triggers
- **Author Performance** — Track team contribution metrics with AI-driven summaries of commit quality, code stability, and review approval rates per author
- **Active Reviews Dashboard** — View and manage all pending, flagged, or currently in-progress commit reviews directly from an interactive Telegram inline menu
- **Commit History Log** — Access an organized, historical archive of past repo commits
- **Configurable auto-timeout** — set how many hours you want before GitGuard takes action on its own, and whether that action should be accept or rollback
- **Multi-user, zero config sharing** — every user onboards through the Telegram bot and stores their own credentials; the server never shares tokens between accounts
- **Downloadable reports** — tap the Report button to get a formatted `.docx` file with the full AI analysis, commit details, author info, and recommendations
- **Race condition safe** — per-commit asyncio locks make sure a double-tap never triggers two rollbacks
- **Auto-registers its own webhook** — on every startup, GitGuard re-registers itself with Telegram so it survives redeployments without manual intervention
- **Graceful shutdown** — in-flight commit tasks are drained before exit so no commit is left hanging

---

## Tech stack

| Layer | What's used |
|---|---|
| Web server | FastAPI + Uvicorn |
| AI analysis | Groq API — Llama 3.3 70B Versatile |
| Database | SQLite (WAL mode, thread-local connections) |
| HTTP client | httpx (async) |
| Report generation | python-docx |
| Notifications | Telegram Bot API |
| Config | python-dotenv |

---

## Getting started

### Prerequisites

- Python 3.10+
- A publicly reachable server URL (Render, Railway, a VPS, etc.)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A [Groq API key](https://console.groq.com)

### 1. Clone and install

```bash
git clone https://github.com/By-Michael/GitGuard.git
cd gitguard
pip install -r requirements.txt
```

### 2. Configure environment

Copy the example file and fill in your three required secrets:

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
GROQ_API_KEY=your_groq_api_key_here
PUBLIC_URL=https://your-server-url.com
```

Everything else has a sensible default. See the [Configuration reference](#configuration-reference) below for the full list.

### 3. Run it

```bash
uvicorn webhook_server:app --host 0.0.0.0 --port 8000
```

On startup, GitGuard initialises the SQLite database, registers itself with Telegram, and starts the background timeout worker. You'll see a log line confirming everything is live.

### 4. Onboard via Telegram

Open Telegram, find your bot, and send `/start`. The bot walks you through five steps:

1. Your repository in `owner/repo` format
2. The branch to watch (e.g. `main`)
3. A GitHub personal access token with `repo` scope
4. How many hours before a review times out (0 to disable)
5. What to do on timeout — auto-accept or auto-rollback

Once onboarding is complete, the bot shows you your personal webhook URL.

### 5. Add the webhook to GitHub

Go to your repository → **Settings → Webhooks → Add webhook** and fill in:

- **Payload URL**: the URL the bot just gave you (looks like `https://your-server.com/webhook/github/YOUR_CHAT_ID`)
- **Content type**: `application/json`
- **Secret**: the webhook secret the bot generated for you (you can retrieve it anytime with `/settings`)
- **Events**: just push events is enough

That's it. Push a commit and watch your Telegram.

---

## Configuration reference

All of these can be set in your `.env` file. Only the top three are required — the rest have defaults.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Your Telegram bot token from @BotFather |
| `GROQ_API_KEY` | ✅ | — | Your Groq API key for Llama access |
| `PUBLIC_URL` | ✅ | — | The public URL of your server, used to build webhook URLs shown to users during onboarding |
| `GROQ_MODEL` | | `llama-3.3-70b-versatile` | The Groq model to use for analysis |
| `GROQ_MAX_TOKENS` | | `4096` | Max tokens in the AI response |
| `GROQ_TEMPERATURE` | | `0.3` | Sampling temperature — lower means more consistent decisions |
| `SERVER_HOST` | | `0.0.0.0` | Host to bind the FastAPI server to |
| `SERVER_PORT` | | `8000` | Port to listen on |
| `ROLLBACK_STRATEGY` | | `revert` | How to undo a commit — `revert` creates a safe revert commit, `force_push` rewrites history (destructive) |
| `MAX_CONTEXT_FILES` | | `50` | Max number of repo files sent to the AI for context |
| `MAX_FILE_SIZE_BYTES` | | `50000` | Max size of any individual file sent to the AI |
| `EXCLUDED_PATTERNS` | | *(see below)* | Comma-separated list of paths and patterns to exclude from AI context |

**Default excluded patterns:** `.git`, `node_modules`, `__pycache__`, `.venv`, `*.lock`, `*.min.js`, `*.min.css`, `build`, `dist`, and common binary/media extensions like `.png`, `.jpg`, `.mp4`, `.pdf`, `.zip`.

---

## Telegram bot commands

| Command | What it does |
|---|---|
| `/start` | Starts the onboarding wizard or reconnects an existing account |
| `/status` | Shows your current setup — repo, branch, active reviews |
| `/settings` | Displays your webhook URL and webhook secret |
| `/help` | Lists all available commands with brief descriptions |

---

## How the review flow works

```
GitHub push event
       │
       ▼
POST /webhook/github/{chat_id}
  └─ Verify HMAC signature
  └─ Deduplicate (skip if SHA already in DB)
       │
       ▼
Background task spawned
  └─ Fetch commit metadata + diff (GitHub API)
  └─ Fetch repo context (file tree + key files)
  └─ Send to Groq Llama for risk analysis
  └─ Save review to SQLite
       │
       ▼
Telegram message sent to user
  ├─ Risk level + confidence score
  ├─ Summary and reasoning
  ├─ Concerns and positive aspects
  └─ Inline buttons: ✅ Accept | ❌ Decline | 📊 Report

User taps a button
  ├─ Accept → resolves review, keeps commit
  ├─ Decline → rollback commit on GitHub → notify result
  └─ Report → generate + send .docx analysis file

No response within timeout_hours
  └─ Timeout worker auto-accepts or auto-declines
```

---

## How scoring works

Two numbers appear on every review card, and they mean different things:

- **AI Confidence** — how confident the model is in *its own verdict*. This alone used to be shown as if it were a safety score, which is what caused misleading reads (e.g. a one-character change that corrupts a `<?php` tag being scored ~80/100 because the model was confident the tiny diff was harmless).
- **Safety Score (0-100)** — the number to actually trust. Its band is locked to the risk level (`critical` → 0-24, `high` → 25-54, `medium` → 55-79, `low` → 80-100), so it can never contradict the risk label next to it. Confidence only decides where in that band it lands.

Before the AI-produced risk level is used, `risk_guardrails.py` runs deterministic pattern checks against the **raw diff** (not a summary of it): security-critical paths (`.github/workflows/`, auth, secrets, migrations, `Dockerfile`, `.env`), hardcoded-credential patterns, security controls being disabled (`verify=False`, `DEBUG=True`, etc.), and structural corruption signals (e.g. a removed `<?php`/`?>` tag, or an unbalanced bracket delta within the visible diff). These checks can only push risk **up** and force a `decline`, never soften what the model said — a false positive here just adds an extra concern for you to read; a false negative from the model gets caught.

The same guardrail module also runs against whole-file contents during **Full Code Analysis** (flagging any committed secret regardless of what the model notices in a truncated preview) and the numeric fields in both **Full Code Analysis** and **Author Analysis** reports (commit counts, decline rates, etc.) are computed locally from the database and overwritten onto the AI's response rather than trusted from the model's own restatement of the same numbers — this avoids miscounts once there's more than a handful of commits/authors in the prompt.

---

## Reliability (Phase 4)

- **Shared exception hierarchy** (`exceptions.py`) — every exception the app raises on purpose (GitHub/AI/Telegram/database/job failures) inherits from `GitGuardError`, carrying a `category` (github/ai/telegram/database/jobs) and `retryable` flag, instead of being ad-hoc per module.
- **Structured logging** — set `LOG_FORMAT=json` (the Render blueprint below does this) to emit one JSON object per log line, including the failure `category` when the active exception is a `GitGuardError`. Leave unset for the original human-readable format during local dev.
- **Admin alerting on repeated failures** (`alerting.py`) — separate from the existing per-user "your commit failed" notice, this watches for a *pattern* (e.g. Groq or GitHub being down for everyone) and pings `ADMIN_CHAT_ID` once a failure category crosses `ALERT_FAILURE_THRESHOLD` within `ALERT_WINDOW_SECONDS`, then goes quiet for `ALERT_COOLDOWN_SECONDS` so a sustained outage doesn't spam the chat. Unset `ADMIN_CHAT_ID` disables sending (a warning is logged instead).

---

## Database

GitGuard uses Postgres (via `asyncpg`, connection-pooled) — a `DATABASE_URL` is required. Two main tables:

- **`users`** — one row per Telegram user: GitHub token, webhook secret, repo, branch, timeout preferences, onboarding state.
- **`active_reviews`** — one row per commit currently under review, storing commit metadata and the AI decision as JSON.
- **`jobs`** — the crash-safe queue (Phase 3) that commit processing runs off, so an in-flight analysis survives a restart/redeploy.

For local development, `docker-compose up -d` spins up a throwaway Postgres — see `docker-compose.yml`.

---

## Deploying to Render

The included `render.yaml` blueprint (Phase 5) wires this up for you in one step:

1. Push your code to a GitHub repo
2. In the Render dashboard: **New +** → **Blueprint**, point it at your repo
3. Render provisions a `starter`-plan web service and a `basic-256mb`-plan Postgres instance, and wires `DATABASE_URL` and `PUBLIC_URL` between them automatically
4. Set the two secrets Render will prompt you for: `TELEGRAM_BOT_TOKEN` and `GROQ_API_KEY`
5. (Optional) Set `ADMIN_CHAT_ID` to your own Telegram chat_id to receive admin alerts on repeated failures
6. Deploy

**Note on cost:** the free web-service tier spins down on idle, which breaks the timeout worker and job workers (they need the process to stay running between webhook events) — this is why the blueprint uses the `starter` (always-on) compute plan. There's no free managed-Postgres tier on Render anymore either; `basic-256mb` is the cheapest paid tier that works here.

If you'd rather set this up manually instead of using the blueprint, the manual steps are the same as above minus the one-click provisioning — create the web service and Postgres instance separately in the dashboard and copy the connection string into `DATABASE_URL` yourself.

---

## A note on the rollback strategies

**`revert` (default):** Creates a new commit that undoes the changes of the declined commit. This is the safe option — it preserves full history and is easy to audit. GitHub shows both the original commit and the revert, which keeps things transparent.

**`force_push`:** Removes the declined commit from the branch's history entirely by force-pushing the parent SHA. This is cleaner-looking but it rewrites history, which can cause problems for anyone who already pulled the branch. Only use this if you know what you're doing and your team is aware.

---

## License

Copyright © 2026 Michael Defaru. All rights reserved.

This project is proprietary and closed-source. It is shared publicly strictly for portfolio review and educational evaluation.

Permissions: Recruiters, hiring managers, and engineers are authorized to view, clone, and test this repository locally for the sole purpose of evaluating my candidacy for employment.

Restrictions: Any other use—including commercial deployment, modification, or unauthorized public distribution—is strictly prohibited.
