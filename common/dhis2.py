"""DHIS2 access helpers shared by every script in this repository.

Everything here exists to close a class of defect found in the August 2026
audit, so please route new scripts through these functions rather than
hand-rolling the same request again:

  * Credentials come from the environment only. Nothing is ever hardcoded.
  * Every paging loop raises on an HTTP error instead of returning an empty
    list that reads as "no work to do".
  * Every write returns the server's import stats, so a caller can tell a
    successful import from a rejected one before it checkpoints progress.
  * Metadata creation verifies that the objects it asked for actually exist
    before handing back UIDs, so a stage can never be built from None.
"""

import hashlib
import json
import os
import sys
import time

import requests

from common import dotenv

RETRY_STATUS_CODES = {429, 502, 503, 504}
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 3
DEFAULT_TIMEOUT = 120
DEFAULT_PAGE_SIZE = 500


class Dhis2Error(RuntimeError):
    """Raised when DHIS2 reports a failure that the caller must not ignore."""


# Object names on the server mix dash characters: "Wearable – Heart Rate" uses
# an en dash, while "Cardiac – 12-Lead ECG" uses an en dash AND a hyphen in the
# same name. Normalise every dash to one character on both sides before
# comparing, rather than rewriting one into the other, which would also rewrite
# the hyphen in "12-Lead".
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"


def normalise_name(name):
    """Dash- and case-insensitive form of a metadata name, for comparisons."""
    return " ".join("".join("-" if ch in _DASHES else ch for ch in str(name)).split()).casefold()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def base_url():
    dotenv.load_once()
    url = os.environ.get("DHIS2_URL")
    if not url:
        sys.exit(
            "ERROR: DHIS2_URL is not set.\n"
            "  Put it in .env at the repository root. It is read automatically.\n  cp .env.example .env  and fill it in. No shell sourcing needed."
        )
    return url.rstrip("/")


def api_url():
    return base_url() + "/api"


def get_session(read_only=False):
    """Build an authenticated session.

    read_only=True prefers the auditor account, which the verification
    scripts should use so that a mistake there cannot write anything.
    """
    dotenv.load_once()
    if read_only:
        user = os.environ.get("DHIS2_AUDITOR_USER") or os.environ.get("DHIS2_USERNAME")
        password = os.environ.get("DHIS2_AUDITOR_PASS") or os.environ.get("DHIS2_PASSWORD")
        names = "DHIS2_AUDITOR_USER / DHIS2_AUDITOR_PASS (or DHIS2_USERNAME / DHIS2_PASSWORD)"
    else:
        user = os.environ.get("DHIS2_USERNAME")
        password = os.environ.get("DHIS2_PASSWORD")
        names = "DHIS2_USERNAME / DHIS2_PASSWORD"

    if not user or not password:
        sys.exit(
            f"ERROR: {names} are not set.\n"
            "  Credentials must come from the environment. Never hardcode them.\n"
            "  Put it in .env at the repository root. It is read automatically.\n  cp .env.example .env  and fill it in. No shell sourcing needed."
        )

    session = requests.Session()
    session.auth = (user, password)
    session.headers.update({"Accept": "application/json"})
    return session


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def request(session, method, url, **kwargs):
    """Perform one request, retrying transient failures, raising on the rest.

    Never returns a response the caller has to re-check: anything at or above
    400 that is not retryable raises.

    A retry after a 502/504 or a timeout re-sends a write the server may
    already have committed. That is only safe because every import in this
    repository now carries client-supplied UIDs and uses CREATE_AND_UPDATE,
    so the second attempt updates rather than duplicates. Do not add a write
    path that relies on this retry without deterministic UIDs.
    """
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            last = exc
            if attempt == MAX_RETRIES:
                raise Dhis2Error(f"{method} {url} failed after {MAX_RETRIES} attempts: {exc}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code < 400:
            return resp
        if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        raise Dhis2Error(
            f"{method} {url} returned {resp.status_code}: {resp.text[:800]}"
        )
    raise Dhis2Error(f"{method} {url} failed after {MAX_RETRIES} attempts: {last}")


def get_json(session, path, params=None):
    """GET an /api path and return parsed JSON. Raises on any HTTP error."""
    url = path if path.startswith("http") else f"{api_url()}/{path.lstrip('/')}"
    return request(session, "GET", url, params=params).json()


def extract_items(payload, *candidate_keys):
    """Pull the result list out of a tracker response.

    The key varies by DHIS2 version ('trackedEntities' vs 'instances'), so
    check every plausible name rather than assuming one.
    """
    for key in candidate_keys:
        if key in payload:
            return payload[key]
    return []


def fetch_all_pages(session, path, params, item_keys, page_size=DEFAULT_PAGE_SIZE):
    """Fetch every page of a collection endpoint.

    Stops only on an EMPTY page. The previous version also stopped on a short
    page, which silently lost rows whenever the server capped pageSize below
    what was asked for. One extra request per fetch is the price of knowing
    the collection is exhausted.

    Tracker endpoints are given a stable sort order when the caller does not
    supply one, so the server cannot reorder rows between pages and let the
    same row appear twice while another never appears.

    Raises on any HTTP error rather than returning [] and letting the caller
    mistake failure for emptiness.
    """
    out = []
    page = 1
    while True:
        merged = dict(params or {})
        if path.lstrip("/").startswith("tracker/") and "order" not in merged:
            merged["order"] = "createdAt:asc"
        merged.update({"page": page, "pageSize": page_size})
        batch = extract_items(get_json(session, path, merged), *item_keys)
        if not batch:
            break
        out.extend(batch)
        page += 1
    return out


# ---------------------------------------------------------------------------
# Deterministic UIDs
# ---------------------------------------------------------------------------

_UID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generate_uid(*parts):
    """A valid DHIS2 UID (11 chars, letter first) derived from the parts.

    The same parts always give the same UID, which is what makes a re-run
    safe: a tracker CREATE_AND_UPDATE with a client-supplied UID updates the
    row that is already there instead of creating a second one. Without this,
    every retry after a proxy timeout, every resumed run, and every
    --person-id re-run created duplicate events, because CREATE carries no
    identity the server could match on.

    Parts are joined with a separator that cannot appear in any of them, so
    ("a", "bc") and ("ab", "c") do not collide.
    """
    key = "\x1f".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha256(key).digest()
    first = _UID_ALPHABET[digest[0] % 52]  # letters only
    rest = "".join(_UID_ALPHABET[b % 62] for b in digest[1:11])
    return first + rest


def assign_event_uids(events, key_fn):
    """Give every event a deterministic UID from key_fn(event).

    Two events with the same key (for example two identical sleep segments
    in the source file) get distinct UIDs by sequence number, so nothing is
    dropped and the result is still stable across runs as long as the source
    order is. Mutates and returns the list.
    """
    seen = {}
    for event in events:
        key = tuple(key_fn(event))
        n = seen.get(key, 0)
        seen[key] = n + 1
        event["event"] = generate_uid(*key, n)
    return events


def check_unique_uids(items, kind):
    """Raise if any client-supplied UID appears twice in one payload."""
    counts = {}
    for item in items:
        uid = item.get(kind)
        if uid:
            counts[uid] = counts.get(uid, 0) + 1
    dupes = [u for u, c in counts.items() if c > 1]
    if dupes:
        raise Dhis2Error(
            f"{len(dupes)} duplicate {kind} UID(s) in one payload, e.g. {dupes[:3]}. "
            f"The server would reject or merge these, so refusing to send."
        )


# ---------------------------------------------------------------------------
# Tracker writes
# ---------------------------------------------------------------------------

TRACKER_TYPES = ("TRACKED_ENTITY", "ENROLLMENT", "EVENT", "RELATIONSHIP")


def _stats_of(payload, tracker_type=None):
    """Import stats, for one object type when asked.

    The top-level stats on a tracker import report SUM every object type.
    A payload of 100 tracked entities, each with an enrollment and three
    events, reports created=500. Checking that against len(batch) made
    send_to_dhis2.py mark every successful batch as failed. The per-type
    numbers live in bundleReport.typeReportMap.<TYPE>.stats, which the
    server only includes with reportMode=FULL.
    """
    if tracker_type:
        type_map = (payload.get("bundleReport") or {}).get("typeReportMap") or {}
        report = type_map.get(tracker_type)
        if report and isinstance(report.get("stats"), dict):
            stats = report["stats"]
            return {k: stats.get(k, 0) for k in ("created", "updated", "deleted", "ignored")}
        return None
    stats = payload.get("stats") or payload.get("response", {}).get("stats") or {}
    return {k: stats.get(k, 0) for k in ("created", "updated", "deleted", "ignored")}


def import_tracker(session, payload, strategy, expect=None, expect_count=None,
                   tracker_type=None):
    """POST to /api/tracker and verify the server actually accepted the work.

    expect is "created", "updated" or "upserted" (created + updated, for
    CREATE_AND_UPDATE). tracker_type names which object type the expectation
    is about: "TRACKED_ENTITY" for a participant payload, "EVENT" for events.
    Without it the expectation is checked against the summed top-level stats,
    which is only right when the payload holds a single object type with no
    nested children.

    Returns the stats dict for tracker_type (or the top-level one). Raises
    Dhis2Error when the import reported an error status, ignored anything,
    or fell short of expect_count. Callers must not record progress unless
    this returns.
    """
    resp = request(
        session, "POST", f"{api_url()}/tracker",
        params={"importStrategy": strategy, "async": "false", "reportMode": "FULL"},
        json=payload,
    )
    try:
        result = resp.json()
    except ValueError:
        raise Dhis2Error(f"tracker import returned non-JSON: {resp.text[:500]}")

    status = result.get("status", "UNKNOWN")
    total = _stats_of(result)
    stats = _stats_of(result, tracker_type) if tracker_type else total

    if status not in ("OK", "SUCCESS") or total["ignored"]:
        raise Dhis2Error(
            f"tracker {strategy} reported status={status} stats={total}\n"
            f"{_first_conflicts(result)}"
        )

    if stats is None:
        # Server did not return a per-type breakdown. Do not fail a batch the
        # server committed, but do not claim an exact count either.
        print(f"  [warn] no per-type stats for {tracker_type} in the import "
              f"report; verified status and ignored=0 only. totals={total}")
        return total

    if expect and expect_count is not None:
        got = (stats["created"] + stats["updated"]) if expect == "upserted" else stats[expect]
        if got != expect_count:
            raise Dhis2Error(
                f"tracker {strategy} expected {expect}={expect_count} "
                f"for {tracker_type or 'all types'} but got {stats}"
            )
    return stats


def _first_conflicts(result, limit=5):
    """Pull the first few validation messages out of an import report."""
    msgs = []
    for report in result.get("validationReport", {}).get("errorReports", [])[:limit]:
        msgs.append(f"  {report.get('errorCode', '')} {report.get('message', '')}")
    for bundle in result.get("bundleReport", {}).get("typeReportMap", {}).values():
        for obj in bundle.get("objectReports", [])[:limit]:
            for err in obj.get("errorReports", [])[:limit]:
                msgs.append(f"  {err.get('errorCode', '')} {err.get('message', '')}")
    if not msgs:
        return "  " + json.dumps(result)[:600]
    return "\n".join(msgs[:limit])


def _expectation_for(strategy):
    return {"CREATE": "created", "UPDATE": "updated",
            "CREATE_AND_UPDATE": "upserted"}.get(strategy, "upserted")


def send_events(session, events, strategy, batch_size=200, on_batch=None):
    """Send events in batches, verifying each one.

    For CREATE_AND_UPDATE every event must carry a client-supplied "event"
    UID (see assign_event_uids); that is the only thing that makes the
    strategy idempotent. Returns the summed stats. Raises on the first batch
    the server refuses, so a caller cannot checkpoint a participant whose
    data was rejected.
    """
    if strategy == "CREATE_AND_UPDATE":
        missing = [e for e in events if not e.get("event")]
        if missing:
            raise Dhis2Error(
                f"{len(missing)} event(s) have no client UID. CREATE_AND_UPDATE "
                f"without one is just CREATE and duplicates on re-run. Use "
                f"dhis2.assign_event_uids first."
            )
    check_unique_uids(events, "event")

    totals = {"created": 0, "updated": 0, "deleted": 0, "ignored": 0}
    expect = _expectation_for(strategy)
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        stats = import_tracker(
            session, {"events": batch}, strategy,
            expect=expect, expect_count=len(batch), tracker_type="EVENT",
        )
        for k in totals:
            totals[k] += stats[k]
        if on_batch:
            on_batch(i, len(batch), stats)
    return totals


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def post_metadata(session, payload):
    """POST /api/metadata and fail loudly on a soft error.

    DHIS2 can answer 200 with status ERROR, so the status code alone is not
    evidence that anything was created.
    """
    resp = request(
        session, "POST", f"{api_url()}/metadata",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
    )
    result = resp.json()
    status = result.get("status", "UNKNOWN")
    stats = result.get("stats", {})
    if status not in ("OK", "SUCCESS") or stats.get("ignored", 0):
        raise Dhis2Error(
            f"metadata import reported status={status} stats={stats}\n"
            f"{_first_conflicts(result)}"
        )
    return result


def lookup_by_name(session, resource, names, program_uid=None):
    """Resolve {name: uid} for a resource.

    Exact name:eq first, one request per name (not name:in:[a,b,c], which
    breaks silently on a comma). Anything still unresolved is then matched
    dash- and case-insensitively against the full list of names, because
    this server holds "Cardiac – 12-Lead ECG" with an en dash while the
    script that created it spells the hyphen. The exact match alone made
    step 1 non-idempotent: every re-run created a second, empty stage.

    A name that matches more than one object after normalisation raises,
    since picking one would silently send data to the wrong stage.
    """
    found = {}
    for name in names:
        items = get_json(
            session, resource,
            {"filter": f"name:eq:{name}", "fields": "id,name", "paging": "false"},
        ).get(resource, [])
        if len(items) > 1:
            raise Dhis2Error(
                f"{len(items)} {resource} are named exactly '{name}': "
                f"{[i['id'] for i in items]}. Delete the duplicate before continuing."
            )
        if items:
            found[name] = items[0]["id"]

    missing = [n for n in names if n not in found]
    if not missing:
        return found

    params = {"fields": "id,name", "paging": "false"}
    if program_uid and resource == "programStages":
        params["filter"] = f"program.id:eq:{program_uid}"
    everything = get_json(session, resource, params).get(resource, [])
    by_key = {}
    for item in everything:
        by_key.setdefault(normalise_name(item["name"]), []).append(item)

    for name in missing:
        matches = by_key.get(normalise_name(name), [])
        if len(matches) > 1:
            raise Dhis2Error(
                f"'{name}' matches {len(matches)} {resource} once dashes and case "
                f"are ignored: {[(m['name'], m['id']) for m in matches]}. "
                f"Delete the duplicate before continuing."
            )
        if matches:
            found[name] = matches[0]["id"]
    return found


def require_all(found, names, what):
    """Raise unless every requested name resolved to a UID."""
    missing = [n for n in names if not found.get(n)]
    if missing:
        raise Dhis2Error(
            f"{len(missing)} {what} could not be resolved after creation. "
            f"Refusing to continue with null UIDs.\n  missing: {missing}"
        )
    return found


def create_data_elements(session, defs):
    """Create data elements idempotently and return a verified {name: uid}.

    defs: iterable of dicts with at least name, valueType. Anything already
    present is reused rather than duplicated.
    """
    defs = list(defs)
    names = [d["name"] for d in defs]
    existing = lookup_by_name(session, "dataElements", names)

    to_create = []
    for d in defs:
        if d["name"] in existing:
            continue
        payload = {
            "name": d["name"],
            "shortName": d.get("shortName", d["name"])[:50],
            "domainType": d.get("domainType", "TRACKER"),
            "valueType": d["valueType"],
            "aggregationType": d.get("aggregationType", "NONE"),
        }
        if d.get("optionSet"):
            payload["optionSet"] = {"id": d["optionSet"]}
        to_create.append(payload)

    if to_create:
        post_metadata(session, {"dataElements": to_create})

    resolved = lookup_by_name(session, "dataElements", names)
    return require_all(resolved, names, "data elements")


def option_code(name):
    """Derive an option code from its display name.

    Used only when CREATING an option that does not exist yet. To write a
    value, always resolve the real code with option_value: this server's
    codes were generated by an earlier version of these scripts and a fresh
    derivation is not guaranteed to reproduce them.
    """
    out = []
    for ch in name.upper():
        out.append(ch if ch.isalnum() else "_")
    code = "".join(out)
    while "__" in code:
        code = code.replace("__", "_")
    return code.strip("_")


def create_option_set(session, name, options):
    """Create an option set with its options nested, idempotently.

    Options are created inside the option set rather than as standalone
    objects resolved by a global name lookup. Verified against DHIS2 2.44:
    two option sets can hold options with the same name and the same code as
    separate objects, so plain codes are safe and match what this instance
    already uses.
    """
    existing = lookup_by_name(session, "optionSets", [name])
    if existing.get(name):
        return existing[name]

    payload = {
        "optionSets": [{
            "name": name,
            "code": option_code(name),
            "valueType": "TEXT",
            "options": [
                {"name": opt, "code": option_code(opt), "sortOrder": i + 1}
                for i, opt in enumerate(options)
            ],
        }]
    }
    post_metadata(session, payload)

    resolved = lookup_by_name(session, "optionSets", [name])
    require_all(resolved, [name], "option sets")

    uid = resolved[name]
    stored = get_json(session, f"optionSets/{uid}", {"fields": "options[id,name,code]"})
    if len(stored.get("options", [])) != len(options):
        raise Dhis2Error(
            f"option set '{name}' has {len(stored.get('options', []))} options, "
            f"expected {len(options)}. Refusing to attach it to a data element."
        )
    return uid


_REGISTRY_CACHE = {}


def option_value(option_set_name, display_name):
    """The code to store for an option, as the server defines it.

    Resolved from the cached metadata rather than derived, because a derived
    code is only correct if the option was created by the current version of
    these scripts. Falls back to the plain derivation when no cache exists,
    which is the fresh-install case.

    The registry is loaded once per process. This used to re-read and
    re-parse the cache file on every call, which is once per event.
    """
    from common import metadata_uids

    if "registry" not in _REGISTRY_CACHE:
        try:
            _REGISTRY_CACHE["registry"] = metadata_uids.load()
        except (KeyError, OSError, SystemExit):
            _REGISTRY_CACHE["registry"] = None
    registry = _REGISTRY_CACHE["registry"]
    if registry is None:
        return option_code(display_name)
    try:
        return registry.option_value(option_set_name, display_name)
    except KeyError:
        return option_code(display_name)


def attach_data_elements(session, stage_uid, de_uids):
    """Append data elements to an existing program stage.

    Uses fields=:owner so the round trip carries every persisted property.
    A bare fields=* can omit associations, and PUT replaces the object, so
    the omitted ones would be dropped.
    """
    de_uids = [u for u in de_uids if u]
    stage = get_json(session, f"programStages/{stage_uid}", {"fields": ":owner"})
    psde = stage.get("programStageDataElements", [])
    present = {p["dataElement"]["id"] for p in psde}
    max_sort = max((p.get("sortOrder") or 0) for p in psde) if psde else 0

    added = 0
    for uid in de_uids:
        if uid in present:
            continue
        max_sort += 1
        psde.append({
            "dataElement": {"id": uid},
            "compulsory": False,
            "sortOrder": max_sort,
        })
        present.add(uid)
        added += 1

    if not added:
        return 0

    stage["programStageDataElements"] = psde
    request(
        session, "PUT", f"{api_url()}/programStages/{stage_uid}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(stage),
    )
    return added


def create_program_stage(session, name, program_uid, de_uids, repeatable=True):
    """Create a program stage idempotently, refusing null data element UIDs."""
    existing = lookup_by_name(session, "programStages", [name], program_uid=program_uid)
    if existing.get(name):
        attach_data_elements(session, existing[name], de_uids)
        return existing[name]

    if any(u is None for u in de_uids):
        raise Dhis2Error(
            f"refusing to create stage '{name}': one or more data element UIDs are None"
        )

    post_metadata(session, {"programStages": [{
        "name": name,
        "program": {"id": program_uid},
        "repeatable": repeatable,
        "featureType": "NONE",
        "programStageDataElements": [
            {"dataElement": {"id": uid}, "compulsory": False, "sortOrder": i + 1}
            for i, uid in enumerate(de_uids)
        ],
    }]})

    resolved = lookup_by_name(session, "programStages", [name], program_uid=program_uid)
    return require_all(resolved, [name], "program stages")[name]


# ---------------------------------------------------------------------------
# Tracked entities and events
# ---------------------------------------------------------------------------

def get_tei_context(session, program_uid, person_id_attr, person_id):
    """Resolve one participant's tracked entity, org unit and enrollment."""
    items = extract_items(
        get_json(session, "tracker/trackedEntities", {
            "program": program_uid,
            "filter": f"{person_id_attr}:eq:{person_id}",
            "fields": "trackedEntity,orgUnit,enrollments[enrollment,orgUnit,status]",
        }),
        "trackedEntities", "instances",
    )
    if not items:
        return None
    if len(items) > 1:
        # Two participants with one person_id. Attaching to whichever the
        # server listed first would split a participant's data across two
        # records, which is worse than stopping.
        raise Dhis2Error(
            f"{len(items)} tracked entities carry person_id={person_id}: "
            f"{[t['trackedEntity'] for t in items]}. Merge or delete the "
            f"duplicate before importing anything for this participant."
        )
    tei = items[0]
    enrollments = tei.get("enrollments") or []
    active = next((e for e in enrollments if e.get("status") == "ACTIVE"), None)
    active = active or (enrollments[0] if enrollments else None)
    if not active:
        return None
    return {
        "trackedEntity": tei["trackedEntity"],
        "orgUnit": active.get("orgUnit") or tei.get("orgUnit"),
        "enrollment": active["enrollment"],
    }


EVENT_FIELDS = ("event,programStage,orgUnit,enrollment,trackedEntity,"
                "occurredAt,status,dataValues[dataElement,value]")


def fetch_events(session, program_uid, stage_uid, tei_uid=None, fields=EVENT_FIELDS):
    """Fetch every event for a stage, optionally scoped to one participant."""
    params = {"program": program_uid, "programStage": stage_uid, "fields": fields}
    if tei_uid:
        params["trackedEntity"] = tei_uid
    return fetch_all_pages(session, "tracker/events", params, ("events", "instances"))


def merge_data_values(event, updates):
    """Build the full dataValues list for an event update.

    Every value the event should keep is carried, so this is correct whether
    the server replaces the set on UPDATE or merges it. DHIS2 2.44 was
    verified to merge, which has a consequence for deletion: OMITTING an
    element does not clear it. The only way to remove a value is to send the
    element with an empty value, which is what None does here.

    updates: {dataElement uid: value}. A value of None clears that element.
    """
    merged = {dv["dataElement"]: dv.get("value") for dv in event.get("dataValues", [])}
    for uid, value in updates.items():
        if value is None:
            if uid in merged:
                merged[uid] = ""
        else:
            merged[uid] = str(value)
    return [{"dataElement": k, "value": v} for k, v in merged.items() if v is not None]


def event_update_payload(event, stage_uid, program_uid, data_values):
    """Full event context for a tracker UPDATE.

    DHIS2 rejects an update that omits any of program, programStage, orgUnit,
    enrollment, occurredAt or status, even when only one value is changing.
    """
    payload = {
        "event": event["event"],
        "program": program_uid,
        "programStage": stage_uid,
        "orgUnit": event["orgUnit"],
        "enrollment": event.get("enrollment"),
        "occurredAt": event["occurredAt"],
        "status": event.get("status", "COMPLETED"),
        "dataValues": data_values,
    }
    if event.get("trackedEntity"):
        payload["trackedEntity"] = event["trackedEntity"]
    return payload


def data_value(uid, value):
    """Build one dataValue, dropping anything that is not finite.

    str(float('nan')) is 'nan', which DHIS2 stores or rejects as garbage on a
    NUMBER element. See also common.numeric.is_finite_number.
    """
    from common.numeric import is_finite_number

    if uid is None or value is None:
        return None
    if isinstance(value, float) and not is_finite_number(value):
        return None
    return {"dataElement": uid, "value": str(value)}
