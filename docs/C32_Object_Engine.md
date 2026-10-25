# The object engine (C32), 4.0.0

A second motion engine, chosen per camera in the Cog panel (Motion engine: Classic or Objects). The classic
engine stays the default. CrystalHeeler decided the design on 2026-10-24 and ordered the build on 2026-10-25
(an unattended run: 3.9.0, 4.0.0, 4.1.0).

## The decisions (2026-10-24)

1. Everything from the research except the person/vehicle/animal classifier, which comes later, with per-camera
   toggles per object type and the type in file names.
2. Any moving object records, subject to a minimum size, only where detection is set (the whole picture, or
   zones).
3. The sensitivity slider becomes each zone's minimum object size.
4. An object belongs to the zone holding most of it, at its feet. Only a clear move into another zone switches
   the recording to that zone.
5. A confirmed object is followed into the next zone below that zone's size, for up to 3 s unseen (B75).
6. The light pause and the brightness test (C30) and the slow-movement check stay.
7. A per-camera choice between the two engines.
8. When a camera can only be judged from still pictures, the page warns that its detection is reduced.

## How it works

Each live picture (4 a second) is compared with the one 1 s older, as in the classic engine: the same changed
cells, the same light rule (75% of the picture's areas), the same headlight pause, light hold, 1.25 s look before
a recording and colour switch (3.9.0).

1. **Objects.** The changed cells are grouped: cells up to 2 cells apart belong to one object
   (`anycam_objects.find_objects`). Each object has its size, its box, its feet (the bottom middle of the box), how
   much of it got brighter, and its fill (cells / box).
2. **Where.** The zone at its feet; when its feet are in no zone, the zone holding most of its cells; else outside
   the zones. An object in an Off zone, or outside the zones on a camera that detects in zones only, does not
   count.
3. **Size.** The smallest object that counts is set by the zone's sensitivity (outside the zones: the camera's),
   plus the night boost: 10% of the picture at 1, 0.36% at 63, 0.085% at 90, 0.05% at 100, down to 0.02% with the
   boost (`anycam_objects.obj_min_pct`).
4. **Not a body.** An object that is one-sided (90% brighter or darker), lit by more than 25 grey levels and
   scattered (fill 0.35 or less) is a lit surface. At night, an object that is one-sided, solid (fill 0.55 or more)
   and under 1% of the picture is an insect in the infrared: brighter where it is, darker where it was 1 s before.
5. **Tracking.** An object that counts starts or feeds a track: the nearest track within a quarter of the picture's
   width, or whose box (grown by 4 cells) overlaps. A track is confirmed once it has 3 pictures and its centre has
   moved 3% of the picture's width. A lamp switching on stays still and is never confirmed.
6. **Recording.** A confirmed object starts a recording (after the 1.25 s look); the zone at its feet names the
   file. While recording, any confirmed object keeps it going, below any size and in any zone, up to 3 s unseen
   (B75). The zone list (C31) gains a zone when an object's feet stay in it for two pictures.
7. **Lights.** A light change, a colour switch or a light pause drops every track, and the object engine holds
   3 s after a light change (the classic engine 2 s): the picture settling looks like a shrinking object.
8. **Reduced detection.** A camera whose live stream does not reach the detector is judged from still pictures,
   about one a second. The Cog panel says so, and its card's record button shows a warning sign.

All of it is pure Python; numpy was allowed but is not needed at 128 x 96 cells.

## The replay (25 Oct)

The add-on's own motion path (`_motion_feed`) replayed 409 recordings, with each camera's zones and sensitivity
from the settings copy of 24 Oct 22:42: every first file of 20–24 Oct on the patio camera (ch7) and every file of
the night of 24–25 Oct. The classic column is 3.9.0.

| Recordings | Classic | Objects |
|---|---|---|
| Headlights (29) | 0 | 2 |
| Insects (10) | 10 | 8 |
| Motion-sensitive light (8) | 0 | 0 |
| Dawn switch (2) | 0 | 0 |
| A person (6) | 6 | 6 |
| The cat (1) | 1 | 1 |
| By day, unlabelled (228) | 211 | 217 |
| At night, unlabelled (122) | 20 | 21 |

The 15 unlabelled files where the engines disagree were viewed:
- **Classic only (4):** an insect near the lens (2), nothing visible (2).
- **Objects only (11):** people at the far side of the picture that the classic engine missed (3), headlights (2),
  nothing visible (6, by day: moving shadows or leaves).

**What the tuning found.**
- The first version recorded the insects in all 10 files. Their twin (darker where the insect was 1 s before)
  formed its own track; the insect test now covers both sides.
- 2 insect files still record. When the insect and its twin are within 2 cells, they form one half-bright,
  half-dark object that looks like a small person.
- Two headlight files recorded when the picture settled after a light change: the 3 s hold.

**Not tested.**
- The live detector on the Pi (the replay decoded files with OpenCV), and its time per picture with several
  cameras on the object engine.
- People at night far from the camera: none in the labelled set.
- A white cat dashing past at night may look like an insect (small, solid, brighter only).
