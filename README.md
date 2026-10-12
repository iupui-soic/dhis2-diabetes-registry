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
pip install -r requirements.txt
nbstripout --install --attributes .gitattributes   # once per clone
```

The scripts read `.env` themselves. Do not source it with `set -a && . ./.env`:
the shell mangles any password containing `&` or `$`, and the variable arrives
empty, which looks exactly like a missing credential.

`.env` needs a DHIS2 URL, a write account for the metadata and import steps,
a read-only auditor account for the verification scripts, and the dataset
root. On the JupyterHub server the dataset is at `/data0/AI-READI/full_subset`.

The server has an `Auditor` role with search, view and export authorities and
nothing that writes. Create a user with only that role (Users app > Add,
assign the registry org unit) and put it in `DHIS2_AUDITOR_USER` and
`DHIS2_AUDITOR_PASS`. Without those two, `get_session(read_only=True)` falls
back to the write account and the verification scripts are no longer unable
to write.

`nbstripout` installs a git filter that drops notebook outputs on commit.
Notebook outputs in this repository have held participant rows before, and
`.gitignore` cannot catch those.

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
is idempotent, so re-running it is safe. Step 2 is checkpointed and resumable,
and every event it creates carries a UID derived from its source identity
(participant, stage and hour, or SOP Instance UID), sent with
CREATE_AND_UPDATE. Re-running a step, resuming after a kill, or retrying after
a proxy timeout therefore updates the same events rather than duplicating them.
Events loaded before derived UIDs existed carry server-assigned UIDs, so each
step-2 script first matches the participant's existing events by identity and
reuses their UIDs (`common.dhis2.EventReconciler`). Verified on participant
1072: a re-run over 3,798 existing events updated every one in place and
created none.

```bash
# 0. Core registry. Do this first: everything else attaches to these entities.
#    The program, its attributes and its three core stages were built in the
#    Maintenance app and are not created by any script. build_uid_lookup.py
#    resolves their UIDs by name (confirmed against the live program on
#    2026-10-12); --inventory shows what the server has if a name stops
#    matching. It warns if the person_id attribute is not flagged unique;
#    set that in Maintenance so the server itself refuses duplicates.
python3 "1. core registry/build_uid_lookup.py"      # writes uid_lookup.json
python3 "1. core registry/import_data.py"           # writes import_payload.json
python3 "1. core registry/send_to_dhis2.py"         # posts it, skipping person_ids already on the server

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

# 7. Verify. Writes stratified_sample_checkpoint.json (per-group counts) and
#    stratified_sample_mismatches.json (every mismatch with its stage,
#    participant, hour, field and both values) next to the script, and
#    prints a breakdown by field and kind at the end. --fresh clears both.
python3 "7. verification data script/stratified_sample_verification.py"
```

The long imports are meant to run detached:

```bash
nohup python3 "2. wearable script/hourly_step2_final.py" > hourly.log 2>&1 &
tail -f hourly.log
```

Each one writes a checkpoint next to itself. Stopping and re-running picks up
where it left off, and only participants that actually succeeded are skipped.
A participant with no tracked entity yet is recorded as *skipped*, not done,
so once the core registry has them the next run imports their data.
Checkpoints written by the pre-refactor scripts are read as well.

## Conventions

**Credentials from the environment.** `common.dhis2.get_session()` reads them
and exits with one clear message if they are missing.

**UIDs resolve by name.** `common.metadata_uids` holds the UIDs this project
recorded and looks up the rest from the server, caching to
`metadata_uid_cache.json`. No script should carry a `REPLACE_ME` block to be
pasted in by hand.

**Writes are checked.** `common.dhis2.send_events` and `import_tracker` raise
unless the server confirms the expected number of created or updated records
*for the object type being sent*. The top-level stats on a tracker import
report sum tracked entities, enrollments and events, so a participant batch
is checked against the `TRACKED_ENTITY` breakdown. Nothing is marked complete
on the strength of having been sent.

**Events carry derived UIDs.** Build them with `common.dhis2.assign_event_uids`
or `generate_uid`, and send with `CREATE_AND_UPDATE`. A plain `CREATE` has
nothing to match on and duplicates on every retry.

**Existing events are matched by identity, not by UID.** Before sending,
wrap the participant's events in `common.dhis2.EventReconciler` with an
identity function: `hour_identity` for hourly stages, `value_identity(...)`
on the code, label and date for a diagnosis, on the header path for an ECG,
on the SOP Instance UID for an image. Matched events keep the server's UID
and are updated; unmatched ones are created under their derived UID. The
reconciler also counts server events the source no longer accounts for and
says so in the per-participant line.

**Verification records every mismatch, not just the count.** `Tally.compare`
in `stratified_sample_verification.py` appends each mismatch with its
group, stage, participant, hour, field, both values and the source hour's
reading count to `stratified_sample_mismatches.json`, and the run ends
with a breakdown by field and kind (`values differ` versus `undefined on
both sides`). A match rate on its own cannot be explained; the file is
what lets a discrepancy be traced to its cause without re-running the
source aggregation. It holds participant-level values, so it is
gitignored; the counts go in `results/`.

**Checkpoints record outcomes.** `common.checkpoint.Checkpoint` marks a
participant done only after a confirmed write, and records failures so the
next run retries them.

**Build event updates with `common.dhis2.merge_data_values`.** It carries the
event's existing values alongside the change, so the payload is correct
whether the server replaces or merges the set. DHIS2 2.44 merges, which means
omitting an element never clears it: pass `None` for an element and the helper
sends it with an empty value, which is the only way to remove one.

**Metadata names are matched dash-insensitively.** The server holds
"Cardiac – 12-Lead ECG" with an en dash. `lookup_by_name` falls back to a
normalised comparison, and `common.metadata_uids` refuses to load if two
stages differ only by dash or case, since that is what a duplicate created by
an exact-match re-run looks like.

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

## Verification results

The most recent run of `stratified_sample_verification.py` (12 October 2026,
proportional allocation, target 2,000 participants) is recorded in
`7. verification data script/results/`, as the checkpoint JSON and the run
log. It compared 14,331,881 values across 2,001 participant draws, with
zero events lacking a source hour and zero source hours lacking an event in
every group. 200 values mismatched (99.9986% match). All 200 were traced:
each is a statistic that is undefined on both sides, a standard deviation of
an hour with one reading (64 glucose, 55 environmental) or the mean, minimum
and maximum of a glucose hour containing only `High`/`Low` markers (81). The
script counts an empty pair as a mismatch on purpose, so no stored value
differed from its source. The script now writes every mismatch with its
context to `stratified_sample_mismatches.json` and prints a breakdown by
field and kind at the end of a run, so this trace no longer needs a separate
script.

| Group | Draws | Compared | Mismatched | Match % |
| --- | ---: | ---: | ---: | ---: |
| Wearable (HR, RR, SpO2, stress) | 471 | 1,288,628 | 0 | 100.0000 |
| Glucose | 160 | 369,940 | 145 | 99.9608 |
| Environment (8 channels) | 1,328 | 12,662,360 | 55 | 99.9996 |
| Diagnosis | 4 | 72 | 0 | 100.0000 |
| Retinal photography | 29 | 10,872 | 0 | 100.0000 |
| Retinal OCTA (event counts) | 8 | 8 | 0 | 100.0000 |
| ECG (event counts) | 1 | 1 | 0 | 100.0000 |

The proportional allocation gives the small stages very few draws. Pass
`--fixed-n` to set a floor if those groups are the question.

`cohort_query_gradable_cgm.py`, re-run the same day, assembled 2,205 of the
2,280 participants (gradable photography and at least seven CGM days) in
184 s through the web interface; the result is in the same JSON file.

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

## Publication

A preliminary account of this registry and its mapping model was accepted as
an abstract at the IEEE-EMBS International Conference on Biomedical and
Health Informatics (BHI) 2026: A. N. Perumbilly, Z. Yin, S. Bucher and
S. Purkayastha, "A Metadata-Driven ETL and Mapping Model for a Multimodal
Diabetes Registry in DHIS2." The registry has grown since that abstract
(27 stages and 445 data elements against the 23 and 152 reported there),
and its verification figure came from the earlier sampling procedure that
the audit replaced; the figures in this README supersede it.

## Security

The original initial commit contained the DHIS2 admin password, the full
`registry_master_v3.csv` with 2,280 participant rows, two affected-patient
lists, the import checkpoints and notebook outputs. The password has been
rotated. On 2026-10-12 the history was rewritten with `git filter-repo` and
force-pushed, and every blob in every remaining commit was checked clean. Any
clone made before that date still holds the old history: re-clone, never
pull or merge from it. GitHub keeps serving the old commits by SHA, and the
fork keeps its copy, until GitHub support purges them; that request is
tracked outside this README.

`.gitignore` covers `.env`, checkpoints, logs, the affected-patient lists
and any dataset file. `nbstripout` strips notebook outputs on commit. Before
committing:

```bash
git diff --cached | grep -iE "password|secret|token"
git diff --cached --stat | grep -iE "ipynb|csv|json"
```

The AI-READI dataset is access-controlled. Do not commit any part of it,
including in notebook outputs.
