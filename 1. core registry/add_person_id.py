import pandas as pd
import requests

DHIS2_URL = "http://localhost:8080"
AUTH = ("admin", "Londonbridge@2026")
PROGRAM_UID = "Z7Gdp0CXP9K"
PERSON_ID_ATTR_UID = "EGcWxvs2UK8"

df = pd.read_csv("registry_master_v3.csv")

def make_key(row):
    return (
        str(int(row["year_of_birth"])),
        str(row["study_group"]),
        str(row["cl_maristat"]) if pd.notna(row["cl_maristat"]) else "NA",
        str(row["clinical_site"]),
        str(row["fh_dm2pt"]) if pd.notna(row["fh_dm2pt"]) else "NA",
        str(row["fh_dm2sb"]) if pd.notna(row["fh_dm2sb"]) else "NA",
    )

csv_by_key = {}
duplicates = 0
for _, row in df.iterrows():
    key = make_key(row)
    if key in csv_by_key:
        duplicates += 1
    csv_by_key[key] = row

print(f"Built {len(csv_by_key)} unique keys from CSV ({duplicates} collisions found)")

resp = requests.get(
    f"{DHIS2_URL}/api/tracker/trackedEntities",
    auth=AUTH,
    params={
        "program": PROGRAM_UID,
        "pageSize": 2280,
        "fields": "trackedEntity,attributes[displayName,value]",
    },
)
entities = resp.json().get("trackedEntities", [])
print(f"Fetched {len(entities)} entities from DHIS2")

matched = 0
unmatched = 0
updates = []

for e in entities:
    attrs = {a["displayName"]: a["value"] for a in e.get("attributes", [])}
    key = (
        attrs.get("Year of Birth", ""),
        attrs.get("Diabetes Severity Group", ""),
        attrs.get("Marital Status", "NA"),
        attrs.get("Clinical Recruitment Site", ""),
        attrs.get("Family History - Parent T2D", "NA"),
        attrs.get("Family History - Sibling T2D", "NA"),
    )
    if key in csv_by_key:
        matched += 1
        person_id = str(int(csv_by_key[key]["person_id"]))
        updates.append({"trackedEntity": e["trackedEntity"], "person_id": person_id})
    else:
        unmatched += 1

print(f"Matched: {matched}, Unmatched: {unmatched}")

if unmatched > 0:
    print("STOPPING - not all records matched uniquely. Review before proceeding.")
else:
    BATCH = 100
    for i in range(0, len(updates), BATCH):
        batch = updates[i:i+BATCH]
        payload = {
            "trackedEntities": [
                {
                    "trackedEntity": u["trackedEntity"],
                    "attributes": [{"attribute": PERSON_ID_ATTR_UID, "value": u["person_id"]}],
                }
                for u in batch
            ]
        }
        r = requests.post(
            f"{DHIS2_URL}/api/tracker",
            auth=AUTH,
            params={"importStrategy": "UPDATE"},
            json=payload,
        )
        result = r.json()
        stats = result.get("stats", {})
        print(f"Batch {i//BATCH + 1}: updated={stats.get('updated', 0)}, ignored={stats.get('ignored', 0)}")

    print("Done - person_id added to all matched records")
