"""Detection zones (3.4.0, build plan C17): geometry, checks and judging.

A zone is a polygon drawn over a camera's picture. Motion inside it is
measured against the zone's own area, with the zone's own sensitivity, so a
garage door that is 0.2% of the picture can record without the whole
picture becoming so sensitive that it records noise. The 23 answers
CrystalHeeler approved on 2026-10-04 are in docs/Detection_Zones_Plan.md;
the numbers in the comments below are those answers.

This file holds no camera state and imports no other AnyCam file.
anycam_motion.py keeps the zones in /data/motion.json (answer 17) and calls
the functions here.
"""

ZONE_MAX = 6                 # zones for each camera (requirement 8)
ZONE_NAME_MAX = 40           # answer 7
ZONE_POINTS_MAX = 100        # input limit only; detection cost does not grow with points
ZONE_GRID = (128, 96)        # a camera with zones is judged on this grid (answer 8)
ZONE_MIN_CELLS = 24          # the page warns under this (answer 8)
ZONE_SLOW_S = 5.0            # the slow comparison's picture age (answer 11)

ZONES: dict[str, dict] = {}  # camera_id -> {"zones": [...], "zones_only": bool}
# Recording file base name -> the zone that started it (answer 15), for the
# Storage tab; kept in /data/recording_zones.json, newest REC_ZONES_MAX.
REC_ZONES: dict[str, str] = {}
REC_ZONES_MAX = 2000
_MASKS: dict[tuple, list] = {}   # (camera_id, grid) -> [(zone, cells)], built once


def zone_cfg(camera_id: str) -> dict:
    """The camera's zone settings, empty when it has none."""
    cfg = ZONES.get(camera_id) or {}
    return {"zones": list(cfg.get("zones") or []), "zones_only": bool(cfg.get("zones_only"))}


def has_zones(camera_id: str) -> bool:
    """True when the camera has at least one closed zone: it is judged by zones."""
    return any(z.get("closed") for z in zone_cfg(camera_id)["zones"])


def zones_only(camera_id: str) -> bool:
    return has_zones(camera_id) and zone_cfg(camera_id)["zones_only"]


def set_zones(camera_id: str, cfg: dict | None) -> None:
    """Replace the camera's zones; None or no zones removes them."""
    if cfg and cfg.get("zones"):
        ZONES[camera_id] = cfg
    else:
        ZONES.pop(camera_id, None)
    for key in [k for k in _MASKS if k[0] == camera_id]:
        del _MASKS[key]


# ── geometry ─────────────────────────────────────────────────────────────────
def _orient(a: tuple, b: tuple, c: tuple) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1: tuple, p2: tuple, p3: tuple, p4: tuple) -> bool:
    """True when segment p1-p2 crosses segment p3-p4 (touching counts)."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return True

    def on(a: tuple, b: tuple, c: tuple) -> bool:     # c on segment a-b, given collinear
        return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
    return ((d1 == 0 and on(p3, p4, p1)) or (d2 == 0 and on(p3, p4, p2))
            or (d3 == 0 and on(p1, p2, p3)) or (d4 == 0 and on(p1, p2, p4)))


def self_crossing(points: list) -> bool:
    """True when two sides of the closed polygon cross (answer 6)."""
    n = len(points)
    sides = [(tuple(points[i]), tuple(points[(i + 1) % n])) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue                      # neighbours share a corner
            if segments_cross(*sides[i], *sides[j]):
                return True
    return False


def point_in_polygon(x: float, y: float, points: list) -> bool:
    inside = False
    n = len(points)
    for i in range(n):
        (x1, y1), (x2, y2) = points[i], points[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def polygon_cells(points: list, grid: tuple) -> list[int]:
    """The grid cells whose centres are inside the polygon."""
    gw, gh = grid
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, x1 = max(0, int(min(xs) * gw)), min(gw - 1, int(max(xs) * gw))
    y0, y1 = max(0, int(min(ys) * gh)), min(gh - 1, int(max(ys) * gh))
    return [y * gw + x for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
            if point_in_polygon((x + 0.5) / gw, (y + 0.5) / gh, points)]


# ── checks at the boundary ───────────────────────────────────────────────────
def validate(data: object) -> tuple[dict, list[str]]:
    """Check a camera's zones from the page; return (clean, errors)."""
    errors: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("zones"), list):
        return {}, ["zones must be a list"]
    raw = data["zones"]
    if len(raw) > ZONE_MAX:
        return {}, [f"At most {ZONE_MAX} zones for each camera"]
    clean, names = [], set()
    for i, z in enumerate(raw, 1):
        if not isinstance(z, dict):
            errors.append(f"Zone {i} is not a zone")
            continue
        name = str(z.get("name") or "").strip()
        if not name or len(name) > ZONE_NAME_MAX:
            errors.append(f"Zone {i}: a name of 1 to {ZONE_NAME_MAX} characters")
        elif name.lower() in names:
            errors.append(f"Zone names must be different: {name}")
        names.add(name.lower())
        pts = z.get("points")
        if (not isinstance(pts, list) or len(pts) > ZONE_POINTS_MAX
                or not all(isinstance(p, (list, tuple)) and len(p) == 2
                           and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in p)
                           for p in pts)):
            errors.append(f"{name or f'Zone {i}'}: points must be up to {ZONE_POINTS_MAX} "
                          f"pairs from 0 to 1")
            continue
        try:
            level = int(z.get("level", 63))
        except (TypeError, ValueError):
            level = -1
        if not 0 <= level <= 100:
            errors.append(f"{name}: sensitivity must be Off (0) or 1 to 100")
        closed = bool(z.get("closed"))
        points = [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in pts]
        if closed and len(points) < 3:
            errors.append(f"{name}: a closed zone needs at least 3 points")
        elif closed and self_crossing(points):
            errors.append(f"{name}: two of its lines cross; move a point")
        clean.append({"name": name, "points": points, "level": level, "closed": closed})
    return {"zones": clean, "zones_only": bool(data.get("zones_only"))}, errors


# ── judging ──────────────────────────────────────────────────────────────────
def masks(camera_id: str, grid: tuple) -> list[tuple[dict, list[int]]]:
    """Each closed zone with its cells on the grid, built once per change."""
    key = (camera_id, tuple(grid))
    if key not in _MASKS:
        _MASKS[key] = [(z, polygon_cells(z["points"], grid))
                       for z in zone_cfg(camera_id)["zones"] if z.get("closed")]
    return _MASKS[key]


def outside_cells(camera_id: str, grid: tuple) -> list[int]:
    """The cells in no closed zone (answer 4: an Off zone masks its cells too)."""
    key = (camera_id, tuple(grid), "outside")
    if key not in _MASKS:
        inside = set()
        for _z, cells in masks(camera_id, grid):
            inside.update(cells)
        _MASKS[key] = [i for i in range(grid[0] * grid[1]) if i not in inside]
    return _MASKS[key]


def evaluate(camera_id: str, flags: bytes, grid: tuple, outside_pct: float,
             level_pct: "callable") -> list[tuple[str | None, float, float]]:
    """(zone name or None for outside, % changed, % needed) for each judged area.

    flags: one byte per grid cell, 1 when the cell changed. level_pct turns
    a zone's sensitivity into the % its area must change; the night boost
    is applied there (answer 12). An Off zone is never judged; outside is
    not judged when the camera detects in zones only (answer 13).
    """
    out: list[tuple[str | None, float, float]] = []
    for z, cells in masks(camera_id, grid):
        if z["level"] <= 0 or not cells:
            continue
        pct = 100.0 * sum(flags[i] for i in cells) / len(cells)
        out.append((z["name"], pct, level_pct(z["level"])))
    if not zones_only(camera_id):
        cells = outside_cells(camera_id, grid)
        if cells:
            pct = 100.0 * sum(flags[i] for i in cells) / len(cells)
            out.append((None, pct, outside_pct))
    return out


def rec_zone_for(filename: str) -> str | None:
    """The zone that started the recording in this file, if one did."""
    import re
    return REC_ZONES.get(re.sub(r"(_part\d+)?\.mp4$", "", filename))


def best_pass(results: list[tuple[str | None, float, float]]) -> tuple[str | None, float] | None:
    """The area that passed by the widest margin, or None when none passed."""
    passed = [(pct / need, name, pct) for name, pct, need in results if need > 0 and pct >= need]
    if not passed:
        return None
    _, name, pct = max(passed, key=lambda p: p[0])
    return name, pct
