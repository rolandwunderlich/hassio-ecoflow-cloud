"""Diff SHP3 DisplayPropertyUpload fields between two captures and rank candidates.

Built to find the field that carries the mains CT readings once CTs are wired
to the SHP3 grid-sensor ports: capture before, capture after, and the field
that appears (or turns non-zero) and tracks load is the CT data.

Inputs are either HA logs with `custom_components.ecoflow_cloud.api.ecoflow_mqtt`
at DEBUG (lines "Message for <sn> and Topic ... : <hex>", any prefix, e.g. a
journal export), or capture files with one hex frame per line (last token;
`#` comments allowed, as in replay_frames.py). Reading the prod log means the
capture needs no second MQTT client: a second subscriber makes the broker
split delivery and starves the sparse fields.

Every field is read with a schema-free wire scan, so fields absent from the
DP3 proto (where a new CT field would be) still show up. Nested messages are
flattened to dotted paths (`1015.2`), repeats get an index (`1015[3].2`).
String values are reported by length only, so circuit labels stay out of the
output.

Usage:
  python reverse-eng/shp3_field_diff.py --before BEFORE --after AFTER [opts]
  python reverse-eng/shp3_field_diff.py --split-at "2026-07-11 14:00" LOG [opts]

Run from the repo root with the repo venv (it imports the integration's
header and XOR decode).
"""

import argparse
import json
import logging
import math
import re
import statistics
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ecoflow_cloud.devices.internal.proto import ef_dp3_iobroker_pb2 as dp3  # noqa: E402
from custom_components.ecoflow_cloud.devices.internal.smart_home_panel_3 import (  # noqa: E402
    F_GRID_L1_AMP,
    F_GRID_L1_PWR,
    F_GRID_L2_AMP,
    F_GRID_L2_PWR,
    F_GRID_PWR,
    F_L1_VOL,
    F_L2_VOL,
    F_LOAD_PWR,
    SmartHomePanel3,
)

# The ~120 s frame with grid status (619), reserve (461) and the settings; a
# CT field may travel only in it, so each side needs several of them.
F_FULL_MARKER = 619
FULL_PERIOD_S = 120
MIN_FULL_FRAMES = 5
SHIFT_MIN = 0.5  # median move, as a share of the before range, worth flagging

PROTO_NAMES = {f.number: f.name for f in dp3.DP3DisplayPropertyUpload.DESCRIPTOR.fields}
SHP3_DECODED = {
    F_GRID_PWR: "grid_pwr",
    F_LOAD_PWR: "load_pwr",
    F_GRID_L1_PWR: "grid_l1_pwr",
    F_GRID_L2_PWR: "grid_l2_pwr",
    F_L1_VOL: "l1_vol",
    F_L2_VOL: "l2_vol",
    F_GRID_L1_AMP: "grid_l1_amp",
    F_GRID_L2_AMP: "grid_l2_amp",
    **{n: "circuit label" for n in SmartHomePanel3.NAME_FIELDS},
    **{SmartHomePanel3.CIRCUIT_FIELD_BASE + i: f"circuit {i + 1}" for i in range(SmartHomePanel3.CIRCUITS)},
}

LOG_RE = re.compile(r"Message for (?P<sn>\S+) and Topic \S+ : (?P<hex>[0-9a-fA-F]+)\s*$")
TS_RE = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass
class Frame:
    ts: datetime | None
    values: dict[str, Any]  # path -> int | float | ("str", len) | ("bytes", len)


@dataclass
class Side:
    frames: list[Frame] = field(default_factory=list)
    skipped: int = 0

    @property
    def full(self) -> int:
        return sum(str(F_FULL_MARKER) in f.values for f in self.frames)

    def span_s(self) -> float | None:
        ts = [f.ts for f in self.frames if f.ts]
        return (max(ts) - min(ts)).total_seconds() if len(ts) > 1 else None


class WireError(ValueError):
    pass


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        if i >= len(b) or s > 63:
            raise WireError("truncated varint")
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def _strict_fields(b: bytes) -> list[tuple[int, int, Any]]:
    """Parse a whole buffer as protobuf or raise; [(field, wire, value)]."""
    out = []
    i = 0
    while i < len(b):
        tag, i = _varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            raise WireError("field 0")
        v: Any
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            if i + 8 > len(b):
                raise WireError("truncated f64")
            v, i = struct.unpack("<d", b[i : i + 8])[0], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            if i + ln > len(b):
                raise WireError("truncated len")
            v, i = b[i : i + ln], i + ln
        elif wt == 5:
            if i + 4 > len(b):
                raise WireError("truncated f32")
            v, i = struct.unpack("<f", b[i : i + 4])[0], i + 4
        else:
            raise WireError(f"wire type {wt}")
        out.append((fn, wt, v))
    return out


def flatten(b: bytes, prefix: str = "", depth: int = 0, out: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    fields = _strict_fields(b)
    counts: dict[int, int] = defaultdict(int)
    for fn, _, _ in fields:
        counts[fn] += 1
    seen: dict[int, int] = defaultdict(int)
    for fn, wt, v in fields:
        seen[fn] += 1
        key = f"{fn}[{seen[fn]}]" if counts[fn] > 1 else str(fn)
        path = f"{prefix}.{key}" if prefix else key
        if wt != 2:
            out[path] = v
            continue
        if v and depth < 3:
            try:
                flatten(v, path, depth + 1, out)
                continue
            except WireError:
                pass
        try:
            out[path] = ("str", len(v.decode("utf-8")))
        except UnicodeDecodeError:
            out[path] = ("bytes", len(v))
    return out


def _panel() -> SmartHomePanel3:
    dev = object.__new__(SmartHomePanel3)
    dev._meta = {}
    return dev


def decode(dev: SmartHomePanel3, raw: bytes) -> dict[str, Any] | None:
    """Flattened DisplayPropertyUpload (254/21) fields, or None for other frames."""
    hi = dev._decode_header_message(raw)
    if not hi or hi.get("cmdFunc") != 254 or hi.get("cmdId") != 21:
        return None
    pdata = dev._extract_payload_data(hi["header_obj"]) or b""
    pdata = dev._perform_xor_decode(pdata, hi)
    try:
        return flatten(pdata)
    except WireError:
        return None


def known(path: str) -> str:
    """What the integration already calls this path's top-level field, or ''."""
    top = int(path.split(".")[0].split("[")[0])
    return SHP3_DECODED.get(top) or PROTO_NAMES.get(top, "")


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("T", " ")) if s else None


def read_lines(path: Path, sn_prefix: str) -> list[tuple[datetime | None, str]]:
    """Frames from an HA log (only "Message for" lines count) or a hex capture."""
    lines = path.read_text(errors="replace").splitlines()
    rows = []
    if any("Message for " in line for line in lines):
        for line in lines:
            if "Message for " not in line or not (m := LOG_RE.search(line)):
                continue
            if m["sn"].startswith(sn_prefix):
                ts = TS_RE.search(line, 0, m.start())
                rows.append((_ts(ts[0] if ts else None), m["hex"]))
        return rows
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            tok = line.split()[-1]
            if HEX_RE.match(tok) and len(tok) % 2 == 0:
                rows.append((None, tok))
    return rows


def load(rows: list[tuple[datetime | None, str]], dev: SmartHomePanel3) -> Side:
    side = Side()
    for ts, hexs in rows:
        vals = decode(dev, bytes.fromhex(hexs))
        if vals is None:
            side.skipped += 1
        else:
            side.frames.append(Frame(ts, vals))
    return side


def split(rows: list[tuple[datetime | None, str]], at: str) -> tuple[list, list]:
    if at.isdigit():
        return rows[: int(at)], rows[int(at) :]
    cut = _ts(at)
    stamped = [(ts, h) for ts, h in rows if ts is not None]
    if cut is None or len(stamped) < len(rows):
        sys.exit("--split-at with a time needs a timestamped log; use a frame index")
    return [r for r in stamped if r[0] < cut], [r for r in stamped if r[0] >= cut]


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def stats(side: Side) -> dict[str, dict[str, Any]]:
    by: dict[str, list[Any]] = defaultdict(list)
    for f in side.frames:
        for p, v in f.values.items():
            by[p].append(v)
    out = {}
    for p, vs in by.items():
        nums = [x for x in (_num(v) for v in vs) if x is not None]
        out[p] = {
            "n": len(vs),
            "nonzero": sum(1 for x in nums if x != 0),
            "median": statistics.median(nums) if nums else None,
            "min": min(nums) if nums else None,
            "max": max(nums) if nums else None,
            "distinct": len({repr(v) for v in vs}),
            "kind": "num" if nums else (vs[0][0] if isinstance(vs[0], tuple) else "?"),
        }
    return out


def corr(side: Side, path: str, ref: str) -> tuple[float | None, int]:
    """Pearson r between a path and a reference path over frames carrying both."""
    xs, ys = [], []
    for f in side.frames:
        x, y = _num(f.values.get(path)), _num(f.values.get(ref))
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    if len(xs) < 5 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None, len(xs)
    return statistics.correlation(xs, ys), len(xs)


def rank(before: Side, after: Side, refs: list[str]) -> list[dict[str, Any]]:
    sb, sa = stats(before), stats(after)
    rows = []
    for p in sorted(
        set(sb) | set(sa), key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"[.\[\]]+", s) if x]
    ):
        b, a = sb.get(p), sa.get(p)
        if a and (not b or b["nonzero"] == 0) and a["nonzero"] > 0:
            change = "new" if not b else "zero->nonzero"
        elif b and not a:
            change = "gone"
        elif b and a and b["kind"] == a["kind"] == "num" and b["median"] != a["median"]:
            scale = max(abs(b["max"] - b["min"]), abs(b["median"]), 1e-9)
            moved = abs(a["median"] - b["median"]) / scale
            change = f"shift {moved:.2f}" if moved >= SHIFT_MIN else ""
        else:
            change = ""
        rs = {r: corr(after, p, r) for r in refs if r != p}
        best = max((abs(r) for r, _ in rs.values() if r is not None), default=0.0)
        rows.append(
            {"path": p, "known": known(p), "change": change, "before": b, "after": a, "corr": rs, "best_r": best}
        )

    # Unknown fields that appear rank first; already-decoded ones (circuits
    # switching on look "new" too) after them.
    def score(r: dict[str, Any]) -> tuple[int, int, float]:
        tier = 0 if r["change"] in ("new", "zero->nonzero") else 1 if r["change"].startswith("shift") else 2
        return (tier, bool(r["known"]), -r["best_r"])

    return sorted(rows, key=score)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def report(before: Side, after: Side, rows: list[dict[str, Any]], refs: list[str], top: int) -> None:
    for name, side in (("before", before), ("after", after)):
        span = side.span_s()
        print(
            f"{name}: {len(side.frames)} frames (254/21), {side.skipped} other, "
            f"{side.full} full frames (field {F_FULL_MARKER}), span {_fmt(span)} s"
        )
        if side.full < MIN_FULL_FRAMES:
            print(
                f"  WARNING: fewer than {MIN_FULL_FRAMES} full frames; a CT field sent only every "
                f"{FULL_PERIOD_S} s may be missed. Capture at least {MIN_FULL_FRAMES * FULL_PERIOD_S // 60} min."
            )
    flagged = [r for r in rows if r["change"]]
    print(f"\n{len(rows)} paths, {len(flagged)} changed. Correlation refs (after side): {', '.join(refs)}\n")
    head = f"{'path':<18} {'known as':<22} {'change':<14} {'n b/a':>11} {'nz b/a':>11} {'median before':>14} {'median after':>14} {'max after':>12}"
    print(head + "".join(f" {'r~' + r:>10}" for r in refs))
    for r in (flagged + [r for r in rows if not r["change"]])[:top]:
        b, a = r["before"] or {}, r["after"] or {}
        line = (
            f"{r['path']:<18} {r['known'][:22]:<22} {r['change']:<14} {_fmt(b.get('n')) + '/' + _fmt(a.get('n')):>11} "
            f"{_fmt(b.get('nonzero')) + '/' + _fmt(a.get('nonzero')):>11} {_fmt(b.get('median')):>14} "
            f"{_fmt(a.get('median')):>14} {_fmt(a.get('max')):>12}"
        )
        for ref in refs:
            v, n = r["corr"].get(ref, (None, 0))
            line += f" {(f'{v:+.2f}/{n}' if v is not None else '-'):>10}"
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log", nargs="?", type=Path, help="single log/capture to split with --split-at")
    ap.add_argument("--before", type=Path)
    ap.add_argument("--after", type=Path)
    ap.add_argument("--split-at", help="'YYYY-MM-DD HH:MM[:SS]' or a frame index")
    ap.add_argument("--sn-prefix", default="HR63", help="SHP3 serial prefix in log lines (default HR63)")
    ap.add_argument(
        "--ref",
        action="append",
        help=f"path to correlate against (repeatable; default {F_GRID_PWR} grid, {F_LOAD_PWR} load)",
    )
    ap.add_argument("--top", type=int, default=40, help="rows to print (default 40)")
    ap.add_argument("--json", type=Path, help="write the full ranking here")
    args = ap.parse_args()
    logging.basicConfig(level=logging.CRITICAL)  # the decode's per-frame errors are noise here

    if args.log and args.split_at:
        b_rows, a_rows = split(read_lines(args.log, args.sn_prefix), args.split_at)
    elif args.before and args.after:
        b_rows, a_rows = read_lines(args.before, args.sn_prefix), read_lines(args.after, args.sn_prefix)
    else:
        ap.error("give --before and --after, or one LOG with --split-at")

    dev = _panel()
    before, after = load(b_rows, dev), load(a_rows, dev)
    refs = args.ref or [str(F_GRID_PWR), str(F_LOAD_PWR)]
    rows = rank(before, after, refs)
    report(before, after, rows, refs, args.top)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1, default=str))


if __name__ == "__main__":
    main()
