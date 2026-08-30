import json
import requests

DHIS2_URL = "http://localhost:8080"
DHIS2_USER = "admin"
DHIS2_PASSWORD = "district"

PROJECT_DIR = "/home/ainaperu/diabetes_registry_project"
PAYLOAD_PATH = f"{PROJECT_DIR}/import_payload.json"
BATCH_SIZE = 100

with open(PAYLOAD_PATH) as f:
    data = json.load(f)

all_teis = data["trackedEntities"]
print(f"Total participants to import: {len(all_teis)}")

total_created = 0
total_ignored = 0
error_log = []

for i in range(0, len(all_teis), BATCH_SIZE):
    batch = all_teis[i:i + BATCH_SIZE]
    batch_num = i // BATCH_SIZE + 1
    total_batches = (len(all_teis) + BATCH_SIZE - 1) // BATCH_SIZE

    response = requests.post(
        f"{DHIS2_URL}/api/tracker",
        json={"trackedEntities": batch},
        auth=(DHIS2_USER, DHIS2_PASSWORD),
        params={"async": "false", "importStrategy": "CREATE"},
    )

    try:
        result = response.json()
    except ValueError:
        print(f"Batch {batch_num}: non-JSON response, status {response.status_code}")
        print(response.text[:500])
        break

    stats = result.get("stats", {})
    created = stats.get("created", 0)
    ignored = stats.get("ignored", 0)

    total_created += created
    total_ignored += ignored

    print(f"Batch {batch_num}/{total_batches}: {created} created, {ignored} ignored, http={response.status_code}")

    if ignored > 0 or response.status_code >= 400:
        error_log.append({"batch": batch_num, "response": result})

print()
print(f"FINAL RESULT: {total_created} created, {total_ignored} ignored out of {len(all_teis)}")

if error_log:
    with open(f"{PROJECT_DIR}/import_errors.json", "w") as f:
        json.dump(error_log, f, indent=2)
    print(f"Saved details for {len(error_log)} problem batch(es) to import_errors.json")
else:
    print("No errors - every participant imported successfully!")
