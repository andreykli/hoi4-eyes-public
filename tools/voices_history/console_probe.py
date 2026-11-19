# Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors
# SPDX-License-Identifier: MIT
# Adapted from injector.py:Injector.write_run_file and
# safe_actions.py:ActionJournal.receive. See LICENSE and ORIGIN.md.
"""Manual, read-only acceptance probe for the HOI4 run-file transport."""
import re
from pathlib import Path
from uuid import uuid4

from hoi4_country_tags import COUNTRY_TAG_RE
from validate_hoi4_gui import parse_pdx


PROBE_NAME = re.compile(r"eyes_transport_([0-9a-f]{32})\.txt")
LOG_MARKER = re.compile(
    rb"(?:^|\]:?\s*)EYES_TRANSPORT_id_([0-9a-f]{32})_status_(present|absent|wrong_player)$"
)


def write_probe(directory, player, counterpart):
    if (not COUNTRY_TAG_RE.fullmatch(player) or not COUNTRY_TAG_RE.fullmatch(counterpart)
            or player == counterpart):
        raise ValueError("Two distinct HOI4 country tags required")
    nonce = uuid4().hex
    path = Path(directory) / f"eyes_transport_{nonce}.txt"
    prefix = f"EYES_TRANSPORT_id_{nonce}_status_"
    script = (
        f"{player} = {{ if = {{ limit = {{ is_ai = no country_exists = {counterpart} }} "
        f"if = {{ limit = {{ has_non_aggression_pact_with = {counterpart} }} "
        f"log = {prefix}present }} else = {{ log = {prefix}absent }} }} "
        f"else = {{ log = {prefix}wrong_player }} }}"
    )
    parse_pdx(script)
    with path.open('x', encoding='ascii', newline='\n') as file:
        file.write("eval_effect " + script + "\n")
    return path


def probe_result(path, log):
    name = PROBE_NAME.fullmatch(Path(path).name)
    if not name:
        raise ValueError("Expected an EYES transport probe filename")
    nonce = name[1].encode('ascii')
    results = []
    with Path(log).open('rb') as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - 4_000_000)
        stream.seek(offset)
        window = stream.read(4_000_000)
    lines = window.split(b'\n')
    # Ignore a partial line at either boundary and count only exact marker messages.
    for line in lines[bool(offset):-1]:
        marker = LOG_MARKER.search(line.rstrip())
        if marker and marker[1] == nonce:
            results.append(marker[2].decode('ascii'))
    return {"status": "observed" if len(results) == 1 else "duplicate" if results else "not_observed",
            "pact_observation": results[0] if len(results) == 1 else None}
