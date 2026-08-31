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

    Terminates on a short page as well as an empty one, so a server that
    ignores `page` cannot spin this forever. Raises on any HTTP error rather
    than returning [] and letting the caller mistake failure for emptiness.
    """
    out = []
    page = 1
    while True:
        merged = dict(params or {})
        merged.update({"page": page, "pageSize": page_size})
        batch = extract_items(get_json(session, path, merged), *item_keys)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    return out


# ---------------------------------------------------------------------------
# Tracker writes
# ---------------------------------------------------------------------------

def _stats_of(payload):
    stats = payload.get("stats") or payload.get("response", {}).get("stats") or {}
    return {k: stats.get(k, 0) for k in ("created", "updated", "deleted", "ignored")}


def import_tracker(session, payload, strategy, expect=None, expect_count=None):
    """POST to /api/tracker and verify the server actually accepted the work.

    Returns the stats dict. Raises Dhis2Error when the import reported an
    error status, ignored anything, or fell short of expect_count. Callers
    must not record progress unless this returns.
    """
    resp = request(
        session, "POST", f"{api_url()}/tracker",
        params={"importStrategy": strategy, "async": "false"},
        json=payload,
    )
    try:
        result = resp.json()
    except ValueError:
        raise Dhis2Error(f"tracker import returned non-JSON: {resp.text[:500]}")

    status = result.get("status", "UNKNOWN")
    stats = _stats_of(result)

    if status not in ("OK", "SUCCESS") or stats["ignored"]:
        raise Dhis2Error(
            f"tracker {strategy} reported status={status} stats={stats}\n"
            f"{_first_conflicts(result)}"
        )
    if expect and expect_count is not None and stats[expect] != expect_count:
        raise Dhis2Error(
            f"tracker {strategy} expected {expect}={expect_count} but got {stats}"
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


def send_events(session, events, strategy, batch_size=200, on_batch=None):
    """Send events in batches, verifying each one.

    Returns the summed stats. Raises on the first batch the server refuses,
    so a caller cannot checkpoint a participant whose data was rejected.
    """
    totals = {"created": 0, "updated": 0, "deleted": 0, "ignored": 0}
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        expect = "created" if strategy == "CREATE" else "updated"
        stats = import_tracker(
            session, {"events": batch}, strategy,
            expect=expect, expect_count=len(batch),
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


def lookup_by_name(session, resource, names):
    """Resolve {name: uid} for a resource, one request per name.

    Deliberately not a name:in:[a,b,c] filter: that breaks silently on any
    name containing a comma and gives no way to tell which entry is missing.
    """
    found = {}
    for name in names:
        items = get_json(
            session, resource,
            {"filter": f"name:eq:{name}", "fields": "id,name", "paging": "false"},
        ).get(resource, [])
        if items:
            found[name] = items[0]["id"]
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


def option_value(option_set_name, display_name):
    """The code to store for an option, as the server defines it.

    Resolved from the cached metadata rather than derived, because a derived
    code is only correct if the option was created by the current version of
    these scripts. Falls back to the plain derivation when no cache exists,
    which is the fresh-install case.
    """
    from common import metadata_uids

    try:
        return metadata_uids.load().option_value(option_set_name, display_name)
    except (KeyError, OSError, SystemExit):
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
    existing = lookup_by_name(session, "programStages", [name])
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

    resolved = lookup_by_name(session, "programStages", [name])
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

    A tracker UPDATE treats the submitted event as authoritative, so every
    value the event should keep has to be present in the payload. Sending
    only the changed value is what allowed the August 2026 retinal backfill
    to strip 42,054 events.

    updates: {dataElement uid: value}. A value of None removes that element.
    """
    merged = {dv["dataElement"]: dv.get("value") for dv in event.get("dataValues", [])}
    for uid, value in updates.items():
        if value is None:
            merged.pop(uid, None)
        else:
            merged[uid] = str(value)
    return [{"dataElement": k, "value": v} for k, v in merged.items() if v is not None]


def event_update_payload(event, stage_uid, program_uid, data_values):
    """Full event context for a tracker UPDATE.

    DHIS2 rejects an update that omits any of program, programStage, orgUnit,
    enrollment, occurredAt or status, even when only one value is changing.
    """
    return {
        "event": event["event"],
        "program": program_uid,
        "programStage": stage_uid,
        "orgUnit": event["orgUnit"],
        "enrollment": event.get("enrollment"),
        "occurredAt": event["occurredAt"],
        "status": event.get("status", "COMPLETED"),
        "dataValues": data_values,
    }


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
