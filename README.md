# DHIS2 Type 2 Diabetes Registry: AI-READI ETL

Loads the [AI-READI](https://docs.aireadi.org) dataset into a DHIS2 Tracker
program, one participant per tracked entity, with each modality as its own
repeatable program stage.

Target instance: `https://t2d-registry.plhi.us`
Program: Diabetes Registry, `W3LSFZH3UDq`
Cohort: 2,280 participants

## Before anything else

Credentials come from the environment. Never put one in a script, a notebook,
or a shell command that gets committed.

```bash
cp .env.example .env      # .env is gitignored
$EDITOR .env
set -a && . ./.env && set +a
pip install -r requirements.txt
```

`.env` needs a DHIS2 URL, a write account for the metadata and import steps,
optionally a read-only auditor account for the verification scripts, and the
dataset root. On the JupyterHub server the dataset is at
`/data0/AI-READI/full_subset`.

## Layout

| Folder | What it does |
| --- | --- |
| `common/` | Shared helpers. Every script routes DHIS2 access through here. |
| `1. core registry/` | Builds and imports the 2,280 participants from `registry_master_v3.csv`. |
| `2. wearable script/` | Heart rate, respiratory rate, SpO2, stress, sleep, activity, calories, CGM glucose. |
| `3. ecg/` | 12-lead ECG, one event per recording, parsed from WFDB headers. |
| `4. envt/` | Environment sensor: PM1, PM2.5, PM4, PM10, humidity, temperature, VOC, NOx. |
| `5. diagnosis/` | Diagnosis history from OMOP `condition_occurrence.csv`. |
| `6. retinal imaging and OCTA/` | Retinal photography and OCTA, with DICOM previews uploaded as file resources. |
| `7. verification data script/` | Read-only checks of imported values against the source files. |

Folder names are numbered in the order they were built, not the order you run
them. See below for the run order.

## Run order

Every modality is two steps: create the metadata, then import the data. Step 1
is idempotent, so re-running it is safe. Step 2 is checkpointed and resumable.

```bash
# 0. Core registry. Do this first: everything else attaches to these entities.
python3 "1. core registry/import_data.py"           # writes import_payload.json
python3 "1. core registry/send_to_dhis2.py"         # posts it

# 1. Cache the metadata UIDs. Re-run after any step-1 script.
python3 -m common.metadata_uids --refresh

# 2. Wearable and CGM
python3 "2. wearable script/hourly_step1_final.py"
python3 "2. wearable script/hourly_step2_final.py"
python3 "2. wearable script/hour_backfill_step1_metadata.py"
python3 "2. wearable script/hour_backfill_step2.py"
python3 "2. wearable script/glucose_timestamp_step1_metadata.py"
python3 "2. wearable script/glucose_recount_step1_metadata.py"
python3 "2. wearable script/glucose_recount_step2_backfill.py"
python3 "2. wearable script/threshold_step1_metadata.py"
python3 "2. wearable script/threshold_step2_backfill.py"

# 3. ECG
python3 "3. ecg/ecg_step1_metadata.py"
python3 "3. ecg/ecg_step2_import.py"

# 4. Environment
python3 "4. envt/env_step1_metadata_v2.py"
python3 "4. envt/env_step2_import.py"

# 5. Diagnosis
python3 "5. diagnosis/diagnosis_step1_metadata.py"
python3 "5. diagnosis/diagnosis_step2_import.py"

# 6. Retinal. Always pilot one participant and look at it in Capture first.
python3 "6. retinal imaging and OCTA/retinal_step1_metadata.py"
python3 "6. retinal imaging and OCTA/retinal_step3_add_enface_path.py"
python3 "6. retinal imaging and OCTA/retinal_step2_import.py" --person-id 1072
python3 "6. retinal imaging and OCTA/retinal_step2_import_full.py" --all

# 7. Verify
python3 "7. verification data script/stratified_sample_verification.py"
```

The long imports are meant to run detached:

```bash
nohup python3 "2. wearable script/hourly_step2_final.py" > hourly.log 2>&1 &
tail -f hourly.log
```

Each one writes a checkpoint next to itself. Stopping and re-running picks up
where it left off, and only participants that actually succeeded are skipped.

## Conventions

**Credentials from the environment.** `common.dhis2.get_session()` reads them
and exits with one clear message if they are missing.

**UIDs resolve by name.** `common.metadata_uids` holds the UIDs this project
recorded and looks up the rest from the server, caching to
`metadata_uid_cache.json`. No script should carry a `REPLACE_ME` block to be
pasted in by hand.

**Writes are checked.** `common.dhis2.send_events` and `import_tracker` raise
unless the server confirms the expected number of created or updated records.
Nothing is marked complete on the strength of having been sent.

**Checkpoints record outcomes.** `common.checkpoint.Checkpoint` marks a
participant done only after a confirmed write, and records failures so the
next run retries them.

**A tracker UPDATE replaces an event's data values.** Always build the payload
with `common.dhis2.merge_data_values`, which merges your change into the
event's existing values. Sending only the changed value risks dropping
everything else on the event.

**Everything is UTC.** The wearable and CGM files carry a `Z`, and the
environment CSVs carry no suffix but are UTC too. Both were confirmed by
diurnal check, described in `common/timeutil.py`. Compare hours with
`hour_key`, which compares instants, never by slicing formatted strings.

**Option set values are codes, not names.** Build them with
`common.dhis2.option_value(option_set_name, display_name)`. DHIS2 validates
against the code, so writing the display name is silently rejected.

**Guard non-finite numbers.** The SpO2 exports contain NaN, `str(nan)` is
`"nan"`, and NaN does not raise. Use `common.dhis2.data_value`, which drops it.

## Data notes

These are properties of the source data, verified against
`/data0/AI-READI/full_subset`, not of the code.

- **SpO2 contains NaN.** 77 of 93 participant files sampled (83%) have at
  least one. Filtered in `common.numeric.clean_readings`.
- **Respiratory rate and stress use negative sentinels.** Roughly half of all
  readings, dropped by the extractors.
- **Heart rate uses a zero fill.** Dropped by the extractor.
- **CGM uses the strings `High` and `Low`** for readings outside the sensor's
  measurable range. Present in 13 of 60 files sampled. They are excluded from
  `count` but included in the time-in-range denominator, which
  `aggregate_glucose` now also returns as `count_total`.
- **NOx coverage varies by participant.** The column is always present. Person
  1001 is all NaN, person 7427 has 203,183 usable values in 203,185 rows.
- **`condition_source_value` is truncated at 49 characters** in the AI-READI
  export. 3,388 of 12,375 rows sit at exactly 49. Imported as-is.
- **`registry_master_v3.csv` has no visit date.** Real dates come from
  `participants.tsv`.
- **The dataset tree nests each modality one level deeper** than the manifest
  paths imply. `common.aireadi.resolve` handles both layouts.

## Known limitations

- **Demographic matching is ambiguous.** The six attributes
  `add_person_id.py` joins on identify only 1,418 of 2,280 participants
  uniquely; 862 rows share a key with at least one other. The script refuses
  to write to the ambiguous ones. New imports set `person_id` directly and do
  not need it.
- **Steps Reading Count counts segments per hour touched**, so it does not sum
  to a participant's segment total. The step sum itself is prorated correctly.
- **Retinal OCTA and ECG are verified by event count only.** Their
  source-to-field mappings have not been traced field by field.
- **The 82,302-value core registry result is from an earlier phase.** The
  verification script reports it separately and never folds it into a measured
  match rate.

## Security

The DHIS2 admin password was committed in the initial commit and has since
been rotated. `.gitignore` now covers `.env`, checkpoints, logs and any
dataset file. Before committing:

```bash
git diff --cached | grep -iE "password|secret|token"
```

The AI-READI dataset is access-controlled. Do not commit any part of it.
