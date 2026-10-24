# Recording review, 2026-10-24

CrystalHeeler sent 11 recordings from the Lorex DVR's front channel (test system B, 3.8.3), all inside the
add-on log of 2026-10-22 17:37 to 2026-10-23 12:37, for four problems: insects, light changes, vehicle
headlights, and objects that move between zones. Each clip was decoded frame by frame in order and matched
to its log lines. The camera's on-screen clock runs 2–4 s behind the add-on log.

Zone settings in force (sensitivity slider; the night boost adds 15):

| Zone | Slider | Needs by day | Needs at night |
|---|---|---|---|
| FrontYard-Right, Patio | 95 | 1.24% | 0.65% |
| FrontYard-Left | 90 | 1.54% | 0.80% |
| Walkway, NeighborSideYard | 50 | 8.79% | 4.58% |

A zone's threshold is a share of the zone's own cells (128 x 96 grid), from
`74 * (1/74) ** ((level - 1) / 99)` percent.

## 1. Vehicle headlights (C30) — the largest group

- **Clips:** 4 from the left (18:21:58 at dusk in colour, 20:55:25, 21:08:35, 23:24:00) and 1 from the right
  (05:50:13).
- **How they record.** A car turning on the street lights the lawns and rakes the porch posts' shadows
  across them. The beam reaches FrontYard-Left first, so its zone starts the recording, and in the same
  second FrontYard-Right, NeighborSideYard, Walkway and Patio change too. The change covers 1–10% of the
  picture and at most 38% of its 16 areas, far below the 75% light rule.
- **Measured signature.** The same comparison as the add-on was used: 128 x 96 grey, normalised, each
  picture against the one 1 s older.
  - At the moment each left clip triggered, 90–100% of the changed cells got brighter, by +37 to +46 grey
    levels.
  - As the beam swept on, the changed cells were mixed (moving shadows). As it left, they were mostly darker.
  - A walking person or a cat is mixed throughout: 36–58% brighter, mean change about ±10.
- **Share.** 14 of the 25 night recordings in the log started in FrontYard-Left with FrontYard-Right passing
  within 2.5 s, the pattern of every left headlight clip. A person cannot be on both lawns, either side of
  the walkway, in the same second.
- **The right-side clip (05:50:13)** is the car itself passing on the street at the top right: about 1% of
  the picture, mixed brighter and darker. A brightness test does not catch it. The NeighborSideYard zone
  reaching the street is the likely cause.
- **No project found by the research has a brighten-only test** (section 6). It needs a field test.

## 2. Light changes (B73, B77)

- **Clip 18:22:50** (FrontYard-Left, 10 s): black-and-white from the first frame, brightness settling, no
  moving object. CrystalHeeler's guess was right: it recorded just after the day-to-night switch.
- **Log.** At 18:22:45 three comparisons were treated as a light change (81–88% of the areas), and the
  normal checks ignored the next 2 s. At 18:22:50 the slow-movement check (each picture against the one 5 s
  older) compared with a picture from before the switch and started the recording. This is B73.
- **Morning.** The night-to-day light change (06:39:42) recorded nothing, which matches what CrystalHeeler
  saw in the videos.
- **The other dusk recording (18:21:58)** was headlights, not the switch.

## 3. Insects (B76, B78)

- **Clip 19:48:59** (FrontYard-Right, 10 s): a faint, blurred, infrared-lit streak near the bird feeder at
  the top right, 10–11 cells (0.08% of the picture), in a different place in each picture.
- **Log.** At 19:48:54 the fast checks rejected it ("changed in one picture only"). Five seconds later the
  slow-movement check compared two new pictures with that 5 s old picture, which held the insect. Both
  "differed", so it recorded. The insect in the old reference picture made the recording, not anything new.
  This is B78.

## 4. Objects moving between zones (B75, C31)

- **The man (07:51:42) and the boy (17:40:36).** Every zone they walked through passed its threshold: the
  man in FrontYard-Left 7.0%, Walkway 37.4%, FrontYard-Right 13.8% and Patio 43.5%. Once a recording runs, a
  pass in any zone keeps it going, so it stays one file, named after the first zone only. Detection worked;
  the name hides it.
- **The boy far away (17:42:11–17:43:28).** At the tree, FrontYard-Right saw 0.3–0.9% against 1.2% needed,
  five times. A small, distant figure is too small a share of a large zone, and the slider cannot fix it: at
  100, FrontYard-Right still needs 1.0% by day. A smaller zone around the far lawn would (the same figure is
  a larger share of a smaller zone).
- **The cat (05:31:39).** It walked down the Walkway. At 05:31:36 the Walkway saw 4.4% against 4.6% needed,
  and at 05:31:33 1.7%. FrontYard-Left then recorded it at 1.9% against 0.8%. No other zone saw it before.
  - Walkway at 51 or more would have caught the 4.4%.
  - At 60 it needs 2.97% at night and 5.69% by day; the boy on the walkway by day peaked at 11–22%.
  - Catching the farther 1.7% sighting needs 73.
- **Two zones in the same second (B75).** A recording needs a second changed picture in the same zone within
  0.5 s. An object that passes one zone, then the next, confirms neither. The log shows this 4 times
  (19:50:16, 21:52:16, 22:13:05, 00:07:02).

## 5. Log coverage (B70)

The export holds 10,000 lines. 6,212 of them (62%) are one-per-check motion lines, so it covers 19 hours of a
run of several days.

## 6. What other motion engines do (research, 2026-10-24)

None of these projects fixes these problems with a better pixel threshold. They add blob size, persistence
or travel over time, and a cut-off for a large sudden change. Some then run a person/vehicle classifier.

- **[Frigate](https://docs.frigate.video/configuration/motion_detection/)** (MIT).
  - Motion: a running-average background, contour area, and a recalibration above 80% change
    ([source](https://github.com/blakeblackshear/frigate/blob/dev/frigate/motion/improved_motion.py)).
  - Motion only starts object detection. An object is
    [in a zone](https://docs.frigate.video/configuration/zones/) when the bottom centre of its box is inside,
    for 3 pictures.
- **[Motion](https://motion-project.github.io/motion_config.html)** (GPL-2): a despeckle filter, a light-switch
  mute for 5 pictures, and a minimum count of pictures with motion.
- **[ZoneMinder](https://zoneminder.readthedocs.io/en/latest/userguide/definezone.html)** (GPL-2): blob
  filters with minimum and maximum size and count, and a mute after an overload.
- **[Blue Iris](https://ipcamtalk.com/goto/post?id=296543)** (proprietary): object size and travel, a reset
  for large changes, and masks. Its users report no full fix for headlights.
- **[Agent DVR](https://www.ispyconnect.com/docs/agent/motion-detection)**: object tracking with a minimum
  travel and time.
- **Insects.** [SecuritySpy](https://bensoftware.com/forum/discussion/3719/feature-request-ai-mothra-night-sprites-suppression)
  leaves them to its classifier. Its best advice is an infrared light away from the lens.
- **Classifiers on a Pi 4 CPU**
  ([benchmark](https://github.com/aallan/benchmarking-ml-on-the-edge),
  [YOLO on NCNN](https://github.com/Qengineering/YoloV8-ncnn-Raspberry-Pi-4)):

  | Model | Pi 4 CPU | Coral (USB 3) |
  |---|---|---|
  | MobileNet SSD | 83–113 ms per picture | 15–18 ms |
  | YOLOX-nano | 7 pictures a second | |

  A classifier needs a crop of the full picture, plus numpy and a runtime (tflite-runtime 2.3 MB or
  onnxruntime 21 MB).

The research ranked these for AnyCam, cheapest first:

1. Blobs (connected changed cells) with an absolute minimum size, and zone membership by the blob's bottom
   centre.
2. A tracker that confirms an object over 3+ pictures and a minimum travel, across zones.
3. A light hold that ends when the picture is stable, not after a fixed 2 s.
4. A brightness-direction test (headlights, insects).
5. A classifier on a full-resolution crop, only while motion is seen.
6. numpy for the cell comparison, which makes 1–4 cheap.

The costs at 128 x 96 are estimates, not measurements.

## 7. A headlight rule, replayed on the sample clips (C30)

The rule is checked on each comparison, about 4 a second. Each comparison sets the picture against the one
1 s older, on the 128 x 96 grid.

- **Start a pause:** all four must hold.
  - 0.5% of the picture or more changed.
  - 90% or more of the changed cells got brighter.
  - They got brighter by +25 grey levels or more on average.
  - In the 1.5 s before, no comparison changed 0.3% or more, unless it was itself 75% or more brighter (a
    light that grows).
- **During the pause:** no recording starts. The slow-movement check drops its older pictures (B73).
- **End of the pause:** after two quiet comparisons in a row (under 0.3%), or 4 s after it started.
- **The beam leaving:** within 3 s after a pause, a change whose cells are 90% or more darker, by −25 or more,
  pauses again, at most 6 s after the first start. A darker-only change never starts a pause by itself.

The replay used the same comparison as the add-on (scratch tool, OpenCV), on the recorded clips:

| Clip | Pauses | Left over for detection |
|---|---|---|
| Headlights at dusk, 18:21:58 | 2.3–6.5 s | nothing |
| Headlights, 20:55:25 | 1.4–5.7 s, 5.9–7.6 s | nothing |
| Headlights, 21:08:35 | 2.6–6.8 s | nothing |
| Headlights, 23:24:00 | 2.0–6.2 s | nothing |
| Car on the street at the right, 05:50:13 | 1.7–5.9 s, 7.6–7.9 s | the moving car, 6.2–8.2 s (trim the zone) |
| The boy (2 clips), the man, the cat | none | all of the movement |
| The insect, the IR switch | none | nothing (other rules already handle them) |

Two versions failed first, and the final rule is shaped by them:

- A darker-only start fired when the man, in a dark jacket, first walked into the picture.
- A brighter-only change also fired when he went in the door and the bright doorway reappeared behind him.

The quiet-before condition separates these: a headlight comes out of a quiet picture, a person does not.

**Limits.**
- The clips start about 3 s before their trigger, so the history before that counts as quiet.
- The replay is not the add-on's own code (OpenCV, not Pillow; 7 recorded pictures a second).
- A person who walks in during a pause is detected when it ends, at most 4 s later. A person who comes and
  goes within it is missed.

