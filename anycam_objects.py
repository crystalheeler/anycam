"""4.0.0 (C32): the object engine — changed cells grouped into objects, followed over time.

CrystalHeeler's decisions of 2026-10-24 (docs/BUILD_PLAN.md, C32): any moving
object records, only where detection is set; the sensitivity slider sets the
smallest object; an object belongs to the zone at its feet; a confirmed object
is followed into the next zone for up to 3 s unseen.

The classic engine counts changed cells per zone, so an insect near the lens,
a passing light or a far-away person can all reach a zone's share. This engine
groups the changed cells of each comparison (a picture against the one 1 s
older) into objects, follows each object from picture to picture, and counts it
only once it has been seen in OBJ_CONFIRM_PICTURES pictures and has moved.

This file has no AnyCam imports: anycam_motion.py calls it with the changed
cells and the two pictures, and keeps the result in the camera's motion state.
Pure Python: at 128 x 96 cells a comparison with a person in it groups a few
hundred cells.
"""
import math

OBJ_LINK = 2                  # changed cells this many cells apart belong to one object
OBJ_MAX_CELLS = 0.30          # more changed than this share of the picture: one object, not grouped
OBJ_CONFIRM_PICTURES = 3      # an object counts once seen in this many pictures ...
OBJ_MIN_TRAVEL = 0.03         # ... and moved this share of the picture's width
OBJ_FOLLOW_S = 3.0            # a confirmed object is followed this long unseen (B75)
OBJ_DROP_S = 1.0              # an unconfirmed one is forgotten after this long unseen
OBJ_MATCH = 0.25              # an object this share of the width from a track's last place is the same one
OBJ_PAD = 4                   # ... or when their boxes, grown by this many cells, overlap
OBJ_TRACKS_MAX = 12           # tracks kept per camera


def find_objects(flags: bytes, prev: bytes, curr: bytes, grid: tuple[int, int]) -> list[dict]:
    """Group the changed cells into objects; describe each one.

    flags: one byte per cell, non-zero where the comparison changed.
    prev, curr: the two grey pictures of the comparison (same size as flags).
    """
    gw, gh = grid
    idx = [i for i, f in enumerate(flags) if f]
    if not idx:
        return []
    if len(idx) > OBJ_MAX_CELLS * gw * gh:
        return [_describe(idx, prev, curr, gw)]
    todo = set(idx)
    objects = []
    for start in idx:
        if start not in todo:
            continue
        todo.discard(start)
        stack, cells = [start], []
        while stack:
            c = stack.pop()
            cells.append(c)
            y, x = divmod(c, gw)
            for yy in range(max(0, y - OBJ_LINK), min(gh, y + OBJ_LINK + 1)):
                row = yy * gw
                for xx in range(max(0, x - OBJ_LINK), min(gw, x + OBJ_LINK + 1)):
                    n = row + xx
                    if n in todo:
                        todo.discard(n)
                        stack.append(n)
        objects.append(_describe(cells, prev, curr, gw))
    objects.sort(key=lambda o: -o["n"])
    return objects


def _describe(cells: list[int], prev: bytes, curr: bytes, gw: int) -> dict:
    xs = [c % gw for c in cells]
    ys = [c // gw for c in cells]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    deltas = [curr[c] - prev[c] for c in cells]
    n = len(cells)
    cx = sum(xs) / n
    return {"n": n, "box": (x0, y0, x1, y1), "cx": cx, "cy": sum(ys) / n,
            "foot": (int(round(cx)), y1),
            "bright": sum(1 for d in deltas if d > 0) / n,
            "delta": sum(deltas) / n,
            "fill": n / ((x1 - x0 + 1) * (y1 - y0 + 1)),
            "cells": cells}


def _near(track: dict, obj: dict, gw: int) -> float | None:
    """Distance from a track's last place to an object, or None when too far."""
    last = track["last"]
    dist = math.hypot(obj["cx"] - last["cx"], obj["cy"] - last["cy"])
    if dist <= OBJ_MATCH * gw:
        return dist
    ax0, ay0, ax1, ay1 = last["box"]
    bx0, by0, bx1, by1 = obj["box"]
    if (ax0 - OBJ_PAD <= bx1 and bx0 <= ax1 + OBJ_PAD
            and ay0 - OBJ_PAD <= by1 and by0 <= ay1 + OBJ_PAD):
        return dist
    return None


def track(state: dict, objects: list[dict], t: float, grid: tuple[int, int],
          counts=lambda obj: True) -> list[dict]:
    """Follow the objects of one comparison; return the tracks seen in it.

    state: the camera's tracker state (a dict kept between calls).
    counts(obj): whether an object may start or feed a track that is not yet
    confirmed (big enough, where detection is set). A confirmed track takes
    any object it matches, so it is followed below a zone's size (B75).
    """
    gw = grid[0]
    tracks = state.setdefault("tracks", [])
    if state.get("grid") != grid:
        tracks.clear()
        state["grid"] = grid
    tracks[:] = [tr for tr in tracks
                 if t - tr["t_last"] <= (OBJ_FOLLOW_S if tr["confirmed"] else OBJ_DROP_S)
                 and tr["t_last"] <= t]
    seen, used = [], set()
    for obj in objects:
        best, best_d = None, None
        for tr in tracks:
            if id(tr) in used:
                continue
            d = _near(tr, obj, gw)
            if d is not None and (best_d is None or d < best_d):
                best, best_d = tr, d
        if best is None:
            if not counts(obj) or len(tracks) >= OBJ_TRACKS_MAX:
                continue
            state["next_id"] = state.get("next_id", 0) + 1
            best = {"id": state["next_id"], "t0": t, "first": obj, "path": [], "confirmed": False,
                    "t_conf": None, "travel": 0.0, "n_max": 0}
            tracks.append(best)
        elif not best["confirmed"] and not counts(obj):
            continue
        used.add(id(best))
        best["last"], best["t_last"] = obj, t
        best["path"].append((t, obj["cx"], obj["cy"], obj["n"], obj["bright"], obj["fill"]))
        del best["path"][:-16]
        best["n_max"] = max(best["n_max"], obj["n"])
        f = best["first"]
        best["travel"] = max(best["travel"],
                             math.hypot(obj["cx"] - f["cx"], obj["cy"] - f["cy"]) / gw)
        if (not best["confirmed"] and len(best["path"]) >= OBJ_CONFIRM_PICTURES
                and best["travel"] >= OBJ_MIN_TRAVEL):
            best["confirmed"], best["t_conf"] = True, t
        seen.append(best)
    return seen


def obj_min_pct(level: float) -> float:
    """Sensitivity -> the smallest object that records, in % of the picture.

    10% at 1 down to 0.05% at 100, on the same kind of curve as the classic
    engine's share; the night boost can take the level past 100 (to 0.02%).
    """
    return max(0.02, 10.0 * (0.005 ** ((min(level, 120) - 1) / 99.0)))
