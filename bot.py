#!/usr/bin/env python3
"""Twenty CRM -> Telegram deal-stage bot.

Posts one Telegram message every time a deal changes stage in Twenty, and
answers /daily, /weekly and /monthly reports.

Stages watched, in order:
    Meeting Booked -> Quote Received -> Quote Sent
    -> Agreement Signed -> Closed Won

Modes:
    python3 bot.py once              one poll + answer queued commands, then exit
    python3 bot.py loop              poll every `poll_seconds` for RUN_SECONDS,
                                     then exit (this is what GitHub Actions runs:
                                     one run per cron tick, polling throughout)
    python3 bot.py serve             resident loop, instant replies
    python3 bot.py report daily      post a digest for a period and exit
    python3 bot.py seed              record current stages without notifying

Credentials (env var first, so GitHub Actions secrets work, then local file):
    TWENTY_TOKEN          | ~/.twenty_team_token
    TELEGRAM_BOT_TOKEN    | ~/.telegram_twenty_bot  {"bot_token":..,"chat_id":..}
    TELEGRAM_CHAT_ID      |
"""
import html, json, os, sys, time, traceback
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twenty_api as tw
import stages as stages_module
from stages import (STAGE_ORDER, STAGE_EMOJI, stage_label, pipeline_label,
                    stage_rank)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
OFFSET_FILE = os.path.join(STATE_DIR, "tg_offset.json")
APPOINTED_FILE = os.path.join(STATE_DIR, "appointed.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
TG_FILE = os.path.expanduser("~/.telegram_twenty_bot")

BULLET = "•"
DEFAULT_CONFIG = {
    "pipelines": ["ES_CARRIER"],
    "include_unassigned": True,
    "poll_seconds": 60,
    "app_base_url": "https://app.twenty.com",
    "timezone": "America/New_York",
    "notify_new": True,
    "notify_removed": False,
    # A deal row exists from the moment someone clicks New in Twenty, so
    # nothing is announced until the deal is explicitly submitted: the rep
    # ticks the Submitted checkbox on the record. Everything before that is a
    # draft as far as the bot is concerned.
    "submitted_field": "submitted",
    # Fallback for when the Submitted field is missing from the workspace:
    # announce once the required fields are filled and the record has been
    # idle this long. Set require_submitted_flag false to use it deliberately.
    "require_submitted_flag": True,
    "new_deal_settle_minutes": 5,
    "new_deal_required_fields": ["name"],
    # The Appointed toggle on a company record.
    "notify_appointed": True,
    "notify_unappointed": True,
    # Editing the stage options in Twenty rewrites every deal at once. Past
    # this many stage changes in one poll, send a single summary instead of a
    # card per deal.
    "burst_threshold": 10,
    "burst_notify": False,
    "quiet_hours": [],
}

# Short name used in the header line, e.g. "E&S — QUOTE SENT".
PIPELINE_SHORT = {"ES_CARRIER": "E&S", "DIRECT_SOLD": "DIRECT",
                  "BROKER_SOLD": "BROKER", None: "UNASSIGNED"}
# Stages whose message always carries a value, $0 included.
MONEY_STAGES = {"CLOSED_WON", "QUOTE_SENT"}
SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€"}
TZ_SHORT = {"EDT": "ET", "EST": "ET", "PDT": "PT", "PST": "PT",
            "CDT": "CT", "CST": "CT", "MDT": "MT", "MST": "MT",
            "BST": "UK", "GMT": "UK"}


# ------------------------------------------------------------------ plumbing
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in load_json(CONFIG_FILE, {}).items()
                if not k.startswith("_")})
    return cfg


def tz(cfg):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone") or "America/New_York")
    except Exception:
        return None


def now_local(cfg):
    return datetime.now(tz(cfg))


def preflight():
    """Say which credentials arrived, without printing any of them. Makes a
    failed Actions run diagnosable from the log alone."""
    have = {
        "TWENTY_TOKEN": bool(os.environ.get("TWENTY_TOKEN")),
        "TELEGRAM_BOT_TOKEN": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "TELEGRAM_CHAT_ID": bool(os.environ.get("TELEGRAM_CHAT_ID")),
    }
    if any(have.values()) or os.environ.get("CI"):
        log("credentials: " + ", ".join(
            f"{k}={'set' if v else 'MISSING'}" for k, v in have.items()))
        missing = [k for k, v in have.items() if not v]
        if missing:
            log("Add the missing secret(s) under Settings -> Secrets and "
                "variables -> Actions -> Repository secrets. Names are "
                "case-sensitive, and repository secrets are not the same as "
                "variables or environment secrets.")


def tg_config():
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env_token:
        chats = [c.strip() for c in
                 (os.environ.get("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
        return {"bot_token": env_token.strip(),
                "chat_id": chats[0] if chats else None,
                "allowed_chats": chats[1:],
                "home": ({"chat_id": chats[0],
                          "thread_id": int(os.environ["TELEGRAM_THREAD_ID"])}
                         if chats and os.environ.get("TELEGRAM_THREAD_ID") else None)}
    cfg = load_json(TG_FILE, None)
    if not cfg or not cfg.get("bot_token"):
        sys.exit(f"No Telegram credentials. Set TELEGRAM_BOT_TOKEN, or create {TG_FILE} "
                 "with:  python3 setup_bot.py <BOT_TOKEN>")
    return cfg


def chat_ids(tg):
    ids = []
    if tg.get("chat_id"):
        ids.append(str(tg["chat_id"]))
    for c in tg.get("allowed_chats") or []:
        if str(c) not in ids:
            ids.append(str(c))
    return ids


def tg_api(method, tg, params=None, timeout=60, attempts=3):
    """Telegram call with retries — a dropped TLS handshake would otherwise
    swallow a ping silently."""
    url = f"https://api.telegram.org/bot{tg['bot_token']}/{method}"
    for attempt in range(attempts):
        req = urllib.request.Request(url,
                                     data=urllib.parse.urlencode(params or {}).encode())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "replace")
            if e.code == 429 and attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            if e.code == 409:
                # Another copy of the bot is holding the long poll. Harmless in
                # itself, but it means two instances are running.
                log("telegram getUpdates: another bot instance is polling")
                return None
            log(f"telegram {method}: {e.code} {body}")
            return None
        except Exception as e:
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            log(f"telegram {method} gave up after {attempts}: {e}")
    return None


def home(tg):
    """(chat_id, thread_id) the bot is pinned to with /here, or None."""
    h = tg.get("home") or {}
    return (str(h["chat_id"]), h.get("thread_id")) if h.get("chat_id") else None


def targets(tg):
    """Where pings go: the pinned chat/topic if set, else every listed chat."""
    return [home(tg)] if home(tg) else [(c, None) for c in chat_ids(tg)]


def set_home(tg, chat_id, thread_id):
    """Pin pings to one chat and, in a forum group, one topic."""
    data = load_json(TG_FILE, {})
    h = {"chat_id": str(chat_id)}
    if thread_id:
        h["thread_id"] = int(thread_id)
    data["home"] = h
    allowed = [str(c) for c in data.get("allowed_chats") or []]
    if str(chat_id) not in allowed and str(chat_id) != str(data.get("chat_id")):
        allowed.append(str(chat_id))
    data["allowed_chats"] = allowed
    save_json(TG_FILE, data)
    os.chmod(TG_FILE, 0o600)
    tg["home"], tg["allowed_chats"] = h, allowed


def wrong_topic(tg, chat_id, thread_id):
    """True for a message in the pinned group but outside the pinned topic."""
    h = home(tg)
    if not h or not h[1] or str(chat_id) != h[0]:
        return False
    return str(thread_id or "") != str(h[1])


def send(tg, text, chat_id=None, thread_id=None):
    for cid, tid in ([(chat_id, thread_id)] if chat_id else targets(tg)):
        params = {"chat_id": cid, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"}
        if tid:
            params["message_thread_id"] = tid
        tg_api("sendMessage", tg, params)
        time.sleep(0.35)


# ---------------------------------------------------------------- formatting
def amount_value(amount):
    """The deal amount as a float, or None when it has not been filled in."""
    micros = (amount or {}).get("amountMicros")
    if micros in (None, ""):
        return None
    try:
        return int(micros) / 1_000_000
    except (TypeError, ValueError):
        return None


def fmt_money(val, currency="USD"):
    sym = SYMBOLS.get((currency or "USD").upper(), (currency or "USD").upper() + " ")
    return f"{sym}{val:,.2f}" if val % 1 else f"{sym}{val:,.0f}"


def money(amount):
    val = amount_value(amount)
    return None if val is None else fmt_money(val, (amount or {}).get("currencyCode"))


def fmt_time(iso, cfg):
    """'September 22, 2026 at 9:52 AM ET'."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz(cfg)) if tz(cfg) else dt
    label = TZ_SHORT.get(local.strftime("%Z"), local.strftime("%Z"))
    return f"{local.strftime('%B %-d, %Y at %-I:%M %p')} {label}".strip()


def pretty_enum(value):
    """INSURANCE_BROKER -> Insurance Broker."""
    if not value:
        return None
    return " ".join(w.capitalize() for w in str(value).replace("_", " ").split())


def deal_title(opp, ctx=None):
    """Deals created in the Twenty UI start blank, so fall back to the company."""
    name = (opp.get("name") or "").strip()
    if name:
        return name
    company = company_field(opp, ctx, "name") or opp.get("company")
    return f"{company} (unnamed deal)" if company else "(unnamed deal)"


def company_field(opp, ctx, field):
    rec = (ctx or {}).get("companies", {}).get(opp.get("companyId")) or {}
    if field == "state":
        return (rec.get("address") or {}).get("addressState") or rec.get("region")
    if field == "type":
        return pretty_enum(rec.get("leadType"))
    return rec.get(field)


def member(ctx, member_id):
    return (ctx or {}).get("members", {}).get(member_id)


def arrow(old, new):
    """Forward through the funnel, or back."""
    return "\u2192" if stage_rank(new) >= stage_rank(old) else "\u21a9"


def deal_url(cfg, opp_id):
    return f"{cfg['app_base_url'].rstrip('/')}/object/opportunity/{opp_id}"


def esc(v):
    return html.escape(str(v))


def card(cfg, opp, ctx, headline, updated_label, stage=None, transition=None):
    """The message body. Lines with nothing behind them are left out."""
    stage = stage or opp.get("stage")
    pipeline = opp.get("pipeline")
    emoji = STAGE_EMOJI.get(stage, "\U0001f514")
    header = (f"{emoji} <b>{PIPELINE_SHORT.get(pipeline, pipeline_label(pipeline).upper())}"
              f" — {headline.upper()}</b> {emoji}")

    lines = [header, ""]
    company = company_field(opp, ctx, "name")
    if company:
        lines.append(f"\U0001f3e2 {esc(company)}")
    lines.append(f"\U0001f3af Deal: <b>{esc(deal_title(opp, ctx))}</b>")
    ctype = company_field(opp, ctx, "type")
    if ctype:
        lines.append(f"\U0001f3f7 Type: {esc(ctype)}")
    state = company_field(opp, ctx, "state")
    if state:
        lines.append(f"\U0001f30e State: {esc(state)}")
    lines.append(f"\U0001f504 {updated_label}: {transition or ''}<b>{stage_label(stage)}</b>")

    amt = money(opp.get("amount"))
    if stage in MONEY_STAGES:
        val = amount_value(opp.get("amount")) or 0
        cur = (opp.get("amount") or {}).get("currencyCode")
        lines.append(f"\U0001f4b0 Amount: <b>{fmt_money(val, cur)}</b>")
    elif amt:
        lines.append(f"\U0001f4b0 Amount: <b>{amt}</b>")

    when = fmt_time(opp.get("updatedAt"), cfg)
    if when:
        lines.append(f"⏰ Updated: {when}")

    people = []
    ae = member(ctx, opp.get("ownerId")) or opp.get("hubspotOwner")
    if ae:
        people.append(f"\U0001f468‍\U0001f4bc AE: {esc(ae)}")
    bdr = member(ctx, opp.get("bdrId"))
    if bdr:
        people.append(f"\U0001f465 BDR: {esc(bdr)}")
    source = pretty_enum(opp.get("dealSource"))
    if source:
        people.append(f"\U0001f4e3 Source: {esc(source)}")
    people.append(f"\U0001f4cd Stage: {stage_label(stage)}")
    lines += [""] + people

    close = fmt_time(opp.get("closeDate"), cfg)
    if close:
        lines += ["", f"\U0001f4c5 Close Date: {close}"]

    lines += ["", f'\U0001f517 <a href="{deal_url(cfg, opp["id"])}">'
                  f'View Opportunity in Twenty</a>']
    return "\n".join(lines)


def stage_change_msg(cfg, opp, old, new, ctx):
    moved_back = old is not None and stage_rank(new) < stage_rank(old)
    headline = f"{stage_label(new)}{' (moved back)' if moved_back else ''}"
    transition = f"{stage_label(old)} {arrow(old, new)} " if old else ""
    return card(cfg, opp, ctx, headline, "Stage Updated",
                stage=new, transition=transition)


def new_deal_msg(cfg, opp, ctx):
    return card(cfg, opp, ctx, "New Deal", "Created At")


def burst_msg(cfg, stage_moves):
    """One message for a bulk change, instead of a card per deal.

    Editing the stage options in Twenty rewrites every deal's stage at once;
    130 cards is noise, and the shape of the change is what matters."""
    by_stage = {}
    for _, opp, old in stage_moves:
        by_stage.setdefault(opp.get("stage"), []).append(opp)
    lines = [f"\u26a0\ufe0f <b>BULK STAGE CHANGE \u2014 {len(stage_moves)} deals</b> \u26a0\ufe0f", "",
             "This looks like a pipeline edit in Twenty rather than reps moving "
             "deals, so here is the summary instead of one card each:", ""]
    for st in sorted(by_stage, key=stage_rank):
        lines.append(f"{STAGE_EMOJI.get(st, BULLET)} \u2192 {stage_label(st)}: "
                     f"<b>{len(by_stage[st])}</b>")
    lines += ["", f"\u23f0 {fmt_time(now_local(cfg).isoformat(), cfg)}"]
    return "\n".join(lines)


def removed_msg(cfg, prev, ctx):
    pipeline = prev.get("pipeline")
    return (f"\U0001f5d1 <b>{PIPELINE_SHORT.get(pipeline, pipeline_label(pipeline).upper())}"
            f" — DEAL REMOVED</b> \U0001f5d1\n\n"
            f"\U0001f3af Deal: <b>{esc(prev.get('name') or '(unnamed deal)')}</b>\n"
            f"\U0001f4cd Last stage: {stage_label(prev.get('stage'))}\n"
            f"⏰ Noticed: {fmt_time(now_local(cfg).isoformat(), cfg)}")


def company_card(cfg, rec, appointed=True):
    """Card for a company whose Appointed toggle just changed."""
    head = "APPOINTED" if appointed else "APPOINTMENT REMOVED"
    emoji = "\U0001f91d" if appointed else "\u21a9"
    lines = [f"{emoji} <b>AGENCY \u2014 {head}</b> {emoji}", ""]
    lines.append(f"\U0001f3e2 <b>{esc(rec.get('name') or '(unnamed company)')}</b>")

    ctype = pretty_enum(rec.get("leadType"))
    if ctype:
        lines.append(f"\U0001f3f7 Type: {esc(ctype)}")
    state = (rec.get("address") or {}).get("addressState") or rec.get("region")
    if state:
        lines.append(f"\U0001f30e State: {esc(state)}")
    domain = (rec.get("domainName") or {}).get("primaryLinkUrl")
    if domain:
        lines.append(f"\U0001f310 {esc(domain)}")
    phone = (rec.get("phone") or {}).get("primaryPhoneNumber")
    if phone:
        lines.append(f"\U0001f4de {esc(phone)}")

    when = fmt_time(rec.get("updatedAt"), cfg)
    if when:
        lines.append(f"\u23f0 {'Appointed' if appointed else 'Changed'}: {when}")
    by = (rec.get("updatedBy") or {}).get("name") or "Unassigned"
    lines.append(f"\U0001f468\u200d\U0001f4bc By: {esc(by)}")

    url = f"{cfg['app_base_url'].rstrip('/')}/object/company/{rec['id']}"
    lines += ["", f'\U0001f517 <a href="{url}">View Company in Twenty</a>']
    return "\n".join(lines)


def record_appointment(rec, appointed, cfg):
    ev = {
        "ts": now_local(cfg).isoformat(timespec="seconds"),
        "kind": "appointed" if appointed else "unappointed",
        "id": rec.get("id"),
        "name": rec.get("name"),
        "by": (rec.get("updatedBy") or {}).get("name"),
        "state": (rec.get("address") or {}).get("addressState") or rec.get("region"),
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(ev) + "\n")
    return ev


def poll_appointments(cfg, tg, seed=False):
    """Ping when a rep flips the Appointed toggle on a company.

    Only appointed companies are fetched, so this stays one small query however
    large the company table gets."""
    if not cfg.get("notify_appointed"):
        return 0
    known = load_json(APPOINTED_FILE, {})
    try:
        recs = tw.find_many("companies", {"filter": "appointed[eq]:true"},
                            page_size=60, max_pages=40)
    except tw.TwentyError as e:
        log(f"appointed lookup failed: {e}")
        return 0

    live = {r["id"]: r for r in recs}
    sent = 0
    for cid, rec in live.items():
        if cid in known:
            continue
        if not seed:
            record_appointment(rec, True, cfg)
            send(tg, company_card(cfg, rec, appointed=True))
            log(f"sent appointed: {rec.get('name')}")
            sent += 1
        known[cid] = {"name": rec.get("name"),
                      "since": now_local(cfg).isoformat(timespec="seconds")}

    for cid in [c for c in known if c not in live]:
        prev = known.pop(cid)
        if not seed and cfg.get("notify_unappointed"):
            rec = {"id": cid, "name": prev.get("name")}
            try:
                rec = tw.get(f"/companies/{cid}")["data"]["company"]
            except tw.TwentyError:
                pass
            record_appointment(rec, False, cfg)
            send(tg, company_card(cfg, rec, appointed=False))
            log(f"sent unappointed: {prev.get('name')}")
            sent += 1

    save_json(APPOINTED_FILE, known)
    if seed:
        log(f"seeded {len(known)} appointed companies")
    return sent


# --------------------------------------------------------------- poll engine
def watched(opp, cfg):
    p = opp.get("pipeline")
    if p is None:
        return bool(cfg.get("include_unassigned"))
    pipes = cfg.get("pipelines") or []
    return (not pipes) or (p in pipes)


def build_ctx(opps, caches):
    """Resolve the people and company details needed to render `opps`.

    Only ever called with the handful of deals about to appear in a message;
    resolving all of them up front stalled the poll."""
    members, companies = caches
    if not members:
        try:
            members.update(tw.workspace_members())
        except tw.TwentyError as e:
            log(f"member lookup failed: {e}")
    missing = {o.get("companyId") for o in opps if o.get("companyId")} - set(companies)
    if missing:
        try:
            companies.update(tw.companies_by_id(missing))
        except tw.TwentyError as e:
            log(f"company lookup failed: {e}")
    return {"members": members, "companies": companies}


def record_event(kind, opp, old, new, ctx, cfg, bulk=False):
    """Append to the event log that /daily, /weekly and /monthly read."""
    ev = {
        "ts": now_local(cfg).isoformat(timespec="seconds"),
        "kind": kind,
        "id": opp.get("id"),
        "name": deal_title(opp, ctx),
        "pipeline": opp.get("pipeline"),
        "from": old,
        "to": new,
        "owner": member(ctx, opp.get("ownerId")) or opp.get("hubspotOwner"),
        "bdr": member(ctx, opp.get("bdrId")),
        "company": company_field(opp, ctx, "name"),
        "amount": money(opp.get("amount")),
        "amount_value": amount_value(opp.get("amount")),
        "currency": (opp.get("amount") or {}).get("currencyCode") or "USD",
    }
    if bulk:
        ev["bulk"] = True          # a pipeline edit, not a rep moving a deal
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(ev) + "\n")
    return ev


def parse_iso(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def field_filled(opp, field):
    val = opp.get(field)
    if field == "company":
        return bool(opp.get("companyId"))
    if field == "owner":
        return bool(opp.get("ownerId") or opp.get("hubspotOwner"))
    if field == "amount":
        return amount_value(opp.get("amount")) is not None
    if isinstance(val, str):
        return bool(val.strip())
    return val not in (None, "", {}, [])


def is_submitted(opp, cfg):
    """Has this deal been submitted?

    The rep ticking <b>Submitted</b> on the record is the trigger: opening the
    New form, saving a half-filled draft and editing it later all leave the box
    unticked, so none of them announce anything.

    Only when the workspace has no Submitted field (or `require_submitted_flag`
    is off) does it fall back to "required fields filled and idle for a while".

    Returns (ready, reason) so the log says what it is waiting on."""
    field = cfg.get("submitted_field") or "submitted"
    if cfg.get("require_submitted_flag"):
        if field not in opp:
            log(f"no '{field}' field on opportunities; falling back to the idle rule")
        else:
            return (True, "submitted") if opp.get(field) else (False, "draft, not submitted")

    for field in cfg.get("new_deal_required_fields") or []:
        if not field_filled(opp, field):
            return False, f"no {field} yet"
    settle = float(cfg.get("new_deal_settle_minutes") or 0)
    if settle:
        touched = parse_iso(opp.get("updatedAt"))
        if touched:
            idle = (datetime.now(timezone.utc) - touched).total_seconds() / 60
            if idle < settle:
                return False, f"edited {idle:.1f}m ago, settling"
    return True, "submitted"


def in_quiet_hours(cfg):
    hours = cfg.get("quiet_hours") or []
    if len(hours) != 2:
        return False
    start, end = hours
    h = now_local(cfg).hour
    return (start <= h or h < end) if start > end else (start <= h < end)


def poll_once(cfg, tg, state, caches, seed=False):
    opps = tw.opportunities()
    live = {o["id"]: o for o in opps if watched(o, cfg)}

    # Work out what changed before resolving any names.
    changes = []          # (kind, opp, old_stage)
    waiting = 0
    for oid, opp in live.items():
        prev = state.get(oid)
        if prev is None:
            # First sighting: record it, announce nothing. The row may be a
            # half-filled form someone has only just opened.
            state[oid] = {"stage": opp.get("stage"), "name": opp.get("name"),
                          "pipeline": opp.get("pipeline"),
                          "announced": bool(seed),
                          "submitted": bool(opp.get(cfg.get("submitted_field")
                                                    or "submitted")),
                          "first_seen": now_local(cfg).isoformat(timespec="seconds"),
                          "updatedAt": opp.get("updatedAt")}
            if not seed:
                waiting += 1
            continue
        field = cfg.get("submitted_field") or "submitted"
        if not prev.get("announced", True):
            ready, reason = is_submitted(opp, cfg)
            if ready and cfg.get("notify_new"):
                changes.append(("new", opp, None))
            elif not ready:
                waiting += 1
                log(f"holding {deal_title(opp)}: {reason}")
            continue
        # Announced already: a deal that was un-submitted and submitted again
        # is a deliberate resubmit, and only then does it go out a second time.
        if prev.get("submitted") and not opp.get(field):
            state[oid] = dict(prev, announced=False, submitted=False)
            log(f"{deal_title(opp)} un-submitted; back to draft")
            continue
        if prev.get("stage") != opp.get("stage"):
            changes.append(("stage", opp, prev.get("stage")))

    # A stage nobody has seen before means the options were edited in the UI.
    if any(c[1].get("stage") not in STAGE_ORDER for c in changes):
        if stages_module.refresh():
            log("stage options changed in Twenty; mappings refreshed")

    ctx = build_ctx([c[1] for c in changes], caches)
    burst = int(cfg.get("burst_threshold") or 0)
    stage_moves = [c for c in changes if c[0] == "stage"]
    is_burst = bool(burst and len(stage_moves) > burst)

    pending = []
    for kind, opp, old in changes:
        stage = opp.get("stage")
        if kind == "new":
            record_event("new", opp, None, stage, ctx, cfg)
            pending.append(("new", new_deal_msg(cfg, opp, ctx)))
        else:
            record_event("stage", opp, old, stage, ctx, cfg, bulk=is_burst)
            if not is_burst:
                pending.append(("stage", stage_change_msg(cfg, opp, old, stage, ctx)))

    announced_now = {c[1]["id"] for c in changes if c[0] == "new"}
    for oid, opp in live.items():
        prev = state.get(oid) or {}
        state[oid] = {"stage": opp.get("stage"), "name": deal_title(opp, ctx),
                      "pipeline": opp.get("pipeline"),
                      "owner": member(ctx, opp.get("ownerId")) or opp.get("hubspotOwner"),
                      "announced": bool(seed or prev.get("announced", True)
                                        or oid in announced_now),
                      "submitted": bool(opp.get(cfg.get("submitted_field")
                                                or "submitted")),
                      "first_seen": prev.get("first_seen"),
                      "updatedAt": opp.get("updatedAt")}

    for oid in [i for i in state if i not in live]:
        prev = state.pop(oid)
        if not seed and cfg.get("notify_removed") and prev.get("announced", True):
            record_event("removed", {"id": oid, "name": prev.get("name"),
                                     "pipeline": prev.get("pipeline")},
                         prev.get("stage"), None, ctx, cfg)
            pending.append(("removed", removed_msg(cfg, prev, ctx)))

    save_json(STATE_FILE, state)
    if seed:
        log(f"seeded {len(state)} deals; notifying from the next poll")
        poll_appointments(cfg, tg, seed=True)
        return 0
    poll_appointments(cfg, tg)
    if waiting:
        log(f"{waiting} new deal(s) not submitted yet; holding")
    if pending and in_quiet_hours(cfg):
        log(f"{len(pending)} events held by quiet hours")
        return 0
    if is_burst:
        # The flood is still suppressed and the events are still tagged in the
        # history; it just goes out silently unless burst_notify is turned on.
        if cfg.get("burst_notify"):
            send(tg, burst_msg(cfg, stage_moves))
        log(f"burst: {len(stage_moves)} stage changes swallowed "
            f"(pipeline edit, not rep activity)")

    for kind, msg in pending:
        send(tg, msg)
        log(f"sent {kind}: {msg.splitlines()[0][:70]}")
    return len(pending)


# ------------------------------------------------------------------ commands
COMMANDS = [
    ("daily", "Today's stage changes and closed business"),
    ("weekly", "Last 7 days"),
    ("monthly", "Last 30 days"),
    ("pipeline", "Deal counts and value by stage now"),
]
HELP = ("<b>Twenty deal bot</b>\n"
        "I ping this chat on every E&amp;S deal stage change.\n\n"
        + "\n".join(f"/{c} — {d}" for c, d in COMMANDS))


def cmd_pipeline(cfg, caches):
    opps = [o for o in tw.opportunities() if watched(o, cfg)]
    counts, totals = {}, {}
    for o in opps:
        s = o.get("stage")
        counts[s] = counts.get(s, 0) + 1
        totals[s] = totals.get(s, 0.0) + (amount_value(o.get("amount")) or 0.0)
    # Every stage is listed, empty ones included, so the shape of the pipeline
    # is always visible.
    order = list(STAGE_ORDER) + [s for s in counts if s not in STAGE_ORDER]
    lines = [f"<b>Pipeline now</b> — {len(opps)} deals"]
    for s in order:
        lines.append(f"{STAGE_EMOJI.get(s, BULLET)} {stage_label(s)}: "
                     f"<b>{counts.get(s, 0)}</b> · {fmt_money(totals.get(s) or 0)}")
    open_val = sum(v for k, v in totals.items() if k != "CLOSED_WON")
    lines.append(f"Open pipeline value: <b>{fmt_money(open_val)}</b>")

    lines.append("")
    lines.append(appointments_line(cfg))
    return "\n".join(lines)


def appointments_line(cfg):
    """Appointed agencies: the running total, and how many this week and month."""
    try:
        appointed = tw.find_many("companies", {"filter": "appointed[eq]:true"},
                                 page_size=60, max_pages=40)
    except tw.TwentyError as e:
        return f"\U0001f91d <b>Appointments</b>: lookup failed ({html.escape(str(e))})"

    recent = {7: 0, 30: 0}
    cutoffs = {d: now_local(cfg) - timedelta(days=d) for d in recent}
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("kind") != "appointed":
                    continue
                ts = parse_iso(ev.get("ts"))
                if not ts:
                    continue
                for d, cutoff in cutoffs.items():
                    if ts >= cutoff:
                        recent[d] += 1

    out = f"\U0001f91d <b>Appointed agencies: {len(appointed)}</b>"
    if any(recent.values()):
        out += f"\n   {recent[7]} in the last 7 days \u00b7 {recent[30]} in the last 30"

    return out


def handle_command(text, chat_id, cfg, tg, state, caches, thread_id=None):
    import reports
    cmd = text.strip().split()[0].lower().split("@")[0]
    try:
        if cmd in ("/start", "/help"):
            send(tg, HELP, chat_id, thread_id)
        elif cmd in ("/daily", "/today"):
            send(tg, reports.report(cfg, "daily", caches), chat_id, thread_id)
        elif cmd in ("/weekly", "/week"):
            send(tg, reports.report(cfg, "weekly", caches), chat_id, thread_id)
        elif cmd in ("/monthly", "/month"):
            send(tg, reports.report(cfg, "monthly", caches), chat_id, thread_id)
        elif cmd == "/pipeline":
            send(tg, cmd_pipeline(cfg, caches), chat_id, thread_id)
        elif cmd == "/chatid":          # undocumented, for adding a group
            send(tg, f"Chat id: <code>{chat_id}</code>"
                 + (f" · topic <code>{thread_id}</code>" if thread_id else ""),
                 chat_id, thread_id)
        else:
            return
        log(f"answered {cmd} for {chat_id}")
    except tw.TwentyError as e:
        send(tg, f"Twenty API error: {html.escape(str(e))}", chat_id, thread_id)


def drain_commands(cfg, tg, state, caches, wait=0):
    """Answer queued Telegram commands. The offset is persisted so a `once`
    run never replays a command it has already answered."""
    offset = load_json(OFFSET_FILE, {}).get("offset", 0)
    allowed = set(chat_ids(tg))
    d = tg_api("getUpdates", tg, {"offset": offset, "timeout": wait}, timeout=wait + 25)
    for u in (d or {}).get("result", []) or []:
        offset = u["update_id"] + 1
        msg = u.get("message") or u.get("channel_post") or {}
        text = msg.get("text") or ""
        chat = str((msg.get("chat") or {}).get("id") or "")
        thread = msg.get("message_thread_id")  # set in forum-group topics
        if not text.startswith("/"):
            continue
        cmd = text.split()[0].lower().split("@")[0]
        owner = chat == str(tg.get("chat_id"))
        if cmd == "/pin" and owner:
            # Owner approves a /here request: /pin <chat_id> [topic_id]
            parts = text.split()
            if len(parts) < 2:
                send(tg, "Usage: <code>/pin &lt;chat_id&gt; [topic_id]</code>", chat)
                continue
            set_home(tg, parts[1], parts[2] if len(parts) > 2 else None)
            allowed = set(chat_ids(tg))
            send(tg, "\U0001F4CC Pinned. Deal pings now post only in that "
                 + ("topic." if len(parts) > 2 else "chat."), chat)
            send(tg, "\U0001F4CC Deal pings will post here from now on.",
                 parts[1], parts[2] if len(parts) > 2 else None)
            continue
        if cmd == "/unpin" and owner:
            data = load_json(TG_FILE, {})
            data.pop("home", None)
            save_json(TG_FILE, data)
            os.chmod(TG_FILE, 0o600)
            tg.pop("home", None)
            send(tg, "Unpinned. Pings go back to every listed chat.", chat)
            continue
        if cmd == "/here":
            if chat in allowed:
                set_home(tg, chat, thread)
                allowed = set(chat_ids(tg))
                send(tg, "\U0001F4CC Pinned to this " + ("topic" if thread else "chat")
                     + ". Deal pings post here, and I'll ignore other topics.",
                     chat, thread)
            else:
                # Unknown group: ask the owner rather than leak deals to it.
                title = (msg.get("chat") or {}).get("title") or chat
                send(tg, f"\U0001F512 <b>{html.escape(title)}</b> asked for deal pings"
                     + (f" in topic {thread}" if thread else "")
                     + f".\nReply <code>/pin {chat}" + (f" {thread}" if thread else "")
                     + "</code> to approve.")
                send(tg, "Asked the owner to approve this topic.", chat, thread)
            continue
        if wrong_topic(tg, chat, thread):
            continue
        if allowed and chat not in allowed:
            log(f"ignoring command from unlisted chat {chat}")
            tg_api("sendMessage", tg, {"chat_id": chat,
                                       "text": f"This chat ({chat}) isn't on the allow list."})
            continue
        handle_command(text, chat, cfg, tg, state, caches, thread)
    save_json(OFFSET_FILE, {"offset": offset})


# ---------------------------------------------------------------------- main
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    preflight()
    cfg = load_config()
    tg = tg_config()
    state = load_json(STATE_FILE, {})
    caches = ({}, {})

    if mode == "seed":
        poll_once(cfg, tg, state, caches, seed=True)
        return

    if mode == "report":
        import reports
        send(tg, reports.report(cfg, sys.argv[2] if len(sys.argv) > 2 else "daily", caches))
        return

    if mode == "loop":
        # GitHub Actions cannot schedule more often than every 5 minutes, but a
        # single run can keep polling while it is alive. One run per cron tick
        # polls every `poll_seconds` for RUN_SECONDS and then exits, which gets
        # the delay down from ~5 minutes to the poll interval.
        budget = int(os.environ.get("RUN_SECONDS") or 240)
        deadline = time.time() + budget
        seed = not state
        polls = 0
        log(f"loop: {budget}s budget, polling every {cfg['poll_seconds']}s")
        while time.time() < deadline:
            started = time.time()
            try:
                poll_once(cfg, tg, state, caches, seed=seed)
                seed = False
                polls += 1
            except tw.TwentyError as e:
                log(f"poll failed: {e}")
            except Exception:
                log("poll crashed:\n" + traceback.format_exc())
            left = deadline - time.time()
            if left <= 0:
                break
            # The Telegram long poll doubles as the wait between polls.
            wait = max(1, min(int(cfg["poll_seconds"] - (time.time() - started)),
                              int(left)))
            try:
                drain_commands(cfg, tg, state, caches, wait=wait)
            except Exception:
                log("command loop error:\n" + traceback.format_exc())
                time.sleep(min(5, max(1, left)))
        log(f"loop done: {polls} polls")
        return

    if mode == "once":
        try:
            poll_once(cfg, tg, state, caches, seed=not state)
        except tw.TwentyError as e:
            log(f"poll failed: {e}")
        drain_commands(cfg, tg, state, caches)
        return

    log(f"serve: watching {cfg.get('pipelines') or 'all pipelines'}, "
        f"every {cfg['poll_seconds']}s, {len(state)} deals in state")
    seed = not state
    next_poll = 0.0
    while True:
        try:
            if time.time() >= next_poll:
                poll_once(cfg, tg, state, caches, seed=seed)
                seed = False
                next_poll = time.time() + cfg["poll_seconds"]
                cfg = load_config()
        except tw.TwentyError as e:
            log(f"poll failed: {e}")
            next_poll = time.time() + max(cfg["poll_seconds"], 120)
        except Exception:
            log("poll crashed:\n" + traceback.format_exc())
            next_poll = time.time() + max(cfg["poll_seconds"], 120)
        try:
            drain_commands(cfg, tg, state, caches,
                           wait=max(1, min(25, int(next_poll - time.time()))))
        except Exception:
            log("command loop error:\n" + traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    main()
