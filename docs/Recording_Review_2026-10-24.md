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

## 8. The headlight rule on every recording (20–24 Oct)

CrystalHeeler opened the whole front-camera recordings folder for this test: 486 files, 326 recordings (213 by
day, 113 at night). The rule of section 7 ran on each recording's first file. It counts as "paused at the
trigger" when a pause covers 2.8–4.6 s into the file, where the recording started after its 3–4 s pre-roll.
Clips with movement left after the pause were checked frame by frame, and so was a sample of the rest.

**Version 3 (section 7):**
- At night it paused at the trigger in 70 of 113 recordings: headlight sweeps, cars on the street, and three
  day-to-night switches (20 Oct 18:24:22, 21 Oct 18:23:39, 23 Oct 18:20:40).
- By day it paused in 3, all after dark-hours light.

Three findings changed the rule.

1. **Two headlight sweeps were missed (23 Oct 19:48:11, 22 Oct 20:37:12).**
   - Just before the beam, something small changed: a shadow swept the lawn as the light came on (1.0%, darker),
     and the car itself showed on the street at the top (0.65%).
   - Either broke the "quiet before" check, which allowed nothing at or above 0.3%.
   - **Change:** quiet before now means nothing at or above 1.5% (unless it was itself mostly brighter).
   - **Replay:** both are caught, plus a car on the street (22 Oct 20:10:28) and a daytime sun-through-cloud
     false alarm (24 Oct 09:50:38). Nothing with a person changed.
   - The man going in the door still does not start a pause: changes of 4–15% came just before.
2. **The porch light (20 Oct 18:57:58, 21 Oct 19:19:51).**
   - A large change (19–23% of the picture), nearly all brighter, as the porch light came on. The camera
     switched from infrared to colour, and a person came out at once.
   - The rule paused the person's first seconds.
   - **Change:** no pause when the picture turns from infrared to colour during the change; that is a light
     someone switched on. The add-on already measures colour for its night state.
   - The day-to-night switch goes the other way (colour to infrared) and still pauses.
3. **The front door opening (23 Oct 20:26:49, 24 Oct 06:36:14).**
   - The storm door's glass makes a narrow bright strip at the right edge (+55 to +76), then a person steps out.
   - The rule pauses; the person keeps moving and is recorded when the pause ends, but the file would start
     about 4 s late and miss the door opening.
   - **Change:** a recording that starts right after a pause starts its file at the beginning of the pause, so
     the pause is inside the pre-roll.
   - This makes any wrong pause cost nothing, as long as the movement goes on past it.
   - It needs about 10 s of video held in memory per armed camera instead of 3 s (about 10 MB at 8 Mbit/s).

**After the changes:**
- At night, 73 of 113 recordings are paused at the trigger.
- By day, 1 of 208: the sun coming out from behind a cloud, also a false alarm.

**The 40 night recordings left:**
- Mostly small changes (0.1–1% of the picture) at the top: the street and the far lawn. Cars and their lights
  on the street, many of them a light leaving (darker only, which never starts a pause).
- A moth near the lens (23 Oct 18:39:12).
- The cat (23 Oct 05:31:39).
- A person coming out of the door (21 Oct 19:18:38).

The street ones need the zone edit: keep NeighborSideYard and the top of FrontYard-Right off the street.

**Not checked:**
- Every paused recording was not viewed; those with movement left after the pause were, and a sample of the
  others (all lights).
- In this replay "turned to colour" was taken from the clip's overall colour, which wrongly lost the dusk
  headlight clip (22 Oct 18:21:58, colour before and after). The add-on would compare colour before and after
  the change.


## 9. First field night on 3.9.0-rc1.0 (24–25 Oct, Test System B)

3.9.0-rc1.0 started at 22:05 on 24 Oct. Every recording from then to 07:00 was viewed (54 files, first part of
each) and matched with the log (exported 25 Oct).

| What the recording shows | Files | Cameras |
|---|---|---|
| Vehicle headlights | 29 | front yard (ch2) 9, patio (ch7) 8, driveway (ch8) 9, driveway (ch6) 3 |
| Insects in the infrared | 10 | back yard (ch4) 9, patio 1; one ran 15.5 min (31 files) |
| A light by the hot tub switching on and off | 9 | back yard, outside the zones |
| The infrared-to-colour switch at dawn | 2 | back yard 06:37, front yard 06:40 |
| Nothing visible | 1 | driveway (ch8) 06:24 |
| A person (after 07:00) | 3 | back yard |

**Headlights.** The front cameras paused for a passing light 22 times in the night. 10 passes made no
recording. 12 passes made the 29 files. The pause starts at the right moment; the recordings come from
what happens around it:

1. **The pause ends while the light is still moving (20 files).** A pause lasts at most 4 s (6 s in all).
   A car's light crosses the front yard in about 10 s. The recording starts 1–3 s after the pause ends:
   - 14 of the 20 through the slow-movement check. It kept storing pictures during the pause, so 5 s later it
     compared the scene with a lit picture.
   - 6 through the normal checks, on the light still moving (no new pause: the picture before was not quiet).
2. **The zone fires before the pause sees the light (4 files).** The first, faint edge of the light passes a
   zone's level (0.8–1.2% of a zone) before it is 0.5% of the picture. The pause starts in the same second,
   after the recording.
3. **One picture records at once (3 files, ch6, no zones).** A change of 3% or more in one picture starts a
   recording on the next picture. The log then says "changed in one picture only — not recorded", which is
   wrong: it was recorded.
4. **No pause (2 files).**

**Why the replay of section 8 missed this.** It checked only whether a pause covered the trigger. It did not
run the slow-movement check or the seconds after the pause.

**Dawn.** The first, half-switched picture of the infrared-to-colour switch is under the 75% light rule. The
back yard recorded it through the slow-movement check, the front yard through the normal checks. The patio
camera saw the same switch as a light change.

**Insects.** The back yard records at 0.8% of the picture at night (sensitivity 90, night +15) and outside
its zones. Insects flying in the infrared change 1–3%, over several pictures, so the one-picture rule does
not stop them.

**Dusk was not tested.** The dusk recordings of 24 Oct (17:59–20:14) are from the build before
3.9.0-rc1.0.

## 10. The 3.9.0-rc2.0 changes, replayed (25 Oct)

The add-on's whole live motion path (`_motion_feed`, with each camera's zones and sensitivity from the
settings copy of 24 Oct 22:42) replayed 409 recordings: every first file of 20–24 Oct on the patio camera
(ch7), and every first file of the night of 24–25 Oct. Pictures were decoded once, 4 a second, at the
detector's size, with the colour of each picture.

**What changed (B84, B85):**
- A light pause lasts up to 8 s (12 s in all), not 4 s (6 s). The light still moving right after a pause
  continues it.
- The slow-movement check keeps no picture during a pause, so it never compares the scene with a lit one.
- A live start waits 1.25 s. It does not start when that time shows a light pause, a light change, the camera
  switching between infrared and colour (a colour jump of 1.0 or more with one side at 2.5 or less), or a
  one-sided change that stopped at once (a lamp).
- The one-picture rule ignores a lit lawn.

**The night of 24–25 Oct (54 files, section 9):**

| Shows | 3.9.0-rc1.0 replay | 3.9.0-rc2.0 replay |
|---|---|---|
| Headlights (29) | 22 recorded | 0 |
| Motion-sensitive light (8) | 4 | 0 |
| Dawn switch (2) | 1 | 0 |
| A person (3) | 3 | 3 |
| Insects (9) | 9 | 9 |

The replay of 3.9.0-rc1.0 records 22 of the 29 headlight files, not 29: each file starts 3–8 s before its
trigger, so the replay misses what came before it.

**All 409 files:** by day 214 of 215 still record; the one lost is a dusk headlight (22 Oct 18:21:58). At night
3.9.0-rc2.0 records 36 of the 127 files 3.9.0-rc1.0 records. Every one of the 91 lost was viewed: headlights,
the porch light switching the camera to colour, the dusk and dawn switches, and one insect. The cat, the person
at the door (21 Oct), both door openings (23 Oct 20:26, 24 Oct 06:36) and the moth still record.

**Pause length:** 4/6 s left 2 of the 29 headlight files, 6/10 s left 1, 8/12 s left none, with no other file
lost. The recording buffer keeps 17.25 s (about 17 MB at 8 Mbit/s per armed camera) so a recording after a
long pause still starts before it.

**Not tested:** the live detector (ffmpeg decoding the sub-stream) on the Pi; the Pi's memory with the longer
buffer; dusk, which no recording of the night holds on 3.9.0-rc1.0; the rabbit that switched the light on
(not found in the 9 lamp files).
