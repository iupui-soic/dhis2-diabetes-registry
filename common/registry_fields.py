"""Field encoding for registry_master_v3.csv.

C-04 in the audit: import_data.py wrote fh_dm2pt and fh_dm2sb to DHIS2 as
"true"/"false" because they are listed in BOOLEAN_FIELDS, while
add_person_id.py rebuilt its join key with str(row["fh_dm2pt"]) and got
"1.0"/"0.0". Two of the six key components could therefore never match, so
the matching script stopped without doing anything on every run.

Both scripts now encode through clean_value here, so the CSV side and the
DHIS2 side cannot drift apart again.
"""

import pandas as pd

# Columns stored in DHIS2 as BOOLEAN. Verified against registry_master_v3.csv:
# every one of these holds only 0 or 1.
BOOLEAN_FIELDS = frozenset({
    "mhterm_dm2", "mhterm_predm", "fh_dm2pt", "fh_dm2sb",
    "mhoccur_hbp", "mhoccur_clsh", "mhoccur_obs", "mhoccur_mi",
    "mhoccur_strk", "mhoccur_circ", "mhoccur_pdr",
    "sualckncf", "susmkncf", "susmkcdur",
})


def clean_value(field, value):
    """Encode one CSV cell the way DHIS2 stores it. None means "no value"."""
    if value is None or pd.isna(value):
        return None
    if field in BOOLEAN_FIELDS:
        return "true" if float(value) == 1.0 else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def attribute_key(row, fields):
    """Build a comparison key from a CSV row using the DHIS2 encoding.

    Returns a tuple with the same shape the DHIS2 attribute values produce,
    so the two sides are directly comparable.
    """
    return tuple(
        clean_value(field, row.get(field)) or "" for field in fields
    )


def dhis2_key(attributes, display_names):
    """Build the same key from a tracked entity's attribute display names."""
    return tuple(attributes.get(name) or "" for name in display_names)
