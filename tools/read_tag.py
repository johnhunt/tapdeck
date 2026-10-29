#!/usr/bin/env python3
"""Standalone check: wait for a tag, print its UID and the decoded text/JSON on it.
Run with the player service stopped (only one program can use the reader)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("BLINKA_SKIP_DEPENDENCY_CHECK", "1")

import board  # noqa: E402
import busio  # noqa: E402

import nfc_reader as nfc  # noqa: E402

pn532 = nfc.open_reader(busio.I2C(board.SCL, board.SDA))
print("Hold a tag on the reader (Ctrl+C to quit)...")
last = None
while True:
    uid = nfc.poll(pn532)
    if uid is None or uid == last:
        last = uid if uid else last
        continue
    last = uid
    t = time.monotonic()
    try:
        msg = nfc.read_tag(pn532, uid)
    except nfc.ReadError as exc:
        print(f"{uid.hex()}: read failed ({exc})")
        last = None
        continue
    text = nfc.tag_text(msg) if msg else None
    print(f"\nUID {uid.hex()}  ({time.monotonic() - t:.2f}s)")
    try:
        print(json.dumps(json.loads(text), indent=2))
    except (TypeError, ValueError):
        print(text if text is not None else "(no text record)")
