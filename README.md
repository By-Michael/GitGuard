# 🛡️ Commit Guardian

A Telegram bot that watches your GitHub repo, has an AI read every commit the second it lands, and asks you — right there in chat — whether to keep it or roll it back.

No dashboard to check, no CI logs to dig through. Someone pushes, your phone buzzes, you tap Accept or Decline.

## Why this exists

Most "commit review" tools assume someone is actually staring at a dashboard. In practice nobody is — reviews pile up, bad pushes sit on `main` for hours, and rollbacks happen manually at the worst possible time. Commit Guardian skips the dashboard entirely and meets you where you already are: Telegram. It reads the diff, the changed files, and enough of the surrounding repo to have context, then gives you a plain-language risk assessment with a couple of buttons underneath it.

If you don't respond, it doesn't just sit there forever either — you set a timeout, and it auto-accepts or auto-reverts based on what you tell it to do.

## What it actually does

1. GitHub sends a webhook the moment someone pushes.
2. Commit Guardian pulls the commit, the diff, the changed files, and relevant repo context (README, package manifests, config, etc.).
3. That gets handed to Groq's Llama 3.3 70B model, which comes back with a decision (`accept` / `decline` / `review`), a risk level, and its reasoning.
4. You get a Telegram message with the summary and three buttons: **Accept**, **Decline** (rolls the commit back), and **Report** (generates a full Word doc explaining the call).
5. If you ignore it long enough, your configured timeout kicks in and the bot acts on your behalf.

Rollbacks are done properly through the GitHub API — by default it computes a three-way merge and reverts *just* that commit, so anything pushed on top of it survives. There's also a force-push mode if you'd rather just rewind the branch.

Everything is multi-user: each Telegram chat gets its own repo, branch, GitHub token, and timeout settings, all stored locally in SQLite. There's no shared state between users.

## Setup

You'll need a Telegram bot, a Groq API key, and somewhere to host this with a public HTTPS URL (Render, Railway, Fly.io, a VPS — anything works; a free Render web service is what the `.env.example` is written for).

**1. Clone it and install dependencies**

```bash
git clone https://github.com/By-Michael/GitGuard.git
cd GitGurad
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

**2. Get your two secrets**

- A Telegram bot token from [@BotFather](https://t.me/BotFather) — send `/newbot` and follow the prompts.
- A Groq API key from [console.groq.com](https://console.groq.com).

**3. Configure**

```bash
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY`, and `PUBLIC_URL` (the URL your server will be reachable at once deployed). Everything else has sane defaults.

**4. Run it**

```bash
python webhook_server.py
```

or, if you'd rather run it through uvicorn directly:

```bash
uvicorn webhook_server:app --host 0.0.0.0 --port 8000
```

**5. Point Telegram at your server**

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=<PUBLIC_URL>/webhook/telegram"
```

**6. Talk to your bot**

Open it in Telegram and send `/start`. It'll walk you through a short setup wizard — which repo, which branch, your GitHub token, and how you want timeouts handled. At the end it gives you a per-user webhook URL and secret to paste into your repo's **Settings → Webhooks → Add webhook** (content type `application/json`).

That's it — push a commit and watch it show up in your chat.

## GitHub token notes

You'll need a classic personal access token with the `repo` and `read:user` scopes. GitHub lets you set an expiry on these (90 days is a reasonable default) — when it expires or gets revoked, the bot will tell you it can no longer fetch commits and ask you to send `/reconnect`. That refreshes just the token without touching your repo, branch, or timeout settings, so you don't have to redo the whole setup.

## Bot commands

| Command | What it does |
|---|---|
| `/start` | Full setup, or a complete reconfigure (wipes existing settings) |
| `/reconnect` | Replace an expired/revoked GitHub token, keep everything else |
| `/status` | Show your current config and any pending reviews |
| `/settings` | Adjust your timeout settings |
| `/help` | List commands |
| `/cancel` | Bail out of setup at any step |

## Rollback strategies

Set via `ROLLBACK_STRATEGY` in your `.env`:

- **`revert`** (default, recommended) — computes a proper revert using GitHub's merge API. Only undoes the declined commit; anything pushed after it stays intact.
- **`force_push`** — resets the branch straight back to the parent commit. Simple, but destructive if anyone has pushed on top of it since — use with care, mostly useful on solo projects or tightly controlled branches.

## How it's built

| File | Responsibility |
|---|---|
| `webhook_server.py` | FastAPI app — receives GitHub & Telegram webhooks, runs the review pipeline |
| `github_service.py` | GitHub API client — fetches commits/repo context, handles rollbacks |
| `ai_service.py` | Talks to Groq, turns a diff + repo context into a structured decision |
| `telegram_service.py` | Bot UX — onboarding wizard, review cards, transparency reports |
| `timeout_worker.py` | Background loop that auto-acts on reviews nobody responded to |
| `database.py` | SQLite layer — per-user config and review state |
| `config.py` | Environment/config loading |

Stack: Python, FastAPI, httpx, SQLite, python-docx, the Telegram Bot API, the GitHub REST API, and Groq's Llama 3.3 70B for the actual analysis.

## A few honest limitations

- It currently checks for timed-out reviews once a minute — fine for most use cases, but don't expect sub-second auto-actions.
- Large pushes are capped to the most recent 3 commits per push (configurable in code) so a 50-commit rebase doesn't flood your chat.
- It's one bot, one repo per user at a time. If you manage several repos, you'll want a chat per repo (or a small extension to support more).

## License

MIT — use it, fork it, break it, improve it.
