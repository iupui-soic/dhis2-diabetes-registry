"""
Fixes the ECG Validation Date field, which was stored as raw '20241014'
instead of a proper '2024-10-14' date format. Converts every existing
event in place. Does not touch any other field.

Run:
    python3 ecg_validation_date_fix.py
(small dataset, ~2257 events - runs in well under a minute in the foreground)
"""

import requests
from datetime import datetime

DHIS2_URL = 'https://t2d-registry.plhi.us'
ADMIN_USER = 'admin'
ADMIN_PASS = 'REPLACE_ME'
PROGRAM_UID = 'W3LSFZH3UDq'

# From ecg_step1_metadata.py's original output:
STAGE_UID = 'xQjp0SgUbzv'
VALIDATION_DATE_DE = 'wweIn1KripN'

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASS)


def format_validation_date(raw):
    """Converts '20241014' -> '2024-10-14'. Returns raw value unchanged
    if it doesn't match the expected 8-digit format."""
    if raw and len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, '%Y%m%d').strftime('%Y-%m-%d')
        except ValueError:
            return raw
    return raw


def fetch_all_events():
    all_events = []
    page = 1
    while True:
        resp = session.get(
            f'{DHIS2_URL}/api/tracker/events',
            params={
                'program': PROGRAM_UID, 'programStage': STAGE_UID,
                'pageSize': 500, 'page': page,
                'fields': 'event,orgUnit,enrollment,trackedEntity,occurredAt,status,dataValues',
            },
        )
        events = resp.json().get('events', [])
        if not events:
            break
        all_events.extend(events)
        page += 1
    return all_events


def build_update(event):
    """Returns an update payload if the date needs fixing, else None."""
    data_values = list(event['dataValues'])
    changed = False

    for dv in data_values:
        if dv['dataElement'] == VALIDATION_DATE_DE:
            old_val = dv['value']
            new_val = format_validation_date(old_val)
            if new_val != old_val:
                dv['value'] = new_val
                changed = True

    if not changed:
        return None

    return {
        'event': event['event'],
        'program': PROGRAM_UID,
        'programStage': STAGE_UID,
        'orgUnit': event['orgUnit'],
        'enrollment': event.get('enrollment'),
        'trackedEntity': event.get('trackedEntity'),
        'occurredAt': event['occurredAt'],
        'status': event.get('status', 'COMPLETED'),
        'dataValues': data_values,
    }


def send_batch(events, batch_size=50):
    import time
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        resp = session.post(
            f'{DHIS2_URL}/api/tracker',
            params={'importStrategy': 'UPDATE', 'async': 'false'},
            json={'events': batch},
        )
        try:
            status = resp.json().get('status', 'UNKNOWN')
        except Exception:
            status = f"HTTP {resp.status_code}"
        if status not in ('OK', 'SUCCESS'):
            print(f"    batch error (items {i}-{i+len(batch)}): {resp.text[:300]}")
        else:
            print(f"    batch {i}-{i+len(batch)}: OK")
        time.sleep(1.5)


def main():
    print("Fetching all ECG events...")
    events = fetch_all_events()
    print(f"Total events fetched: {len(events)}")

    updates = []
    already_correct = 0
    for e in events:
        u = build_update(e)
        if u:
            updates.append(u)
        else:
            already_correct += 1

    print(f"Events needing fix: {len(updates)}")
    print(f"Already correct (or missing this field): {already_correct}")

    if updates:
        send_batch(updates)
        print(f"Sent {len(updates)} updates")

    # Quick verification on a few
    print("\nSample check after update:")
    try:
        resp = session.get(
            f'{DHIS2_URL}/api/tracker/events',
            params={'program': PROGRAM_UID, 'programStage': STAGE_UID,
                     'pageSize': 5, 'fields': 'event,dataValues'}
        )
        data = resp.json()
        for e in data.get('events', []):
            for dv in e['dataValues']:
                if dv['dataElement'] == VALIDATION_DATE_DE:
                    print(f"  {e['event']}: {dv['value']}")
    except Exception as e:
        print(f"  Verification call failed ({e}) - this doesn't affect whether the updates succeeded. "
              f"Re-run the 'still needs fixing' check separately to confirm.")

    print("\nDONE")


if __name__ == '__main__':
    main()
