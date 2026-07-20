# Movie-Alert

Get a **Telegram** ping the moment a **movie + theatre + date** opens for booking
on BookMyShow. You add and remove watches straight from Telegram (`/request`,
`/list`, `/delete`) — no file editing. Runs on **GitHub Actions**, triggered every
~10 min by **cron-job.org** — nothing to keep running on your own machine.

<img width="1220" height="1076" alt="Media" src="https://github.com/user-attachments/assets/3c6a8f8e-5458-42a5-a870-9001a9990de3" />


## How it works

1. **cron-job.org** triggers the workflow every ~10 min (GitHub's own scheduler
   is unreliable, so we trigger it externally).
2. Each run, `bot.py` does three things:
   - **Drains your Telegram commands** since the last run (`getUpdates`) and
     applies them to the request store.
   - **Checks every active watch** — fetching each unique movie+date once through
     **ScraperAPI** (an India IP; BMS 403s foreign/datacenter IPs).
   - When a watch's date opens at its venue, it **sends the alert and deletes that
     watch**, then commits `requests.json` back to the repo.
3. Because commands are only processed on each ~10-min run, replies (including
   `/request` confirmations) can lag up to ~10 minutes. Commands are accepted
   **only from your own chat** (`TELEGRAM_CHAT_ID`).

> The older single-target `poller.py` (driven by `config.json` + `state.json`) is
> still in the repo and reused by `bot.py` for its fetch/Telegram helpers.

## 1. Telegram bot

- Message **@BotFather** → `/newbot` → copy the **token**.
- Send your new bot any message, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id` — that's
  your **chat id**.

## 2. ScraperAPI key

Sign up at **scraperapi.com** (free tier) and copy your API key.

## 3. Add repo secrets

**Settings → Secrets and variables → Actions:**

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SCRAPERAPI_KEY`

## 4. Add watches from Telegram

Message your bot. Commands are only accepted from your own chat.

**`/request`** — add a watch. Send one message with labeled lines (Time optional):

```
/request
Movie Name: Spider-Man: Brand New Day (IMAX 2D)
Date: 2026-08-01
Venue: Broadway Cinemas Coimbatore
Time: 7:00 PM
URL: https://in.bookmyshow.com/movies/coimbatore/spiderman-brand-new-day/buytickets/ET00447840/20260801
```

- **URL** — the movie's BookMyShow page (`.../movies/<city>/<slug>/buytickets/ET.../<date>`,
  or the shorter `.../movies/<city>/<slug>/ET...`). City, slug and event code are
  read from it.
- **Date** — `YYYY-MM-DD`, `YYYYMMDD`, or `DD-MM-YYYY`.
- **Venue** — the theatre name. The bot resolves it to BMS's venue code (e.g.
  `BWCB`) from the page. If several match it asks you to be more specific; if none
  are listed yet it watches by name and matches when booking opens.
- **Time** — informational only (BMS booking links are per-date, not per-showtime);
  it's echoed back in the alert.

The bot replies with a confirmation and an id. When the date opens at that venue you
get the alert **and the watch is auto-deleted**.

**`/list`** — show active watches with their ids.

**`/delete <id>`** — remove one watch (or `/delete all`).

## 5. Schedule it with cron-job.org

**a. GitHub token** — *Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate*. Scope it to this repo, permission
**Actions: Read and write**. Copy the token.

**b. cron-job.org** — create a cronjob:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/<you>/<repo>/actions/workflows/booking-watch.yml/dispatches` |
| Schedule | every 10 minutes |
| Method | `POST` |
| Header | `Accept: application/vnd.github+json` |
| Header | `Authorization: Bearer <your-token>` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Body | `{"ref":"main"}` |

Save, then **Run now**. GitHub returns `204`; a run appears under **Actions**.
From then on it fires every 10 min. Keep the token only in cron-job.org.

## Geo-block (why ScraperAPI)

BMS only serves India and blocks datacenter IPs, so GitHub's US runners get a
**403** on a direct request. `SCRAPERAPI_KEY` routes through an India IP and fixes
it. Alternatives: set a `PROXY_URL` secret (India proxy), or run from a machine in
India. It's IP/geo-based — headers alone won't get past it.

## Reuse it (fork)

Fork the repo, **enable Actions** on the fork (off by default), add your own
secrets, and set up your own cron-job.org trigger. One repo now handles **many
movies at once** via `/request` — no per-movie fork needed. Keep secrets out of the
repo (`.env` is gitignored; if a token has ever been committed, rotate it).

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Drain Telegram commands, check all watches, alert + auto-delete |
| `requests.json` | Auto-managed store: active watches + Telegram update offset |
| `poller.py` | Legacy single-target watcher; supplies fetch/Telegram helpers to `bot.py` |
| `config.json`, `state.json` | Legacy single-target config + last-seen state |
| `.github/workflows/booking-watch.yml` | The runner (dispatched by cron-job.org) |
| `requirements.txt` | Python deps (`requests`) |
