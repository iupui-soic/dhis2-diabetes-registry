"""Read .env without going through the shell.

`set -a && . ./.env && set +a` breaks on any value containing shell
metacharacters. A password with an unquoted `&` in it silently backgrounds
the command and the variable never gets set, which looks exactly like a
missing credential. Passwords are precisely the values most likely to contain
those characters, so the shell is the wrong loader for this file.

This parser takes the value literally, with no expansion and no word
splitting, so a secret never has to be quoted defensively. It is called
automatically by common.dhis2 and common.aireadi, so scripts pick it up
without anyone having to remember the sourcing incantation.

Real environment variables always win, so an explicit export still overrides
the file.
"""

import os

_loaded = False


def find_env_file(start=None):
    """Nearest .env at or above the repository root."""
    here = start or os.path.dirname(os.path.abspath(__file__))
    for directory in (here, os.path.dirname(here)):
        candidate = os.path.join(directory, ".env")
        if os.path.isfile(candidate):
            return candidate
    return None


def parse(path):
    """Parse a .env into a dict. Values are taken literally."""
    values = {}
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            # Strip one layer of matching quotes, but never interpret what is
            # inside them.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    return values


def load(path=None, override=False):
    """Load .env into os.environ. Existing variables win unless override."""
    global _loaded
    path = path or find_env_file()
    if not path:
        return {}
    values = parse(path)
    for key, value in values.items():
        if override or key not in os.environ or not os.environ[key]:
            os.environ[key] = value
    _loaded = True
    return values


def load_once():
    if not _loaded:
        load()
