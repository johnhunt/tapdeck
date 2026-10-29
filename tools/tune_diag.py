"""Tag-on-reader diagnostic: compare polling settings (hit rate, longest miss streak)
and time full tag reads. Run with the player service stopped."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ.setdefault("BLINKA_SKIP_DEPENDENCY_CHECK", "1")
import board  # noqa: E402
import busio  # noqa: E402

import nfc_reader as nfc  # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60
CONFIGS = {  # name: (rf_cycle, rf_off, settle, retries)
    "noRF  r4":        (False, 0.00, 0.000, 4),
    "noRF  r16":       (False, 0.00, 0.000, 16),
    "RF50  s5  r4":    (True, 0.05, 0.005, 4),
    "RF50  s30 r4":    (True, 0.05, 0.030, 4),
    "RF50  s30 r16":   (True, 0.05, 0.030, 16),
    "RF100 s50 r16":   (True, 0.10, 0.050, 16),
}

i2c = busio.I2C(board.SCL, board.SDA)
pn = nfc.open_reader(i2c)
stats = {k: {"hit": 0, "n": 0, "err": 0, "streak": 0.0, "max_streak": 0.0, "miss_since": None,
             "t": 0.0} for k in CONFIGS}


def apply(cfg):
    rf, off, settle, retries = cfg
    nfc.RF_CYCLE, nfc.RF_OFF_SECONDS, nfc.RF_SETTLE_SECONDS = rf, off, settle
    if nfc.PASSIVE_RETRIES != retries:
        nfc.PASSIVE_RETRIES = retries
        nfc.set_max_retries(pn)


t0 = time.monotonic()
last_print = 0
reads = []
while time.monotonic() - t0 < DURATION:
    for name, cfg in CONFIGS.items():
        st = stats[name]
        apply(cfg)
        ts = time.monotonic()
        try:
            uid = nfc.poll(pn)
        except Exception as exc:
            st["err"] += 1
            print("err", name, repr(exc), flush=True)
            time.sleep(0.3)
            pn = nfc.open_reader(i2c)
            nfc.set_max_retries(pn)
            uid = None
        now = time.monotonic()
        st["t"] += now - ts
        st["n"] += 1
        if uid is not None:
            st["hit"] += 1
            st["miss_since"] = None
        else:
            # misses of THIS config only count against it; time measured on the wall clock
            if st["miss_since"] is None:
                st["miss_since"] = now
            st["max_streak"] = max(st["max_streak"], now - st["miss_since"])
    # every ~8s, time one full NDEF read with the fast 16-byte method
    if time.monotonic() - last_print > 8:
        last_print = time.monotonic()
        apply(CONFIGS["RF50  s30 r16"])
        uid = nfc.poll(pn)
        if uid is not None:
            ts = time.monotonic()
            try:
                msg = nfc.read_tag(pn, uid)
                txt = nfc.tag_text(msg) if msg else None
                reads.append((True, time.monotonic() - ts, (txt or "")[:40]))
            except Exception as exc:
                reads.append((False, time.monotonic() - ts, repr(exc)[:60]))
        print(f"{time.monotonic() - t0:4.0f}s  " + "  ".join(
            f"[{k}] {v['hit']}/{v['n']}" for k, v in stats.items()), flush=True)

print("\n=== summary (tag left on reader) ===")
print(f"{'config':16s} {'seen':>9s} {'rate':>5s} {'longest miss':>13s} {'avg poll':>9s} errors")
for k, v in stats.items():
    rate = v["hit"] / v["n"] if v["n"] else 0
    print(f"{k:16s} {v['hit']:4d}/{v['n']:<4d} {rate:5.0%} {v['max_streak']:12.1f}s "
          f"{v['t'] / max(v['n'], 1) * 1000:7.0f}ms {v['err']}")
print("\nfull tag reads (16-byte method):")
for ok, dt, info in reads:
    print(f"  {'OK  ' if ok else 'FAIL'} {dt:.2f}s  {info}")
