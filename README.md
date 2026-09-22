# Twenty → Telegram deal bot

Pings a Telegram chat every time a deal changes stage in [Twenty](https://app.twenty.com),
and answers `/daily`, `/weekly` and `/monthly` reports on demand.

Bot: **@corgiventuresbot**

## Stages

Mapped live from Twenty's metadata, so renaming or adding a stage in the UI is
picked up on the next poll (and immediately if a deal turns up on a stage the
bot does not recognise). The E&S pipeline is:

| Twenty value | Label | |
|---|---|---|
| `QUOTE_RECEIVED` | Submission Received | 📥 |
| `QUOTE_SENT` | Quote Sent | 📤 |
| `CLOSED_WON` | Closed Won | 💰 |

Watched pipelines are set in `config.json` (`ES_CARRIER` plus deals with no
pipeline set, which are usually mis-keyed E&S deals).

**Editing the stage options in the Twenty UI resets every deal that is not on
the default option.** It happened twice while this was being built. The bot
guards against the fallout — more than `burst_threshold` stage changes in one
poll are summarised into a single message rather than a card per deal, and
tagged in the history so the reports do not count them as rep activity — but
snapshot the deals before touching those options.

## What it sends

One card per event — a stage move in either direction, a new deal, or a deal
leaving the pipeline:

```
💰 E&S — CLOSED WON 💰

🏢 Creative Insurance Marketing Company (CIMCO)
🎯 Deal: Sandin Insurance Group - New Deal
🏷 Type: Insurance Broker
🌎 State: Oregon
🔄 Stage Updated: Agreement Signed → Closed Won
💰 Amount: $3,080.63
⏰ Updated: September 22, 2026 at 1:31 PM ET

👨‍💼 AE: Harry Palmer
👥 BDR: Joshua Byrne
📍 Stage: Closed Won

📅 Close Date: September 30, 2026 at 11:37 AM ET

🔗 View Opportunity in Twenty
```

### Drafts vs submitted

Twenty creates the record the moment someone clicks New, so **nothing is
announced until the deal is explicitly submitted**: the rep ticks the
**Submitted** checkbox on the opportunity (field `submitted`, added
2026-09-22). Opening the form, saving a half-filled draft and editing it over
several days all leave the box unticked, so none of them ping. The card goes out
on the first poll after it is ticked, carrying whatever stage the deal landed on.

It is sent **once**. Un-ticking returns the deal to draft; ticking again is
treated as a deliberate resubmit and sends a fresh card. Stage changes on an
already submitted deal ping immediately as usual, and a deal that is deleted
while still a draft is never announced at all.

`require_submitted_flag: false` in `config.json` falls back to the older
heuristic — announce once `new_deal_required_fields` are filled and the record
has been idle for `new_deal_settle_minutes`. That fallback also kicks in
automatically if the `submitted` field is missing from the workspace.

Company, type, state, AE, BDR, source and close date are dropped from the card
when Twenty has nothing in them. **Quote Sent and Closed Won always show an
Amount**, reading `$0 (not set in Twenty)` when the field is blank, because that
is the number being reported on.

## Commands

| Command | What you get |
|---|---|
| `/daily` | Today: closed won and quotes sent with values, every stage change, pipeline now |
| `/weekly` | Last 7 days, plus per-owner and per-day breakdowns |
| `/monthly` | Last 30 days, same shape |
| `/pipeline` | Deal counts and value by stage right now |

(`/chatid` also works, undocumented, for adding the bot to a group.)

## Hosting

**GitHub Actions** (`.github/workflows/poll.yml`) runs `python3 bot.py once`
every 5 minutes: it pings anything that moved since the last run and answers any
commands sent in between.

State (last-seen stage per deal, the event history behind the reports, the
appointed-company set) lives in the **Actions cache**, never in the repo,
because it holds deal and company names. The cache is not publicly downloadable
and is rewritten on every run, so the repo itself stays code-only and can be
public without publishing anything about your pipeline.

Two things to know about Actions as a host:

- 5 minutes is GitHub's floor for `schedule`, and a busy queue can add a few
  more, so a ping lands within roughly 5–10 minutes of the move and a `/daily`
  reply takes about as long. For instant replies, run `python3 bot.py serve` on
  a box that stays up — same code, resident loop.
- GitHub disables scheduled workflows in a repo with no commits for 60 days, so
  push something occasionally (or hit Run workflow) to keep it alive.

`.github/workflows/digest.yml` also posts a daily digest at 18:00 ET on
weekdays, a weekly one on Monday morning, and a monthly one on the 1st.

### Repo secrets

| Secret | Value |
|---|---|
| `TWENTY_TOKEN` | Twenty API key (the working one, `~/.twenty_team_token`) |
| `TELEGRAM_BOT_TOKEN` | @corgiventuresbot's token |
| `TELEGRAM_CHAT_ID` | Target chat id; comma-separate to post to several |

### Running it locally instead

```bash
python3 setup_bot.py <BOT_TOKEN>   # writes ~/.telegram_twenty_bot
python3 bot.py seed                # record current stages, no pings
python3 bot.py serve               # resident loop, instant replies
```

`install.sh` registers a launchd agent for the resident mode on this Mac.

## Files

| File | |
|---|---|
| `bot.py` | Polling, notifications, command handling, the four run modes |
| `reports.py` | `/daily`, `/weekly`, `/monthly` |
| `stages.py` | Stage and pipeline mappings, read live from Twenty metadata |
| `twenty_api.py` | REST client (Twenty needs a curl User-Agent; Cloudflare 403s urllib) |
| `state/state.json` | Last-seen stage per deal |
| `state/events.jsonl` | Event log the reports read |
