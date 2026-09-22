#!/usr/bin/env python3
"""Stage and pipeline mappings for the Twenty opportunity object.

Labels and ordering are read live from Twenty's metadata API (so renaming a
stage or adding one in the UI is picked up on the next run) and cached to
state/stages.json. The constants below are the fallback when the metadata call
fails, and they mirror the live pipeline as of 2026-09-22:

    MEETING_BOOKED  Meeting Booked
    QUOTE_RECEIVED  Quote Received
    QUOTE_SENT      Quote Sent
    PRODUCER_AGREEMENT_SIGNED  Agreement Signed
    CLOSED_WON      Closed Won
"""
import json, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "state", "stages.json")
OPPORTUNITY_OBJECT_ID = "f790657a-3427-473b-8926-50333080a2c1"
CACHE_TTL = 6 * 3600

FALLBACK_STAGES = [
    ("MEETING_BOOKED", "Meeting Booked"),
    ("QUOTE_RECEIVED", "Quote Received"),
    ("QUOTE_SENT", "Quote Sent"),
    ("PRODUCER_AGREEMENT_SIGNED", "Agreement Signed"),
    ("CLOSED_WON", "Closed Won"),
]
FALLBACK_PIPELINES = [
    ("DIRECT_SOLD", "Direct Sold"),
    ("BROKER_SOLD", "Broker Sold"),
    ("ES_CARRIER", "E&S Carrier"),
]
# One emoji per stage, so a glance at Telegram says where the deal got to.
STAGE_EMOJI = {
    "MEETING_BOOKED": "\U0001f3af",              # target
    "QUOTE_RECEIVED": "\U0001f4e5",              # inbox tray
    "QUOTE_SENT": "\U0001f4e4",                  # outbox tray
    "PRODUCER_AGREEMENT_SIGNED": "✍️",  # writing hand
    "CLOSED_WON": "\U0001f4b0",                  # money bag
    "CLOSED_LOST": "❌",
}


def _from_metadata():
    """Pull the live SELECT options; returns (stages, pipelines) or None."""
    try:
        import twenty_api as tw
        d = tw.get("/metadata/fields",
                   {"limit": 500,
                    "filter": f"objectMetadataId[eq]:{OPPORTUNITY_OBJECT_ID}"})
    except Exception:
        return None
    out = {}
    for f in d.get("data", []) or []:
        if f.get("name") in ("stage", "pipeline") and f.get("options"):
            out[f["name"]] = [(o["value"], o["label"]) for o in
                              sorted(f["options"], key=lambda x: x.get("position", 0))]
    if "stage" not in out:
        return None
    return out.get("stage"), out.get("pipeline") or FALLBACK_PIPELINES


def _load():
    cached = None
    try:
        cached = json.load(open(CACHE))
    except (OSError, ValueError):
        pass
    if cached and time.time() - cached.get("fetched", 0) < CACHE_TTL:
        return [tuple(x) for x in cached["stages"]], [tuple(x) for x in cached["pipelines"]]
    live = _from_metadata()
    if live:
        stages, pipelines = live
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            json.dump({"fetched": time.time(), "stages": stages, "pipelines": pipelines},
                      open(CACHE, "w"), indent=1)
        except OSError:
            pass
        return stages, pipelines
    if cached:                       # stale cache beats no cache
        return [tuple(x) for x in cached["stages"]], [tuple(x) for x in cached["pipelines"]]
    return FALLBACK_STAGES, FALLBACK_PIPELINES


_STAGES, _PIPELINES = _load()

STAGE_ORDER = [v for v, _ in _STAGES]
STAGE_LABELS = dict(_STAGES)
PIPELINE_LABELS = dict(_PIPELINES)


def stage_label(value):
    if not value:
        return "No stage"
    return STAGE_LABELS.get(value, value.replace("_", " ").title())


def pipeline_label(value):
    if not value:
        return "No pipeline"
    return PIPELINE_LABELS.get(value, value.replace("_", " ").title())


def stage_rank(value):
    """Position in the funnel; unknown stages sort last."""
    try:
        return STAGE_ORDER.index(value)
    except ValueError:
        return len(STAGE_ORDER)
