# Detection Zones Plan (build plan C17)

Status: requirements from CrystalHeeler, 2026-10-02; all 23 answers approved 2026-10-04. Not built.
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

## Proposed answers (for CrystalHeeler's review, 2026-10-04)

Answers 1 to 20 approved by CrystalHeeler on 2026-10-04 ("All of that sounds great"). Answers 21 to 23 added the same day, after the question how zone editing is opened, and approved ("Let's do it all!").

**Drawing**

1. **Closing the loop.** A click on the first anchor point closes the polygon.
   The first point is drawn larger and lights up when the pointer is within
   12 screen pixels of it; a click there snaps to it. The Enter key also
   closes the loop. A polygon needs at least 3 points.
2. **Deleting, renaming, removing a point.** The drawing window has a zone
   list beside the picture: each zone with its name, its sensitivity, a
   rename button and a delete button (delete asks to confirm). A point is
   removed with a right-click on it (long press on touch). A point is added
   by dragging the small handle in the middle of a line. Backspace, or an
   Undo button, removes the last point while drawing.
3. **Touch screens.** A tap sets a point; the line to the next point shows
   from the last tap. "Finish" and "Close shape" buttons replace the double
   click and the click on the first point. Pinch-zoom and double-tap zoom are
   switched off inside the drawing area only.
4. **Overlapping polygons.** Each zone is judged on its own cells, so a cell
   inside two zones counts for both; no precedence is needed. "Outside the
   zones" means the cells in no zone.
5. **Where a zone's sensitivity is set.** In the drawing window's zone list,
   and in the cog panel's zone list. Both edit the same value, on the
   existing 1 to 100 scale, measured against the zone's own area. The scale
   adds "Off": an Off zone never records, so a zone can mask a tree or a
   flag.
6. **Self-crossing lines.** A polygon whose lines cross is refused when it is
   closed, with a message; the user moves a point.
7. **Names.** The prompt suggests "Zone 1", "Zone 2"; names are unique for
   each camera, at most 40 characters.

**Detection**

8. **Grid resolution.** At 64 x 48 cells a 0.2% zone covers 6 cells, too few
   to tell a door from noise. A camera with at least one zone is detected at
   128 x 96 (4 times the cells; 12 KB a picture at 4 pictures a second, a
   negligible cost). The drawing window warns when a zone covers fewer than
   24 cells. The whole-picture levels are checked again at 128 x 96, because
   the noise per cell changes.
9. **Confirmation rule inside a zone.** The same rule as the whole picture: a
   second changed picture within 0.5 s, counted in the same zone. The
   one-picture 3% rule is off inside zones: in a small zone it is one or two
   cells, which an insect can cover.
10. **Light-change rule.** Judged on the whole picture only, as today, never
   on one zone. A zone drawn tightly around a garage door changes almost
   completely when the door opens; a zone rule would call that light.
11. **Slow movement (a garage door).** Each zone also compares with the
   picture about 5 s old. That comparison counts only when it passes the
   zone's level on two pictures in a row, so a passing insect does not.
12. **Night boost.** The +15 applies to each zone's setting as well, as to
   the camera's.
13. **Detection in zones only.** When on, the area outside the zones is not
   checked at all. When off, it uses the camera's own setting (requirement
   11).

**What the user sees**

14. **Tuning line and live reading.** One entry for each zone and one for
   "outside the zones": for example "Garage: peak 3.1% (records at 2.0%)".
15. **Recording names.** The file name stays as today
   (`LorexCH4_<date>_<time>.mp4`). The zone's name goes into the log line
   and into the recording's entry in the Storage tab. Free-text names would
   make file names long and need cleaning of unsafe characters.
16. **Zone outlines.** Enhanced View gets a "Show zones" switch that draws the
   outlines over the picture; the zone that started a recording is marked.

**Storage and edge cases**

17. **Storage.** In `/data/motion.json` with the camera's other settings:
   for each zone its name, its points in picture coordinates (0 to 1), its
   level, and whether it is closed. An open (unclosed) polygon is saved and
   never detects (requirement 4).
18. **Drawing on a still picture.** The drawing window opens on the live
   feed (requirement 1) with a Pause button, because points are easier to
   place on a still picture. A camera with snapshots only draws on its
   latest picture.
19. **Stream changes.** Picture coordinates do not depend on resolution, so a
   zone holds when AnyCam switches between main and sub-stream, if both show
   the same view.
20. **The PTZ camera.** A zone belongs to one view. When the Hikvision PTZ is
   moved, its zones point at the wrong place; the cog panel says so. Zones
   for each preset position are not proposed.

**Opening and leaving zone editing**

21. **Opening.** Two ways: a "Zones" button in Enhanced View's bottom bar
   opens the drawing window over the camera being watched; an "Edit zones"
   button in the cog panel's motion section opens Enhanced View for that
   camera directly in zone editing. A camera with snapshots only has the
   same button and draws on its latest picture (answer 18).
22. **Inside zone editing.** A click on the picture outside any zone starts a
   new zone (requirement 3); a "+ New zone" button in the zone list does the
   same and is greyed out at 6 zones. A click on an existing zone selects it
   for moving, adding or removing points. A double click leaves the drawing
   of the current zone and stays in zone editing (requirement 4).
23. **Leaving.** "Done" saves all changes and returns to Enhanced View.
   "Cancel" discards every change made since zone editing was opened, after
   a confirmation when there are changes. Esc stops drawing the current
   zone only and never closes the window.

## Technical notes

- No technical reason is known today to limit the lines in a polygon. The
  polygon is turned into a cell mask once, when it is saved, not on every
  picture.
- Detection cost per picture does not grow with the number of points; it
  grows a little with the number of zones (one count per zone).
