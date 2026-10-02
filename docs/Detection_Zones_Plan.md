# Detection Zones Plan (build plan C17)

Status: requirements from CrystalHeeler, 2026-10-02. Not designed in detail, not built.
No version assigned.

## Why

A garage door opening in the far corner of the ch4 picture did not record
(2026-10-02, log 10:52 to 10:58). The door is about 0.2% of the picture. The
most sensitive daytime setting needs 1.0%, and the still scene already
reaches 0.1% to 0.5% a minute, so a lower limit for the whole picture would
record noise. A zone measures change against the zone's own area.

## Requirements (CrystalHeeler, 2026-10-02)

### Drawing

1. A separate drawing window, overlaid on the Enhanced View live feed.
2. A zone is a polygon made of straight lines.
3. The first click starts a line at that point. The line follows the mouse.
   Each further click sets an anchor point, and a new line starts from it.
   This continues until the user closes the loop.
4. A double click at any time leaves drawing. The lines drawn so far are
   saved, but no zone is set or active.
5. The user resumes drawing by clicking in the window again, and the zone
   becomes active when the loop is closed.
6. The anchor points of a polygon can be moved, to adjust a drawn polygon.
7. Lines per polygon: no limit, unless a limit is needed for a technical
   reason; then 10 or 15.
8. At most 6 polygons for each camera.
9. When a polygon is complete, the user is asked for a name (free text).

### Detection

10. Each polygon has its own sensitivity setting.
11. The camera's current sensitivity setting applies only to the area
    outside the zones.
12. When a camera has at least one polygon, its detection menu shows a new
    switch: "Detection in zones only", off or on.

## Open points for the design discussion

CrystalHeeler: "I'm sure there's stuff I'm forgetting to add." Known open points:

- How the user closes the loop: a click on the first anchor point, or a
  click near it (how near), or a key.
- How the user deletes a polygon, renames it, or removes one anchor point.
- Touch screens (phone, tablet): there is no mouse movement and a double
  tap zooms in some browsers.
- Overlapping polygons: which sensitivity applies where two overlap.
- Where a zone's sensitivity is set: in the drawing window, or in the cog
  panel's list of zones.
- What the tuning line and the cog's live reading show for each zone.
- Night boost: whether the +15 applies to each zone's setting.
- Slow movement inside a zone (a garage door takes several seconds):
  whether a zone also compares with a picture about 5 s old.
- The confirmation rule (a second changed picture within 0.5 s) and the
  light-change rule inside a small zone.
- Recording names: whether the zone's name goes into the file name or the
  log line.
- The detection grid is 64 x 48 cells. A polygon is stored in picture
  coordinates (0 to 1) and turned into a set of cells; a very small zone
  covers only a few cells. Whether to raise the grid for cameras with zones.
- Storage: zones go in `/data/motion.json` with the camera's other settings.
- The drawing window uses the Enhanced View live feed; a camera that only
  has snapshots needs a still picture to draw on.

## Technical notes

- No technical reason is known today to limit the lines in a polygon. The
  polygon is turned into a cell mask once, when it is saved, not on every
  picture.
- Detection cost per picture does not grow with the number of points; it
  grows a little with the number of zones (one count per zone).
