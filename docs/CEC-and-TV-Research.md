# HDMI-CEC and "Show the Camera on the TV" — Research Report

Date: 2026-10-26. Owner: CrystalHeeler. Scope: AnyCam add-on on a Raspberry Pi 4 under Home Assistant OS (HAOS), with [go2rtc](https://github.com/AlexxIT/go2rtc) for streams.

## Short answer

**HDMI-CEC alone cannot do the job.** HDMI Consumer Electronics Control (CEC) can wake a TV and switch it to an input. It cannot send a picture. The picture must come from the HDMI device that sends the CEC command. So CEC works only for one TV that has a cable to the Pi (or to a box that shows the camera).

**For "any TV in the house", use the network, not the cable.** The best fit is an overlay app on Android TV, Google TV or Fire TV. Examples are [TvOverlay](https://github.com/gugutab/TvOverlay) and the [PiPup](https://github.com/rogro82/PiPup) family. These apps put a live camera window over the current program. They do not stop the program. Google Cast is a second choice: it is full screen, but users report 10–30 s start delay ([HA Community](https://community.home-assistant.io/t/playing-camera-stream-to-chromecast-or-something/1020595)).

**Recommended AnyCam design:** AnyCam fires a "show on TV" request per camera, with a cooldown and quiet hours. A Home Assistant action does the TV work. AnyCam supplies the go2rtc stream URL. Details are in [Part 6](#6-design-suggestions-for-anycam).

Reddit: not reachable. The fetch tool refused `old.reddit.com`, and the web search returned no Reddit pages.

Method note: pages on [community.home-assistant.io](https://community.home-assistant.io/) and [IP Cam Talk](https://ipcamtalk.com/) returned HTTP 403 to the fetch tool. Claims from those pages come from search-result excerpts of the page text.

---

## 1. What HDMI-CEC is

### 1.1 The bus

- CEC is one shared wire in the HDMI cable. It is separate from the video signals. It links all HDMI ports of a device, and it can work when the device is in standby ([Wikipedia](https://en.wikipedia.org/wiki/Consumer_Electronics_Control)).
- The bus is slow: about 417 bit/s ([Wikipedia](https://en.wikipedia.org/wiki/Consumer_Electronics_Control)).
- Up to 15 devices can share one bus ([Wikipedia](https://en.wikipedia.org/wiki/Consumer_Electronics_Control)).
- The CEC wire is mandatory in HDMI. CEC features in a product are optional ([Wikipedia](https://en.wikipedia.org/wiki/Consumer_Electronics_Control)).

### 1.2 Addresses

- **Logical address (4 bits).** Each frame header holds a 4-bit source and a 4-bit destination. Address 15 (0xF) is the broadcast address ([Wikipedia](https://en.wikipedia.org/wiki/Consumer_Electronics_Control), [Pulse-Eight](https://support.pulse-eight.com/support/solutions/articles/30000053359-understanding-cec-opcodes)). The TV is logical address 0 ([cec-ctl man page](https://www.mankier.com/1/cec-ctl)).
- **Claiming an address.** A device "pings" an address. If no device answers, it takes that address ([Wikipedia](https://en.wikipedia.org/wiki/Consumer_Electronics_Control)).
- **Physical address.** This is the device's place in the HDMI tree, for example `1.0.0.0` for TV input 1. The `--active-source` command carries it ([cec-ctl man page](https://www.mankier.com/1/cec-ctl)).

### 1.3 Opcodes that matter here

Values from [Pulse-Eight](https://support.pulse-eight.com/support/solutions/articles/30000053359-understanding-cec-opcodes):

| Opcode | Name | What it does |
|---|---|---|
| `0x04` | Image View On | Wakes the TV and switches it to the sender's input. |
| `0x0D` | Text View On | Same as Image View On, and also clears TV menus. |
| `0x82` | Active Source | Broadcast: "I am now the source. Show my input." |
| `0x86` | Set Stream Path | The TV asks the device at a physical address to become the source. |
| `0x36` | Standby | Puts one device, or all devices on broadcast, into standby. |
| `0x44` | User Control Pressed | Sends a remote-control key press. |
| `0x80` | Routing Change | An HDMI switch reports a route change. |

To show the Pi on the TV, the Pi sends **Image View On** to address 0, then broadcasts **Active Source** with its physical address ([cec-ctl man page](https://www.mankier.com/1/cec-ctl)).

### 1.4 Brand names

Each brand sells CEC under its own name ([Wikipedia](https://en.wikipedia.org/wiki/Consumer_Electronics_Control)):

- Samsung: Anynet+
- LG: SimpLink
- Sony: BRAVIA Sync, BRAVIA Link, Control for HDMI
- Panasonic: VIERA Link, HDAVI Control, EZ-Sync
- Philips: EasyLink
- Sharp: Aquos Link
- Toshiba: CE-Link, Regza Link
- Roku: 1-Touch Play
- Hisense, Vizio: CEC
- Onkyo: RIHD; Pioneer: Kuro Link; Mitsubishi: NetCommand for HDMI

### 1.5 Reliability between brands

- Manufacturers use different CEC implementations. They are not all compatible ([Audioholics forum](https://forums.audioholics.com/forums/threads/tv-on-off-sync-issue.129930/post-1669327)).
- Power on/off usually works across brands. Menu and input control fail more often ([GTPlanet forum](https://www.gtplanet.net/forum/threads/hdmi-control-between-brands.236017/)).
- Receivers sometimes lose the device list. The fix is to turn HDMI Control off and on ([Audioholics forum](https://forums.audioholics.com/forums/threads/tv-on-off-sync-issue.129930/post-1669327)).
- One plugin author writes that a command set that works on one TV may not work on another. Active Source can make a two-input TV flip to the wrong input ([homebridge-cec-tv-control](https://npmjs.org/package/homebridge-cec-tv-control)).
- A Pi can steal the input 1–2 s after the TV turns on, even with `hdmi_ignore_cec_init=1` ([Plex forum](https://forums.plex.tv/t/rasplex-always-switches-tv-input/80901)).

**Effect on AnyCam:** expect brand-by-brand testing. "It works on my TV" does not carry to other TVs.

---

## 2. Raspberry Pi 4 and CEC

### 2.1 Ports and device nodes

- The Pi 4 has two HDMI controllers. The kernel driver gives one CEC device per controller: `/dev/cec0` and `/dev/cec1` ([Linux kernel mailing list](https://lkml.iu.edu/hypermail/linux/kernel/2101.2/01348.html)).
- Use **HDMI0**, the port next to the USB-C power socket. Users report CEC works only there ([Arch Linux ARM forum](https://archlinuxarm.org/forum/viewtopic.php?p=66879)). Older libcec builds open only `/dev/cec0`, so only HDMI0 works with them ([LibreELEC forum](https://forum.libreelec.tv/thread/28180-rpi4-cec-hard-coded-to-hdmi-0/)).
- Some micro-HDMI adapters do not carry the CEC pin. One user fixed CEC with a one-piece micro-HDMI-to-HDMI cable ([LibreELEC forum](https://forum.libreelec.tv/thread/20161-rpi4-hdmi-cec-not-working/?pageNo=6)). A kernel developer had the same adapter fault ([Linux kernel mailing list](https://lkml.iu.edu/hypermail/linux/kernel/2101.2/01348.html)).

### 2.2 Kernel CEC framework vs libcec

- **Kernel CEC framework.** The driver creates `/dev/cecX`. User programs talk to it through ioctl calls ([kernel.org](https://docs.kernel.org/userspace-api/media/cec/cec-intro.html)).
- **v4l-utils tools.** `cec-ctl` sends, receives and monitors CEC messages. `cec-compliance` tests a remote device. `cec-follower` emulates a device ([kernel.org](https://docs.kernel.org/userspace-api/media/cec/cec-intro.html)).
- **libcec.** This is the Pulse-Eight library, with the `cec-client` tool. It supports Pulse-Eight USB adapters and the Raspberry Pi ([libcec on GitHub](https://github.com/Pulse-Eight/libcec)).
- Since Home Assistant 2021.7.0, its CEC libraries support only CEC devices in the Linux kernel ([community Pi-CEC add-on page](https://rfc1149.net/devel/homeassistant-addon-pi-cec.html)).

### 2.3 Commands: wake the TV and switch to the Pi

With `cec-ctl` ([cec-ctl man page](https://www.mankier.com/1/cec-ctl)):

```sh
# 1. Register the Pi as a playback device (once per boot)
cec-ctl -d /dev/cec0 --playback

# 2. Wake the TV and ask it to show this input
cec-ctl -d /dev/cec0 --to 0 --image-view-on

# 3. Claim the input (use the physical address that cec-ctl reports)
cec-ctl -d /dev/cec0 --to 0 --active-source phys-addr=1.0.0.0

# Later: put the TV in standby
cec-ctl -d /dev/cec0 --to 0 --standby
```

With `cec-client`: `on 0` wakes the TV, `as` makes the Pi the active source, `is` marks it inactive ([Pi My Life Up](https://pimylifeup.com/raspberrypi-hdmi-cec/)).

### 2.4 Known Pi problems

- A user on kernel 6.18 found that the first power-on after boot failed. Adding `--from 14` to `cec-ctl` fixed it ([Arch Linux ARM forum](https://archlinuxarm.org/forum/viewtopic.php?p=73923)).
- A HAOS user on a Pi 4 (HAOS 18.1, HA 2026.7.2) gets "autodetect FAILED" from the CEC Scanner app. The thread shows no fix ([HA Community](https://community.home-assistant.io/t/issues-with-cec/1018323)).
- libcec suggests `hdmi_force_hotplug=1` in `/boot/config.txt` when the Pi does not detect the TV ([libcec on GitHub](https://github.com/Pulse-Eight/libcec)).

### 2.5 The hard limit: the cable

CEC reaches only devices on the same HDMI chain. The Pi must connect to the TV by HDMI, directly or through an audio/video receiver (AVR). One Pi with two ports can reach two TVs at most, and in practice only HDMI0 has CEC. A TV in another room needs its own HDMI source.

---

## 3. Home Assistant and CEC

### 3.1 The `hdmi_cec` integration

- Actions: `power_on`, `select_device` (make a device the active source), `standby`, `send_command` (raw CEC), `volume`, `update` ([HA docs](https://www.home-assistant.io/integrations/hdmi_cec/)).
- It creates `switch` entities by default, or `media_player` entities if you choose ([HA docs](https://www.home-assistant.io/integrations/hdmi_cec/)).
- It is marked as a **legacy integration** and uses YAML setup ([HA docs](https://www.home-assistant.io/integrations/hdmi_cec/)).
- On HAOS, the docs say to test with the CEC Scanner app, and to stop that app from starting on boot, because it blocks the integration ([HA docs](https://www.home-assistant.io/integrations/hdmi_cec/)).
- It can also connect over TCP to a remote CEC server through the `host` option ([HA docs](https://www.home-assistant.io/integrations/hdmi_cec/)).

### 3.2 Can an add-on open `/dev/cec0`?

Yes, in principle. The add-on `config.yaml` has these hardware options ([HA developer docs](https://contributors.home-assistant.io/apps/reference/configuration/)):

| Option | Effect |
|---|---|
| `devices: [/dev/cec0]` | Maps one named device into the container. This is the narrowest choice. |
| `video: true` | "Give access to all video devices." The docs do not say if this includes `/dev/cec*`. |
| `full_access: true` | All hardware. Works only with protection mode off. Lowers the security rating. |
| `homeassistant_api: true` | Lets the add-on call the Core API at `http://supervisor/core/api` as an administrator. |

Proof from other projects:

- [cec2mqtt](https://github.com/RobertMe/cec2mqtt) runs in Docker with `/dev/cec0` mapped in. It reports power state and active source over MQTT with HA discovery. It is still in development.
- A [community Pi-CEC add-on](https://rfc1149.net/devel/homeassistant-addon-pi-cec.html) runs a CEC server on the Pi. The `hdmi_cec` integration connects to it over TCP.

**Not tested here:** whether `/dev/cec0` exists on the current HAOS Pi 4 kernel, and whether `video: true` maps it. AnyCam must test both on the hardware.

---

## 4. Can CEC make a TV show a camera?

**No, not by itself.** CEC sends commands only. "Image View On" and "Active Source" tell the TV which HDMI input to show ([Pulse-Eight](https://support.pulse-eight.com/support/solutions/articles/30000053359-understanding-cec-opcodes)). The pixels come from the device on that input.

So the CEC path needs two parts on the same box:

1. A CEC command to switch the TV to the box's input.
2. A program that draws the camera on the box's HDMI output.

### 4.1 Can the HAOS Pi draw video on its HDMI port?

- Stock HAOS is headless. It shows only the console on HDMI ([HA Community](https://community.home-assistant.io/t/ha-dashboard-via-hdmi-on-tv/919280)).
- The community **HAOSKiosk** add-on starts X, Openbox and the Luakit browser, and shows a dashboard on the HAOS screen. One user on an Intel NUC saw Luakit crash. Another saw the screen go black after about 5 minutes ([HA Community](https://community.home-assistant.io/t/new-release-of-haoskiosk-add-on-0-9-8-to-display-dashboards-directly-on-your-haos-server/899429/8)).
- So an add-on **can** drive the HDMI port. A browser page with a go2rtc player is one way. A video player such as mpv is another way. No source here shows mpv on HAOS. Both need display-device access and testing.

### 4.2 Practical verdict for CEC

- It fits one TV next to the Pi, cabled to HDMI0.
- The TV must be on the Pi's input to show anything. The TV leaves the current program. This is full screen, not picture-in-picture (PiP).
- Return to the old input is not reliable. "Inactive Source" asks the TV to choose, and each brand acts differently ([homebridge-cec-tv-control](https://npmjs.org/package/homebridge-cec-tv-control)).
- Most TVs no longer offer PiP from a second HDMI input. An IP Cam Talk member put it as "PiP is basically dead" ([IP Cam Talk](https://ipcamtalk.com/threads/blue-iris-pip-picture-in-picture-on-tv-when-motion-detected.17219/)).

---

## 5. Alternatives that need no HDMI cable

### 5.1 Android TV / Google TV / Fire TV — overlay apps (best fit)

**TvOverlay**
- Android TV app with a REST API and MQTT. Its `video` field takes a URL and "supports RTSP, HLS, DASH, and SmoothStreaming" ([TvOverlay on GitHub](https://github.com/gugutab/TvOverlay)).
- The repo has sample Home Assistant YAML. With MQTT, HA creates a TvOverlay device ([TvOverlay on GitHub](https://github.com/gugutab/TvOverlay)).
- An openHAB binding author reports RTSP camera video on the TV with under 1 s lag ([openHAB Community](https://community.openhab.org/t/android-notifications-binding-with-support-for-tv-overlay-5-0-0-5-9-9/164620)).
- Needs "Draw over other apps". On some devices you grant it through ADB. The Play Store listing was suspended once ([TvOverlay on GitHub](https://github.com/gugutab/TvOverlay)).

**PiPup and its fork**
- PiPup listens on port 7979 for `POST /notify`. Media can be `image`, `video` or `web`, with a size and one of 5 screen positions ([PiPup on GitHub](https://github.com/rogro82/PiPup)).
- Sideloaded installs need `adb shell appops set nl.rogro82.pipup SYSTEM_ALERT_WINDOW allow` ([PiPup on GitHub](https://github.com/rogro82/PiPup)).
- The original repo is unmaintained. A community HA integration adds a real `pipup` action, through the ADB integration ([HA Community](https://community.home-assistant.io/t/pipup-notification-service/714248)).
- A maintained fork adds popups with no time limit (`duration: 0`), buttons, TTS and screen on/off. It was tested on Fire OS, Google TV and Android TV. Popup video plays silent by default, because audio froze live TV on some devices ([HA Community](https://community.home-assistant.io/t/pipup-fork-home-assistant-integration-popups-tts-buttons-screen-control-on-android-tv-fire-tv/1022171)).
- Users report mixed video results: one needed MJPEG, and one saw an RTSP stream crash the app ([Homey Community](https://community.homey.app/t/app-pro-pipup/18498)).

**Notifications for Android TV / Fire TV (`nfandroidtv`)**
- Built-in HA integration. Shows a still image (URL or file), with duration, position and transparency. No video ([HA docs](https://www.home-assistant.io/integrations/nfandroidtv/)).
- The display options work only with the legacy `notify.<name>` action, not `notify.send_message` ([HA docs](https://www.home-assistant.io/integrations/nfandroidtv/)).
- Use it as a fallback: a snapshot from go2rtc `/api/frame.jpeg` ([go2rtc](https://github.com/AlexxIT/go2rtc)).

**Android Debug Bridge (ADB) integration (`androidtv`)**
- Controls Android TV and Fire TV over ADB. `media_player.select_source` launches an app by app ID. `androidtv.adb_command` sends keys or shell commands ([HA docs](https://www.home-assistant.io/integrations/androidtv/)).
- Only one ADB client can connect at a time. Wi-Fi drops can break the link ([HA docs](https://www.home-assistant.io/integrations/androidtv/)).
- Use: wake the TV, open a full-screen camera app, then return to the old app.

**Fire TV notes**
- Fire OS 8 has no on-screen way to grant the overlay permission. You grant it through ADB ([Play Digital Signage](https://playsignage.com/support/amazon-firetv-launch-on-startup/)).
- IP Cam Talk users pop a camera on Fire TV with TinyCam Pro and Tasker ([IP Cam Talk](https://www.ipcamtalk.com/threads/how-do-i-get-foscam-to-automatically-show-on-fire-tablet-when-motion-is-detected.67658)).

**Platform note.** Android TV allows true system PiP for "smartHome" uses such as doorbells. It does not allow PiP for media playback ([Android Developers](https://developer.android.com/training/tv/start/multitasking)). Overlay apps use a different window type, so this rule does not block them.

### 5.2 Google Cast (Chromecast, Google TV, Nest Hub)

- `camera.play_stream` sends a camera to a media player. Default format is HLS. It needs the `stream` integration ([HA docs](https://www.home-assistant.io/actions/camera.play_stream/)).
- `media_player.play_media` plays any HTTP(S) URL. Cast devices use Google DNS, so `homeassistant.local` names fail; use an IP address ([HA docs](https://www.home-assistant.io/integrations/cast/)).
- `cast.show_lovelace_view` shows a dashboard on the TV. It needs HTTPS access to HA ([HA docs](https://www.home-assistant.io/integrations/cast/)).
- Discovery needs mDNS on the same subnet ([HA docs](https://www.home-assistant.io/integrations/cast/)).
- Latency: users report 20–30 s to start ([HA Community](https://community.home-assistant.io/t/playing-camera-stream-to-chromecast-or-something/1020595)) and 10–30 s ([HA Community](https://community.home-assistant.io/t/camera-play-stream-lag-issues/116258)). One tester saw HLS about 30 s behind live, and MJPEG about 0.5 s behind ([openHAB Community](https://community.openhab.org/t/google-assistent-chromecast-hls-video-stream/99381?page=3)).
- go2rtc rates HLS latency "bad", MSE "medium", WebRTC "best" ([go2rtc](https://github.com/AlexxIT/go2rtc)).
- Casting replaces the current program. It is full screen.

### 5.3 Apple TV

- tvOS shows a PiP window over the current video when a HomeKit doorbell rings ([AppleInsider](https://forums.appleinsider.com/discussion/216345)). Apple's guide confirms live camera PiP and doorbell alerts ([Apple Support](https://support.apple.com/en-ae/guide/tv/atvb90537bb0/tvos)).
- The HA HomeKit Bridge can expose a camera with `linked_doorbell_sensor` and `linked_motion_sensor` ([HA docs](https://www.home-assistant.io/integrations/homekit/)).
- Not confirmed: that an HA-bridged camera gets the same PiP popup on Apple TV. The HA page does not say.

### 5.4 LG webOS

- The HA integration launches apps (`select_source`), sends commands (`webostv.command`) and shows text notifications ([HA docs](https://www.home-assistant.io/integrations/webostv/)).
- The notification icon must be a local file. Many newer firmware versions ignore it ([HA docs](https://www.home-assistant.io/integrations/webostv/)).
- Power-on needs Wake-on-LAN or CEC ([HA docs](https://www.home-assistant.io/integrations/webostv/)).
- One community project shows doorbell snapshots as an overlay, but needs a rooted TV with SSH ([HA Community](https://community.home-assistant.io/t/video-doorbell-snapshots-as-an-overlay-on-an-lg-webos-tv/1019201)).
- No source here shows live camera video over a program on webOS.

### 5.5 Samsung Tizen

- The HA integration sends keys and switches sources. It does not document URL or media playback ([HA docs](https://www.home-assistant.io/integrations/samsungtv/)).
- A feature request for camera popups on the custom SamsungTV Smart integration has no finished build ([GitHub issue](https://github.com/ollo69/ha-samsungtv-smart/issues/106)).
- Samsung's own route: Ring Doorbell Pro video on 2022+ TVs through SmartThings ([Samsung Support](https://www.samsung.com/us/support/answer/ANS00087644)). That is a closed path, not open to AnyCam.

### 5.6 Kodi

- Kodi notifications accept an image URL as the icon, with a display time ([HA docs](https://www.home-assistant.io/integrations/kodi/)).
- Kodi can wake the TV over CEC through the `script.json-cec` add-on ([HA docs](https://www.home-assistant.io/integrations/kodi/)).
- A Kodi box on an HDMI port is a good "CEC + picture" box: it wakes the TV and plays the stream itself.

### 5.7 Roku

- `media_player.play_media` plays URLs, including `hls` and `mp4`. `camera.play_stream` sends HLS camera streams to a Roku ([HA docs](https://www.home-assistant.io/integrations/roku/)).
- Newer Roku OS versions may need a helper app for URL playback ([HA docs](https://www.home-assistant.io/integrations/roku/)).
- Full screen. HLS latency applies.

### 5.8 Comparison table

| Approach | Works from an HAOS add-on? | Needs HDMI cable? | Interrupts viewing? | Full screen or PiP | Latency (reported) | Effort |
|---|---|---|---|---|---|---|
| CEC from the Pi + Pi draws video | Partly: CEC via `devices`; drawing video needs a kiosk-type add-on | Yes, Pi HDMI0 to TV | Yes, switches input | Full screen | CEC bus is fast; TV wake time not measured | High; brand testing |
| Android/Google/Fire TV overlay (TvOverlay, PiPup fork) | Yes, HTTP or HA action | No | No, overlay on top | PiP window | Under 1 s reported for RTSP ([openHAB](https://community.openhab.org/t/android-notifications-binding-with-support-for-tv-overlay-5-0-0-5-9-9/164620)) | Medium: sideload, ADB permission |
| `nfandroidtv` snapshot | Yes, HA action | No | No | Small image popup | Snapshot only | Low |
| ADB: open a camera app full screen | Yes, HA action | No | Yes, replaces app | Full screen | App start time; not measured | Medium |
| Google Cast (`camera.play_stream` / `play_media`) | Yes, HA action | No | Yes, replaces program | Full screen | 10–30 s start ([HA Community](https://community.home-assistant.io/t/camera-play-stream-lag-issues/116258)) | Low |
| Apple TV via HomeKit Bridge | Yes, HA config | No | No | PiP (doorbell) | Not measured | Medium; PiP for bridged cams unconfirmed |
| LG webOS | Partly: text and app launch | No | Text popup only | No live video | n/a | Low; no live video |
| Samsung Tizen | No open path | No | n/a | n/a | n/a | Not available |
| Kodi box | Yes, HA action | Box to TV | Yes | Full screen or notification image | Not measured | Medium |
| Roku | Yes, HA action | No | Yes | Full screen | HLS delay | Low |

---

## 6. Design suggestions for AnyCam

### 6.1 Split the job

- **AnyCam decides** when a camera should go to the TV: motion, cooldown, quiet hours.
- **Home Assistant does** the TV action. HA already has the TV integrations in Part 5.
- AnyCam calls HA through the Supervisor proxy: `POST http://supervisor/core/api/services/<domain>/<service>`, with `homeassistant_api: true` in `config.yaml` ([HA developer docs](https://contributors.home-assistant.io/apps/reference/configuration/)).
- Simplest first step: AnyCam fires an HA event (for example `anycam_show_on_tv`) with the camera name and stream URLs. The user writes one automation per TV. AnyCam does not need to know TV brands.

### 6.2 Per-camera settings

- `show_on_tv`: on/off.
- `tv_targets`: list of HA entities (media players, notify services) or overlay app URLs.
- `tv_mode`: `overlay` (default), `fullscreen`, or `snapshot`.
- `tv_duration_s`: how long to show (default 30).
- `tv_cooldown_s`: no repeat for this camera and TV within this time (default 300).
- `tv_quiet_hours`: start and end time; no TV action inside the window.
- `tv_min_confidence`: use the object engine result, so trees and shadows do not hijack the TV.

### 6.3 Stream URLs for TVs

- Give the TV a go2rtc URL on the LAN, not an HA cloud URL. go2rtc offers RTSP on port 8554, HLS at `/api/stream.m3u8?src=<cam>`, MP4 at `/api/stream.mp4?src=<cam>`, and snapshots at `/api/frame.jpeg` ([go2rtc](https://github.com/AlexxIT/go2rtc)).
- Prefer RTSP for overlay apps (low delay). Use HLS or MP4 only for Cast and Roku.
- Use an IP address, not a `.local` name. Cast devices cannot resolve local names ([HA docs](https://www.home-assistant.io/integrations/cast/)).
- Prefer an H.264 substream. H.265 support differs by device. go2rtc lists H.265 as missing in Firefox and limited in other players ([go2rtc](https://github.com/AlexxIT/go2rtc)).
- Security: the go2rtc API on port 1984 has no password by default ([WebRTC Camera project](https://github.com/AlexxIT/WebRTC)). Do not expose it outside the LAN.

### 6.4 Do not hijack the TV

- Cooldown per camera and per TV.
- One popup per TV at a time. A second camera replaces the first, or queues.
- Quiet hours.
- Overlay first, full screen only on request.
- A global "TV alerts off" switch as an HA entity.

### 6.5 Return to the previous content

- **Overlay apps:** the popup closes after `duration`. Nothing to restore ([PiPup on GitHub](https://github.com/rogro82/PiPup)).
- **ADB full screen:** read the current app ID first. After N seconds, launch that app again with `select_source` ([HA docs](https://www.home-assistant.io/integrations/androidtv/)).
- **Cast:** call `media_player.media_stop` after N seconds. The program that was playing before does not come back by itself.
- **CEC:** send "Inactive Source" or ask the user to press Back. The TV's reaction differs by brand ([homebridge-cec-tv-control](https://npmjs.org/package/homebridge-cec-tv-control)).

### 6.6 CEC path, if wanted

- Only for one TV cabled to the Pi on HDMI0.
- Add `devices: [/dev/cec0]` to `config.yaml`. Install `v4l-utils` in the image. Test that `cec-ctl -d /dev/cec0 --playback` sees the TV.
- Send `--image-view-on`, then `--active-source`. Use `--from` as in the kernel 6.18 report ([Arch Linux ARM forum](https://archlinuxarm.org/forum/viewtopic.php?p=73923)).
- The Pi must also draw the camera full screen on HDMI. That is a separate, larger feature (kiosk browser or player). Treat it as a later step.

### 6.7 Suggested order of work

1. HA event + example automations (overlay app, Cast, `nfandroidtv` snapshot). Low risk.
2. Cooldown, quiet hours, confidence gate in AnyCam.
3. Direct calls to TvOverlay / PiPup HTTP APIs from AnyCam, with no HA automation needed.
4. CEC on the Pi, only if a TV is cabled to it.

---

## Not tested

- No TV, Pi or HAOS hardware was tested for this report. All findings come from documents and user reports.
- Not checked: `/dev/cec0` on the current HAOS Pi 4 kernel; `video: true` mapping of CEC devices; mpv or a browser player on HAOS; Apple TV PiP for HA-bridged cameras.
- Latency numbers are user reports, not measurements.

---

## Sources

1. [Wikipedia — Consumer Electronics Control](https://en.wikipedia.org/wiki/Consumer_Electronics_Control)
2. [Pulse-Eight — Understanding CEC Opcodes](https://support.pulse-eight.com/support/solutions/articles/30000053359-understanding-cec-opcodes)
3. [kernel.org — CEC introduction](https://docs.kernel.org/userspace-api/media/cec/cec-intro.html)
4. [cec-ctl man page (ManKier)](https://www.mankier.com/1/cec-ctl)
5. [libcec on GitHub](https://github.com/Pulse-Eight/libcec)
6. [Linux kernel mailing list — vc4 HDMI CEC for BCM2711](https://lkml.iu.edu/hypermail/linux/kernel/2101.2/01348.html)
7. [LibreELEC forum — RPi4 CEC hard coded to HDMI 0](https://forum.libreelec.tv/thread/28180-rpi4-cec-hard-coded-to-hdmi-0/)
8. [LibreELEC forum — RPi4 HDMI-CEC not working](https://forum.libreelec.tv/thread/20161-rpi4-hdmi-cec-not-working/?pageNo=6)
9. [Arch Linux ARM forum — Getting CEC working on RPi 4](https://archlinuxarm.org/forum/viewtopic.php?p=66879)
10. [Arch Linux ARM forum — kernel 6.18 CEC issue](https://archlinuxarm.org/forum/viewtopic.php?p=73923)
11. [Pi My Life Up — Using HDMI-CEC on a Raspberry Pi](https://pimylifeup.com/raspberrypi-hdmi-cec/)
12. [Plex forum — RasPlex always switches TV input](https://forums.plex.tv/t/rasplex-always-switches-tv-input/80901)
13. [npm — homebridge-cec-tv-control](https://npmjs.org/package/homebridge-cec-tv-control)
14. [Audioholics forum — TV on/off sync issue](https://forums.audioholics.com/forums/threads/tv-on-off-sync-issue.129930/post-1669327)
15. [GTPlanet forum — HDMI control between brands](https://www.gtplanet.net/forum/threads/hdmi-control-between-brands.236017/)
16. [Home Assistant — HDMI-CEC integration](https://www.home-assistant.io/integrations/hdmi_cec/)
17. [HA Community — Issues with CEC](https://community.home-assistant.io/t/issues-with-cec/1018323)
18. [rfc1149.net — Home Assistant Pi-CEC add-on](https://rfc1149.net/devel/homeassistant-addon-pi-cec.html)
19. [cec2mqtt on GitHub](https://github.com/RobertMe/cec2mqtt)
20. [Home Assistant developer docs — App (add-on) configuration](https://contributors.home-assistant.io/apps/reference/configuration/)
21. [HA Community — HAOSKiosk add-on 0.9.8](https://community.home-assistant.io/t/new-release-of-haoskiosk-add-on-0-9-8-to-display-dashboards-directly-on-your-haos-server/899429/8)
22. [HA Community — HA dashboard via HDMI on TV](https://community.home-assistant.io/t/ha-dashboard-via-hdmi-on-tv/919280)
23. [Home Assistant — camera.play_stream action](https://www.home-assistant.io/actions/camera.play_stream/)
24. [Home Assistant — Stream integration](https://www.home-assistant.io/integrations/stream/)
25. [Home Assistant — Google Cast integration](https://www.home-assistant.io/integrations/cast/)
26. [HA Community — Playing camera stream to Chromecast](https://community.home-assistant.io/t/playing-camera-stream-to-chromecast-or-something/1020595)
27. [HA Community — camera.play_stream lag issues](https://community.home-assistant.io/t/camera-play-stream-lag-issues/116258)
28. [openHAB Community — Chromecast + HLS video stream](https://community.openhab.org/t/google-assistent-chromecast-hls-video-stream/99381?page=3)
29. [go2rtc on GitHub](https://github.com/AlexxIT/go2rtc)
30. [WebRTC Camera on GitHub](https://github.com/AlexxIT/WebRTC)
31. [Home Assistant — Android Debug Bridge integration](https://www.home-assistant.io/integrations/androidtv/)
32. [Home Assistant — Notifications for Android TV / Fire TV](https://www.home-assistant.io/integrations/nfandroidtv/)
33. [PiPup on GitHub](https://github.com/rogro82/PiPup)
34. [HA Community — PipUp notification service](https://community.home-assistant.io/t/pipup-notification-service/714248)
35. [HA Community — PiPup fork + Home Assistant integration](https://community.home-assistant.io/t/pipup-fork-home-assistant-integration-popups-tts-buttons-screen-control-on-android-tv-fire-tv/1022171)
36. [Homey Community — PiPup app thread](https://community.homey.app/t/app-pro-pipup/18498)
37. [TvOverlay on GitHub](https://github.com/gugutab/TvOverlay)
38. [openHAB Community — Android Notifications binding with TV Overlay](https://community.openhab.org/t/android-notifications-binding-with-support-for-tv-overlay-5-0-0-5-9-9/164620)
39. [Android Developers — Multitasking on TV](https://developer.android.com/training/tv/start/multitasking)
40. [Play Digital Signage — Fire TV launch on startup (Fire OS 8 overlay permission)](https://playsignage.com/support/amazon-firetv-launch-on-startup/)
41. [IP Cam Talk — Blue Iris PiP on TV when motion detected](https://ipcamtalk.com/threads/blue-iris-pip-picture-in-picture-on-tv-when-motion-detected.17219/)
42. [IP Cam Talk — Show camera on Fire device on motion](https://www.ipcamtalk.com/threads/how-do-i-get-foscam-to-automatically-show-on-fire-tablet-when-motion-is-detected.67658)
43. [AppleInsider — tvOS 14 HomeKit video PiP](https://forums.appleinsider.com/discussion/216345)
44. [Apple Support — Cameras in the HomeKit tab on Apple TV](https://support.apple.com/en-ae/guide/tv/atvb90537bb0/tvos)
45. [Home Assistant — HomeKit Bridge integration](https://www.home-assistant.io/integrations/homekit/)
46. [Home Assistant — LG webOS TV integration](https://www.home-assistant.io/integrations/webostv/)
47. [HA Community — Doorbell snapshots as an overlay on LG webOS](https://community.home-assistant.io/t/video-doorbell-snapshots-as-an-overlay-on-an-lg-webos-tv/1019201)
48. [Home Assistant — Samsung Smart TV integration](https://www.home-assistant.io/integrations/samsungtv/)
49. [GitHub — SamsungTV Smart issue 106](https://github.com/ollo69/ha-samsungtv-smart/issues/106)
50. [Samsung Support — Ring Doorbell Pro on Samsung TV](https://www.samsung.com/us/support/answer/ANS00087644)
51. [Home Assistant — Kodi integration](https://www.home-assistant.io/integrations/kodi/)
52. [Home Assistant — Roku integration](https://www.home-assistant.io/integrations/roku/)

Reddit ([reddit.com](https://www.reddit.com/)) was tried one time and was not reachable.
