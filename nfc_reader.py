"""PN532 (I2C) NFC helpers shared by the player: open the reader, read a tag's
NDEF message with retries, decode records. Lifted from nfc_test.py (known-good)."""
import os
import time

from adafruit_pn532.adafruit_pn532 import MIFARE_CMD_AUTH_A
from adafruit_pn532.i2c import PN532_I2C

# Well-known public keys. Sector 0 of an NFC-formatted Classic card holds the MAD
# (directory) under KEY_MAD; NDEF data sectors use KEY_NDEF. KEY_FACTORY = blank card.
KEY_MAD = bytes.fromhex("A0A1A2A3A4A5")
KEY_NDEF = bytes.fromhex("D3F7D3F7D3F7")
KEY_FACTORY = bytes.fromhex("FFFFFFFFFFFF")

VERBOSE = False



def hexs(data):
    return " ".join(f"{b:02X}" for b in data)


def printable(data):
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


# ---------------------------------------------------------------- card reading

def reselect(pn532):
    """A failed auth/read leaves the card unresponsive; reset and re-select it."""
    poll(pn532)


def auth_sector(pn532, uid, first_block, keys):
    for key in keys:
        if pn532.mifare_classic_authenticate_block(uid, first_block, MIFARE_CMD_AUTH_A, key):
            return key
        reselect(pn532)
    return None


class ReadError(Exception):
    pass


def read16(pn532, block):
    """READ (0x30) via InDataExchange: 16 bytes - one MIFARE Classic block, or FOUR
    NTAG pages. (The library's ntag2xx_read_block fetches 16 and throws 12 away.)
    Returns None on failure, including a reply timeout."""
    resp = pn532.call_function(0x40, params=[0x01, 0x30, block & 0xFF],
                               response_length=17, timeout=0.5)
    if resp is None or len(resp) < 17 or resp[0] != 0x00:
        return None
    return bytes(resp[1:17])


def read_classic(pn532, uid):
    """Return the NDEF message from a MIFARE Classic 1K (data sectors 1-15).
    Raises ReadError if any block fails, so a partial read is never returned."""
    data = bytearray()
    for sector in range(1, 16):  # sector 0 is the directory (MAD); NDEF starts at 1
        first = sector * 4
        key = auth_sector(pn532, uid, first, [KEY_NDEF, KEY_FACTORY])
        if key is None:
            raise ReadError(f"sector {sector}: auth failed")
        if VERBOSE:
            print(f"  sector {sector:2d}: key {key.hex().upper()}")
        for block in range(first, first + 3):  # skip the trailer (keys/access bits)
            blk = read16(pn532, block)
            for _ in range(3):  # a failed read halts the card: re-select, re-auth, retry
                if blk is not None:
                    break
                reselect(pn532)
                if auth_sector(pn532, uid, first, [key]):
                    blk = read16(pn532, block)
            if blk is None:
                raise ReadError(f"block {block}: read failed")
            if VERBOSE:
                print(f"    block {block:3d}: {hexs(blk)}  |{printable(blk)}|")
            data += blk
        status, msg = find_ndef_message(data)
        if status != "need_more":
            return msg
    raise ReadError("ran out of card before the NDEF message ended")


def read_ntag(pn532):
    """Return the NDEF message from an NTAG/Ultralight (user memory from page 4),
    16 bytes (4 pages) per transaction."""
    data = bytearray()
    for page in range(4, 231, 4):
        blk = read16(pn532, page)
        for _ in range(3):  # re-select and retry before giving up
            if blk is not None:
                break
            reselect(pn532)
            blk = read16(pn532, page)
        if blk is None:
            raise ReadError(f"pages {page}-{page + 3}: read failed")
        if VERBOSE:
            print(f"  pages {page:3d}+: {hexs(blk)}  |{printable(blk)}|")
        data += blk
        status, msg = find_ndef_message(data)
        if status != "need_more":
            return msg
    raise ReadError("ran out of card before the NDEF message ended")


def read_tag(pn532, uid, attempts=4):
    """Read with retries; each retry re-selects the card. Returns NDEF bytes or None."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return read_ntag(pn532) if len(uid) == 7 else read_classic(pn532, uid)
        except ReadError as exc:
            last_err = exc
            if VERBOSE:
                print(f"  attempt {attempt} failed: {exc}")
            again = None
            for _ in range(4):  # give the tag up to ~2s to answer again
                again = poll(pn532)
                if again is not None:
                    break
                time.sleep(0.4)
            if again != uid:
                raise ReadError("tag removed mid-read - hold it still a moment longer")
    raise ReadError(f"gave up after {attempts} attempts ({last_err})")


# ---------------------------------------------------------------- NDEF parsing

def find_ndef_message(data):
    """Walk the TLVs. Returns ("ok", msg), ("none", None) or ("need_more", None)
    - the last meaning the data read so far stops part-way through."""
    i = 0
    while i < len(data):
        t = data[i]
        if t == 0x00:  # NULL TLV
            i += 1
            continue
        if t == 0xFE:  # terminator
            return "none", None
        if i + 4 > len(data):
            return "need_more", None
        length = data[i + 1]
        i += 2
        if length == 0xFF:
            length = int.from_bytes(data[i:i + 2], "big")
            i += 2
        if i + length > len(data):
            return "need_more", None
        if t == 0x03:
            return "ok", bytes(data[i:i + length])
        i += length
    return "need_more", None


def parse_records(msg):
    records = []
    i = 0
    while i < len(msg):
        hdr = msg[i]
        sr, il, tnf = hdr & 0x10, hdr & 0x08, hdr & 0x07
        type_len = msg[i + 1]
        i += 2
        if sr:
            payload_len = msg[i]
            i += 1
        else:
            payload_len = int.from_bytes(msg[i:i + 4], "big")
            i += 4
        id_len = 0
        if il:
            id_len = msg[i]
            i += 1
        rtype = msg[i:i + type_len]
        i += type_len + id_len
        payload = msg[i:i + payload_len]
        i += payload_len
        records.append((tnf, rtype, payload))
        if hdr & 0x40:  # ME - message end
            break
    return records


def open_reader(i2c, log=print):
    """(Re)initialise the PN532, retrying until it answers."""
    while True:
        try:
            pn532 = PN532_I2C(i2c, debug=False)
            _, ver, rev, _ = pn532.firmware_version
            pn532.SAM_configuration()
            set_max_retries(pn532)
            log(f"PN532 ready, firmware {ver}.{rev}")
            return pn532
        except (RuntimeError, OSError) as exc:
            log(f"reader not answering ({exc}); retrying in 1s")
            time.sleep(1)


_CMD_RFCONFIGURATION = 0x32
_CMD_INLISTPASSIVETARGET = 0x4A
_CFG_RF_FIELD = 0x01
_CFG_MAX_RETRIES = 0x05
# Tunables (env vars so they can be adjusted in player.env without code changes)
PASSIVE_RETRIES = int(os.environ.get("NFC_RETRIES", "16"))  # search attempts per poll, then "none"
RF_CYCLE = os.environ.get("NFC_RF_CYCLE", "1") == "1"      # reset the field before each poll
RF_OFF_SECONDS = float(os.environ.get("NFC_RF_OFF", "0.05"))   # 10ms wasn't enough in testing
RF_SETTLE_SECONDS = float(os.environ.get("NFC_RF_SETTLE", "0.03"))  # tag power-up; 98% hit rate in testing


def set_max_retries(pn532):
    """By default the PN532 searches for a card FOREVER (MxRtyPassiveActivation=0xFF).
    The library gives up after its timeout but the chip keeps searching, so every
    later command hits a busy reader - tags 'vanish' and ACKs go missing. A finite
    retry count makes each poll finish with a definite yes/no."""
    pn532.call_function(_CMD_RFCONFIGURATION,
                        params=[_CFG_MAX_RETRIES, 0xFF, 0x01, min(PASSIVE_RETRIES, 0xFE)])


def rf_cycle(pn532):
    """Turn the RF field off and on, resetting any tag in the field to idle - a tag
    that has just been read is still 'selected' and won't answer a fresh search."""
    pn532.call_function(_CMD_RFCONFIGURATION, params=[_CFG_RF_FIELD, 0x00])
    time.sleep(RF_OFF_SECONDS)
    pn532.call_function(_CMD_RFCONFIGURATION, params=[_CFG_RF_FIELD, 0x01])
    time.sleep(RF_SETTLE_SECONDS)  # let the tag power up


def select(pn532, timeout=1.0):
    """One InListPassiveTarget (ISO14443A). Returns the UID, or None if no card.
    Unlike the library's read_passive_target this copes with the PN532 replying
    'zero cards found', which it does once retries are finite."""
    resp = pn532.call_function(_CMD_INLISTPASSIVETARGET, params=[0x01, 0x00],
                               response_length=19, timeout=timeout)
    if resp is None:
        raise RuntimeError("no reply to card search (reader busy?)")
    if len(resp) < 6 or resp[0] != 1 or resp[5] > 7:
        return None
    return bytes(resp[6:6 + resp[5]])


def poll(pn532):
    """Presence check that also works for a tag left sitting on the reader."""
    if RF_CYCLE:
        rf_cycle(pn532)
    return select(pn532)


def tag_text(msg):
    """Return the first Text or MIME record's payload as a string, or None."""
    for tnf, rtype, payload in parse_records(msg):
        if tnf == 1 and rtype == b"T":
            status = payload[0]
            lang_len = status & 0x3F
            enc = "utf-16" if status & 0x80 else "utf-8"
            return payload[1 + lang_len:].decode(enc, "replace")
        if tnf == 2:
            return payload.decode("utf-8", "replace")
    return None
