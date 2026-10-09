# B54 follow-up research: Reolink battery cameras, Layer 2, failed logins

Build plan item B54. CrystalHeeler asked three questions on 2026-10-09:

1. Do Reolink cameras exist that need a long RTSP timeout and drop their stream after a
   time cap, and on which path? (The dormant `session_time_cap` branch.)
2. Does a camera exist that streams with no login and whose path is found only with a
   new connection for each request ("Layer 2")?
3. Is "stop at the first rejected login, retry only on stale=true" safe and complete?

"Layer 1" and "Layer 2" are AnyCam's own names, not layers of the network model. Layer 1
walks every candidate RTSP path over one TCP connection. Layer 2 opens a new connection
for each path, 5 s apart, and runs only when Layer 1 heard RTSP but found no path.

Three web-research passes ran on 2026-10-09; the findings about AnyCam's code were then
checked against 3.7.5-rc2.0. Reddit was not reachable from the research tool; items marked
"search snippet only" come from search results, not from the page.

## 1. Reolink battery cameras

**Bottom line: the behavior exists, but only on the Home Hub / NVR path.**

- Reolink's [Introduction to RTSP](https://support.reolink.com/hc/en-us/articles/900000630706-Introduction-to-RTSP/)
  says that on a Reolink NVR or Home Hub a battery camera's stream lasts at most 5 minutes;
  the camera then sleeps, the hub drops the RTSP connection, and the client must ask again.
  The page recommends a request timeout of at least 20 s, for the camera to wake. This is
  the source of AnyCam's old note.
- [Reolink's support table](https://support.reolink.com/hc/en-us/articles/900000617826-Which-Reolink-Products-Support-CGI-RTSP-ONVIF)
  marks battery Wi-Fi cameras (Argus, Altas, battery doorbell) "Must connect to Reolink
  Home Hub", and 4G LTE cameras (Go series and others) as not supported.
- The maintainer of the [Home Assistant Reolink integration](https://community.home-assistant.io/t/support-for-reolink-battery-devices-as-of-2026-6/1012766)
  wrote on 2026-06-05 that these cameras support no normal streaming protocol (RTSP, FLV,
  RTMP); an Argus user in the thread got "Connection refused" on RTSP.
- One exception: the [Battery Video Doorbell (2nd Gen)](https://support.reolink.com/articles/60553722670617-FAQs-Reolink-Battery-Video-Doorbell-2nd-Gen/)
  serves RTSP directly, in wired power mode only.
- The app has its own caps: the Client warns at 5 minutes, the app at 20
  ([Reolink](https://support.reolink.com/hc/en-us/articles/360007684614-The-Log-Out-Time-of-the-Battery-powered-Camera-)).
- Field report: one user saw frame loss about every 20 minutes through a hub
  ([Ben Software forum](https://bensoftware.com/forum/discussion/comment/17898/), low confidence).
- Other brands: Eufy cameras through HomeBase drop the stream when the camera sleeps
  ([Eufy community](https://community.eufy.com/t/eufycam-3-rtsp-bug/3451555), user report).
  No RTSP evidence for Tapo battery cameras behind the H200 hub.

**What this means for AnyCam:** the dormant code gives the camera brand a 25 s timeout on a
walk of the camera's own address, which refuses the connection at once. The behavior
belongs on channels of a Reolink Home Hub or NVR that carry a battery camera: a timeout of
20 s or more, and a reconnect after the 5-minute cap (which also affects the card and the
Enhanced View).

Gaps: no measured wake time through a hub; no field report of an exact 5-minute drop;
Reolink's pages carry no dates.

## 2. Cameras that need a new connection per request (Layer 2)

**Bottom line: no documented camera matches the case.** None was found that streams with no
login and gives its path only with a new connection for each request. The three reasons in
AnyCam's code comments have no public source:

1. "Garbage on the second DESCRIBE on one connection": no bug report, forum thread or CVE.
   Every RTSP client sends all its requests on one connection;
   [FFmpeg](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/rtsp.c) sends a second
   DESCRIBE on the same connection after a 401. Frigate and Home Assistant open streams
   through FFmpeg, so such a camera would fail there too; no such report exists.
2. "The server closes the connection after the first 401": the only source is a
   [live555](https://github.com/rgaufman/live555/blob/master/liveMedia/RTSPClient.cpp)
   comment about RTSP over HTTP tunnels. It cannot apply to the scan: a camera that needs
   no login sends no 401.
3. "A limit that resets on each new connection": no source. The one throttle AnyCam has
   measured works the other way: the Microseven resets a second connection opened within
   5 s, so Layer 2 sets it off.

Closest real cases: [Micro-RTSP](https://github.com/geeksville/Micro-RTSP) (ESP32-CAM) has
no login and answers 404 for a wrong path, but a correct DESCRIBE after a 404 on the same
connection works. [Cameradar](https://github.com/Ullaakut/cameradar) notes cameras that
answer 200 to any path (Layer 1 finds those at once).
[FFmpeg ticket #2761](https://trac.ffmpeg.org/ticket/2761): an IcyBox IB-CAM2002 closed the
connection on a DESCRIBE with no User-Agent header. [retina issue #17](https://github.com/scottlamb/retina/issues/17):
Reolink's live555 server leaks data from an old session into a new one for about 65 s after
a reconnect, so reconnecting causes a fault there.

**Weak points in AnyCam's Layer 1 (checked in `anycam_probe.py`,
`_probe_rtsp_paths_single_socket`):**

- It ignores `Connection: close`. After such a reply the next request fails and the walk
  ends; only Layer 2 would then recover the path. Better: reopen one connection and go on.
- `roundtrip()` reads a reply to the blank line plus the Content-Length. A reply with a body
  and no Content-Length leaves bytes for the next read, so the next path reads the wrong
  reply: the same symptom as reason 1, but AnyCam's fault.
- It sends no User-Agent header (see the IcyBox ticket).

**Recommendation:** no Layer 2 in the scan; Layer 2 only behind Deep Re-Probe; Layer 1 gets
a reopen on `Connection: close`, a stricter reply reader and a User-Agent. This rests on no
evidence found, not on tests.

## 3. Failed logins

**Bottom line: "stop at the first rejected login" is safe against lockout, but needs four
changes.** It matches what [FFmpeg](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/rtsp.c)
and [go2rtc](https://github.com/AlexxIT/go2rtc/blob/master/pkg/rtsp/client.go) do: one try
with the login, a retry only on "stale".

- "stale=true" is part of the Digest standard: when it is missing or FALSE, the client must
  treat the login as invalid ([RFC 7616 §3.3](https://www.rfc-editor.org/rfc/rfc7616#section-3.3)).
  It is not a lock signal. In practice no server sends TRUE:
  [live555](https://github.com/rgaufman/live555/blob/master/liveMedia/RTSPServer.cpp),
  [GStreamer rtsp-server](https://github.com/GStreamer/gstreamer/blob/main/subprojects/gst-rtsp-server/gst/rtsp-server/rtsp-auth.c)
  and [MediaMTX/gortsplib](https://github.com/bluenviron/gortsplib/blob/main/server_conn.go)
  never send it; Hikvision always sends `stale="FALSE"` ([androidx/media #522](https://github.com/androidx/media/issues/522));
  Dahua leaves it out. So a camera that says nothing special is the normal case, and the
  rule treats it correctly: a 401 to a request that carried the login is a rejection.
- A correct login can still get 401: a user without permission for that stream (live555,
  GStreamer, Hikvision accounts without RTSP rights), and one Reolink case with a long URL
  ([Frigate #11214](https://github.com/blakeblackshear/frigate/discussions/11214)).
- No brand signals a lock over RTSP. The lock signals are HTTP only: Hikvision
  `lockStatus`/`unlockTime`, Reolink error -105, Tapo -40404 ("Try again in N seconds"),
  Foscam CGI -2.

**Recommended changes to the rule:**

1. A rejection is a 401 to a request that carried a login built from a nonce received on
   the same connection. The first 401 (no login sent) is only the challenge. Never reuse a
   nonce on a new connection.
2. Check the login once on the brand's main-stream path first. After one success, a later
   401 means "this path is not allowed": skip it and go on, at most 2 of these.
3. A 401 with no WWW-Authenticate header is final (live555's refusal; MediaMTX's wrong
   password, followed by a close).
4. After a rejection, tell the user "password rejected or camera locked". Where the brand
   has an HTTP API, ask it for the lock state.

| Brand / server | stale=true | Bad path, correct login | Lockout | Lock signal | Sources |
|---|---|---|---|---|---|
| Hikvision | No (always FALSE) | 404 wrong channel; 400 old sub-stream form; 401 no permission | 7 (admin) or 5 (user), source IP, 30 min | HTTP only | [manual](https://enpinfo.hikvision.com/hkwsen/unzip/20200529171958_80592_doc/GUID-27ACD723-4663-4E63-B348-7B472D965922.html), [#522](https://github.com/androidx/media/issues/522) |
| Dahua / Amcrest / Lorex | Not sent | No data | Recorder: 5, 300 s, source IP; Amcrest: 5 min | None found | [rroller/dahua](https://github.com/rroller/dahua), [SecuritySpy](https://bensoftware.com/forum/discussion/comment/13104/) |
| Reolink | No data | 404 (trailing slash) | About 10 in 3 min, 5 min; optional | HTTP -105 | [Reolink](https://support.reolink.com/hc/en-us/articles/15507097554841-How-to-Set-up-Illegal-Login-Lockout) |
| Axis | TRUE not seen; nonce about 2 min | No data | On by default from AXIS OS 11.5 | No data | [hardening guide](https://help.axis.com/axis-os-hardening-guide) |
| Uniview | No data | No data | 6 in a row, 10 min (2019 manual) | No data | [manual](https://global.uniview.com/res/201906/12/20190612_1730188_Network%20Cameras%20User%20Manual%20(Uniarch)-V1.00_851850_168459_0.pdf) |
| Tapo | No data | No data | HTTP API up to about 1,800 s | HTTP -40404 | [ioBroker](https://forum.iobroker.net/post/1171722) |
| Foscam | No data | No data | Several in 30 s; RTSP fails did not count on 2018 firmware | HTTP -2 | [CVE-2018-19076](https://nvd.nist.gov/vuln/detail/CVE-2018-19076) |
| Hanwha | No data | No data | 5 or more in 30 s | No data | [hardening guide](https://www.hanwhavision.com/wp-content/uploads/2021/10/IPCameraNetwork_Hardening_Guide_En_20230418.pdf) |
| live555 | No | 404, login checked first | None | No | [source](https://github.com/rgaufman/live555/blob/master/liveMedia/RTSPServer.cpp) |
| GStreamer | No | 404 or 401 by permission | None | No | [source](https://github.com/GStreamer/gstreamer/blob/main/subprojects/gst-rtsp-server/gst/rtsp-server/rtsp-auth.c) |
| MediaMTX | No | 404 / 400 | None | Wrong login: 401 without header, then close | [conn.go](https://github.com/bluenviron/mediamtx/blob/main/internal/servers/rtsp/conn.go) |

Gaps: no data for Ezviz, Vivotek, Hipcam/Microseven; only Foscam documents whether RTSP
fails count like web-login fails (on 2018 firmware they did not); no capture of what any
locked camera sends over RTSP; bad-path answers unknown for Dahua, Axis, Uniview, Tapo,
Foscam. To check on real cameras: lock a Hikvision, Dahua and Reolink, and send a correct
login to a bad path and to a path the user may not view.
