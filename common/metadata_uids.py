"""The canonical UID registry for the Diabetes Registry program.

This module is the fix for two related audit findings:

  * The verification scripts imported `metadata_uids` and it was never
    committed, so none of them could run.
  * Every other script opened with a block of REPLACE_ME constants to be
    pasted in by hand after running its step-1 counterpart, which is how
    metadata and import drifted apart.

UIDs that are documented in the repository are recorded below as constants.
Everything else is resolved from the live server by name through `load()`,
which is the only way to be correct about UIDs this repository never wrote
down. The result is cached in a gitignored file so repeated runs are cheap.

Usage:

    from common import dhis2, metadata_uids as M
    session = dhis2.get_session(read_only=True)
    uids = M.load(session)
    uids.stage("CGM - Glucose")
    uids.data_element("Glucose Mean")
"""

import json
import os

from common import dhis2

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "metadata_uid_cache.json")

# ---------------------------------------------------------------------------
# Known constants, transcribed from the scripts that created these objects.
# ---------------------------------------------------------------------------

PROGRAM_UID = "W3LSFZH3UDq"
PERSON_ID_ATTR_UID = "oFbmOHnKYaX"

WEARABLE_HEART_RATE_STAGE_UID = "XB29GdXrNDb"
WEARABLE_RESPIRATORY_RATE_STAGE_UID = "ZHqSqHOv8is"
WEARABLE_SPO2_STAGE_UID = "QoigcBfYCcG"
WEARABLE_STRESS_STAGE_UID = "g803i2FH8bF"
WEARABLE_SLEEP_STAGE_UID = "aR9APTYYiEe"
WEARABLE_ACTIVITY_STAGE_UID = "uASeLWkCtRB"
WEARABLE_CALORIES_STAGE_UID = "xfNfR1XEwM8"
CGM_GLUCOSE_STAGE_UID = "SS7a20eCnBZ"
ECG_STAGE_UID = "xQjp0SgUbzv"

THRESHOLD_STAGE_UIDS = {
    "HR": WEARABLE_HEART_RATE_STAGE_UID,
    "RR": WEARABLE_RESPIRATORY_RATE_STAGE_UID,
    "SPO2": WEARABLE_SPO2_STAGE_UID,
}

WEARABLE_STAGE_UIDS = {
    "Wearable - Heart Rate": WEARABLE_HEART_RATE_STAGE_UID,
    "Wearable - Respiratory Rate": WEARABLE_RESPIRATORY_RATE_STAGE_UID,
    "Wearable - SpO2": WEARABLE_SPO2_STAGE_UID,
    "Wearable - Stress": WEARABLE_STRESS_STAGE_UID,
    "Wearable - Sleep": WEARABLE_SLEEP_STAGE_UID,
    "Wearable - Activity": WEARABLE_ACTIVITY_STAGE_UID,
    "Wearable - Calories": WEARABLE_CALORIES_STAGE_UID,
    "CGM - Glucose": CGM_GLUCOSE_STAGE_UID,
}

# Shared across all eight wearable and CGM stages.
HOUR_START_DE = "Ef2A6W8ouAq"
HOUR_END_DE = "jSZPIXD5WmW"

SLEEP_DURATION_DE = "Z7fmggiTvKu"

# CGM - Glucose data elements.
GLUCOSE_FIELD_UIDS = {
    "mean": "aEZ4bHknKN9",
    "min": "MFVsn5k7PeT",
    "max": "DPeHrdcFFeu",
    "count": "SE6sHxR9BQd",
    "sd": "wMyU6v71gXo",
    "tir_pct": "chIDrmzk3Nj",
    "tar_pct": "pJUGzbk0n77",
    "tbr_pct": "OyP2poPBmat",
    # Device sentinel counts: the sensor's own out-of-measurable-range flags.
    "high_count": "RZpt03lifR6",
    "low_count": "hi1TAwCqN0f",
    # Clinical threshold counts and timestamps: numeric >180 or <70, combined
    # with the device sentinels.
    "above_ts": "Zu4iFxtthSU",
    "below_ts": "LfzwHxQUotL",
    "above_count": "W5ki8H2QpmF",
    "below_count": "xHFP3IuUfUZ",
}

ECG_VALIDATION_DATE_DE = "wweIn1KripN"

ENV_HUMIDITY_EXTRA_UIDS = {
    "above_count": "c81Px0T8iVm",
    "below_count": "Khi2x79AOlF",
}

RETINAL_PHOTOGRAPHY_STAGE_UID = "liu03FpCFAu"
RETINAL_OCTA_STAGE_UID = "fPtwl74y5sa"

RETINAL_PHOTOGRAPHY_FIELD_UIDS = {
    "manufacturer": "woBQgwZwGQA",
    "manufacturers_model_name": "sjSVdduenGk",
    "laterality": "Lu6rZlrpMTh",
    "anatomic_region": "bv0UVaFHpzV",
    "imaging": "YmW875klX3f",
    "height": "pfm2b5lvaNH",
    "width": "NGJ4xkIQqVf",
    "color_channel_dimension": "RvGT5kqR30X",
    "sop_instance_uid": "Rn0jIRyxQCb",
    "filepath": "guccDnZvslS",
    "preview": "coXV8PezvH4",
}

RETINAL_OCTA_FIELD_UIDS = {
    "manufacturer": "q1J92WGONwn",
    "manufacturers_model_name": "iXyxnT1iofO",
    "laterality": "fHj2ZC0nMie",
    "anatomic_region": "NwN0FKAzgIO",
    "imaging": "FHdoPAMBIYQ",
    "flow_cube_height": "nclbtzFCJCi",
    "flow_cube_width": "Q3LiLrfq8fT",
    "flow_cube_number_of_frames": "myMVImtRU2B",
    "flow_cube_sop_instance_uid": "czla8Bb3juP",
    "flow_cube_file_path": "Z7v2SChygsp",
    "segmentation_file_path": "Fk1rRxktxw4",
    "segmentation_sop_instance_uid": "UwBGNNrxKVA",
    "segmentation_type": "qprOW77njq1",
    "enface_layer": "aNcEJk59LL9",
    "enface_sop_instance_uid": "KRRM3cE6JTz",
    "preview": "nwr0tNqs95Q",
    # Added by retinal_step3_add_enface_path.py. Without it the OCTA preview
    # cannot be rebuilt from the event, which is why the first backfill
    # converted the flow cube by mistake.
    "enface_file_path": None,
}

# Data element names whose UIDs this repository never recorded. They are
# resolved from the server by name in load().
UNRECORDED_DATA_ELEMENTS = [
    "Mean Value", "Minimum Value", "Maximum Value", "Reading Count",
    "Sleep Stage", "Sleep Segment Duration Minutes",
    "Steps Sum", "Steps Reading Count",
    "Calories Sum", "Calories Reading Count",
    "Env Mean", "Env Minimum", "Env Maximum",
    "Env Standard Deviation", "Env Reading Count",
    "Env Hour Start", "Env Hour End",
    "Diagnosis Condition Code", "Diagnosis Condition Label", "Diagnosis Date",
    "Retinal OCTA - En-face DICOM File Path",
]

UNRECORDED_STAGES = [
    "Environment - PM1", "Environment - PM2.5", "Environment - PM4",
    "Environment - PM10", "Environment - Humidity", "Environment - Temperature",
    "Environment - VOC", "Environment - NOx",
    "Diagnosis History",
]

# Object names on the server mix dash characters: "Wearable – Heart Rate"
# uses an en dash, while "Cardiac – 12-Lead ECG" uses an en dash AND a hyphen
# in the same name. Normalise every dash to one character on both sides
# before comparing, rather than rewriting one into the other, which would
# also rewrite the hyphen in "12-Lead".
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"


def _normalise(name):
    return "".join("-" if ch in _DASHES else ch for ch in name).casefold()


class Registry:
    """Resolved metadata, keyed by object name."""

    def __init__(self, stages, data_elements, option_sets=None):
        self.stages = stages
        self.data_elements = data_elements
        self.option_sets = option_sets or {}

    def option_value(self, option_set_name, display_name):
        """The code to store for an option, as the server actually defines it."""
        options = self.option_sets.get(option_set_name)
        if options is None:
            index = {_normalise(k): v for k, v in self.option_sets.items()}
            options = index.get(_normalise(option_set_name))
        if options:
            if display_name in options:
                return options[display_name]
            by_name = {_normalise(k): v for k, v in options.items()}
            if _normalise(display_name) in by_name:
                return by_name[_normalise(display_name)]
        raise KeyError(
            f"option '{display_name}' is not in option set '{option_set_name}'. "
            f"Run `python3 -m common.metadata_uids --refresh` to rebuild the cache."
        )

    def _lookup(self, table, name, what):
        if name in table:
            return table[name]
        index = {_normalise(k): v for k, v in table.items()}
        key = _normalise(name)
        if key in index:
            return index[key]
        raise KeyError(
            f"{what} '{name}' is not in the registry. "
            f"Run `python3 -m common.metadata_uids --refresh` to rebuild the cache."
        )

    def stage(self, name):
        return self._lookup(self.stages, name, "program stage")

    def data_element(self, name):
        return self._lookup(self.data_elements, name, "data element")

    def maybe_data_element(self, name):
        try:
            return self.data_element(name)
        except KeyError:
            return None

    def environment_stage(self, column):
        return self.stage({
            "pm1": "Environment - PM1",
            "pm2.5": "Environment - PM2.5",
            "pm4": "Environment - PM4",
            "pm10": "Environment - PM10",
            "hum": "Environment - Humidity",
            "temp": "Environment - Temperature",
            "voc": "Environment - VOC",
            "nox": "Environment - NOx",
        }[column])

    def env_field_uids(self):
        return {
            "mean": self.data_element("Env Mean"),
            "min": self.data_element("Env Minimum"),
            "max": self.data_element("Env Maximum"),
            "sd": self.data_element("Env Standard Deviation"),
            "count": self.data_element("Env Reading Count"),
            "hour_start": self.data_element("Env Hour Start"),
            "hour_end": self.data_element("Env Hour End"),
        }

    def wearable_shared_uids(self):
        return {
            "mean": self.data_element("Mean Value"),
            "min": self.data_element("Minimum Value"),
            "max": self.data_element("Maximum Value"),
            "count": self.data_element("Reading Count"),
        }


def _fetch(session):
    """Read every stage, data element and option set the program uses."""
    program = dhis2.get_json(
        session, f"programs/{PROGRAM_UID}",
        {"fields": "programStages[id,name,programStageDataElements[dataElement[id,name]]]"},
    )
    stages, elements = {}, {}
    for stage in program.get("programStages", []):
        stages[stage["name"]] = stage["id"]
        for psde in stage.get("programStageDataElements", []):
            de = psde["dataElement"]
            elements[de["name"]] = de["id"]

    # Option codes are read from the server, never derived. The codes already
    # in use here are plain (WITHIN_RANGE), and a script that invented its own
    # convention would write values no option matches.
    option_sets = {
        item["name"]: {opt["name"]: opt["code"] for opt in item.get("options", [])}
        for item in dhis2.get_json(
            session, "optionSets",
            {"fields": "name,options[name,code]", "paging": "false"},
        ).get("optionSets", [])
    }
    return stages, elements, option_sets


def load(session=None, refresh=False):
    """Return a Registry, using the cache unless refresh is requested."""
    if not refresh and os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as fh:
            cached = json.load(fh)
        return Registry(cached["stages"], cached["dataElements"],
                        cached.get("optionSets"))

    if session is None:
        session = dhis2.get_session(read_only=True)
    stages, elements, option_sets = _fetch(session)
    with open(CACHE_FILE, "w") as fh:
        json.dump(
            {"stages": stages, "dataElements": elements, "optionSets": option_sets},
            fh, indent=2, sort_keys=True,
        )
    return Registry(stages, elements, option_sets)


def _main():
    import argparse
    parser = argparse.ArgumentParser(description="Rebuild the metadata UID cache")
    parser.add_argument("--refresh", action="store_true", help="re-read from the server")
    args, _ = parser.parse_known_args()

    registry = load(refresh=args.refresh or not os.path.exists(CACHE_FILE))
    print(f"program stages: {len(registry.stages)}")
    print(f"data elements:  {len(registry.data_elements)}")

    missing = [n for n in UNRECORDED_DATA_ELEMENTS if registry.maybe_data_element(n) is None]
    if missing:
        print(f"\nnot found on the server ({len(missing)}):")
        for name in missing:
            print(f"  {name}")
    print(f"\ncache written to {os.path.normpath(CACHE_FILE)}")


if __name__ == "__main__":
    _main()
