# B54: probing an identified camera's other ports — research

Build plan item B54, open question 1: once ONVIF or a brand path has identified a camera
at scan time, should the scan still probe the host's other open ports? Today each extra
web port gets 17 MJPEG, 11 HLS, 9 WebRTC and 7 WebSocket requests (44 in all, 4 s timeout
each), and sometimes an RTSP path walk.

CrystalHeeler asked on 2026-10-09: how likely is it that this probing triggers bad
behavior, how likely is it that it finds other feeds or cameras, and what real-world
numbers support each answer. Two web-research passes ran on 2026-10-09, then the findings
were checked against the AnyCam code (3.7.5-rc2.0).

## Summary

- **Harm from the anonymous probes: low.** No documented case of short, valid, one-at-a-time
  GETs that get 401 or 404 crashing or locking out a camera. Every camera crash CVE found
  needs crafted or malformed input. Vendor lockouts count failed username/password
  attempts. Estimate: well under 1% of home devices per scan (inference, medium confidence;
  no study measures it).
- **Harm from the probes with a saved login: high when the saved password is out of date.**
  Vendor lockout limits are 5 to 10 failed logins.
- **Harm on raw printer ports (9100–9107): real.** Printers print data sent to them. The
  code check below found that AnyCam sends its web probes to port 9100.
- **Missing a feed or a camera by skipping the other ports: low.** Estimate: under 1% of
  hosts that pass the camera test (inference, medium confidence). Web-port streams on an
  ONVIF or RTSP camera are copies of its RTSP main or sub-stream. Multi-camera servers
  (motionEye, go2rtc, Frigate, MediaMTX, UniFi Protect) almost never pass the camera test:
  they use port 8554 or others, not 554, and do not answer ONVIF discovery.

## Code checks (AnyCam 3.7.5-rc2.0)

1. **Port 9100 gets the web probes.** 9100 (raw print), 631 (IPP) and 22 (SSH) were added to
   the nmap port list in 2.4.0-rc2.2 as "classifier-only" ports, so the verdict can reject
   printers. But the port loop in `run_scan` probes every open port, and the "not camera"
   verdict stops no probe (it only decides the last "needs password" card). So a printer
   with 9100 open gets the HTTP probe set on its raw print port. Printed pages were not seen
   in a field log; this is from the code. The 1.x gate `is_camera_positive()` respected the
   "not camera" verdict; it was lost in the 2.0.6 / 2.1.0 rebuild (see B55,
   `docs/B55-MacAddressRegression.md`).
2. **The saved-login retry.** On a port with a saved card that has a password, the scan
   repeats the MJPEG and HLS probes with that login (28 requests) and runs the RTSP walk
   with it (one failed login per path that answers 401). With an out-of-date password that
   passes Hikvision's limit of 7. B53 (3.7.5-rc2.0) already skips a host with a saved,
   working camera, so this now needs a saved card with a password and no working stream.
3. **AnyCam has no HTTP-FLV probe.** The research names Reolink HTTP-FLV as the one real
   loss from a port skip. AnyCam does not probe FLV today, so a skip loses nothing there.
4. **Not about ports, noticed in passing:** at password entry, an ONVIF device's profiles
   are reduced to one per (width, height, codec). An ONVIF NVR whose channels share a
   resolution would end up as one card. Not seen in the field; the test systems have no
   ONVIF NVR.

## Harm: evidence

| Device / brand | Behavior | Trigger | Numbers | Source | Type |
|---|---|---|---|---|---|
| Hikvision camera | Rejects the source IP | Failed username/password | 7 tries for admin, 5 for operator; 30 min lock | [Hikvision security guide](https://www.hikvision.com/content/dam/hikvision/en/cybersecurity/Network-Camera-Security-Guide.pdf) | Vendor document |
| Hikvision camera | Lock shown in the 401 body | Lockout active | `lockStatus` and `unlockTime` fields | [Hikvision_access v0.2.1](https://github.com/Iakovosv/Hikvision_access/releases/tag/v0.2.1) | Code |
| Hikvision camera | Locked by a client that retries in a loop | Wrong credentials | Lock on by default | [CamioCam issue #34](https://github.com/CamioCam/rtsp/issues/34) | Anecdote |
| Dahua / Amcrest / Lorex | Account locked | Wrong password | About 5 tries (3 on some firmware); 5 or 30 min | [LookoutNVR](https://lookoutnvr.com/docs/camera-setup), [LearnCCTV](https://learncctv.com/dahua-account-has-been-locked/) | Third party |
| Reolink | All users locked | Wrong password | About 10 tries in 3 min; 5 min lock; optional | [Reolink support](https://support.reolink.com/articles/15507097554841-How-to-Set-up-Illegal-Login-Lockout/) | Vendor document |
| Reolink RLC-810A | Error -105 "Frequent logins" | Repeated logins | Locked 15–20 min | [Reolink community](https://community.reolink.com/topic/3154/rlc-810a-v3-1-0-764-frequent-logins-error) | Anecdote |
| Axis | Source IP blocked | Failed logins | 20 in 1 s; 10 s block; default since AXIS OS 11.5 | [Axis hardening guide](https://help.axis.com/en-us/axis-os-hardening-guide) | Vendor document |
| TP-Link Tapo | "Temporary Suspension" | Failed local logins | 600 s and about 1,800 s seen | [ioBroker forum](https://forum.iobroker.net/post/1171722) | Anecdote |
| Hipcam RealServer | RTSP offline | Malformed `client_port` in SETUP | About 45 s outage | [CVE-2023-50685 write-up](https://github.com/UnderwaterCoder/Hipcam-RTSP-Format-Validation-Vulnerability) | Researcher report |
| Hikvision | Crash | Long RTSP Range header | — | [CVE-2013-4977](https://nvd.nist.gov/vuln/detail/CVE-2013-4977) | CVE |
| Reolink RLC-410W | Reboot | Crafted HTTP request, no login | 1 request | [TALOS-2021-1422](https://www.talosintelligence.com/vulnerability_reports/TALOS-2021-1422) | CVE / Talos |
| Tapo | Crash | Bad request body | — | [CVE-2026-0918](https://www.sentinelone.com/vulnerability-database/cve-2026-0918/) | CVE |
| Generic cameras | Refuse connections | Too many concurrent sessions | Limit not stated | [Frigate go2rtc guide](https://docs.frigate.video/troubleshooting/go2rtc/) | Docs |
| Industrial lab (Siemens, Schneider, Allen-Bradley) | Failure state; power cycle needed | Some Nmap flags; one OpenVAS technique | 2 of 5 endpoints | [Samanis et al. 2022](https://arxiv.org/abs/2202.01604) | Measured, small sample |
| Siemens S7-1200 PLC | Stop/defect state | Malformed HTTP at a high rate | — | [CVE-2011-20001](https://nvd.nist.gov/vuln/detail/cve-2011-20001) | CVE |
| Live control networks | Robot arm moved; wafer fab hung; gas utility down 4 h | Ping sweep or scan | 3 incidents | [Sandia 2005, quoted by LLNL](https://www.energy.gov/sites/default/files/2017/02/f34/LLNL_SASEDS_Peer_Review_2016.pdf) | Secondhand |
| Printers (raw port) | Print pages of probe data | Data sent to 9100–9107 | Left out of Nmap version scans by default | [Nmap book](https://nmap.org/book/vscan-fileformat.html) | Docs |
| Synology NAS | Source IP blocked | Failed logins only | 10 in 5 min; no expiry by default | [Synology Knowledge Center](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/connection_security_protection?version=7) | Vendor |
| 83 M home devices in 15.5 M homes | No harm reported | Port scan, HTTP root page fetch, Telnet/FTP login tries | No crash rate given | [Kumar et al., USENIX Security 2019](https://www.usenix.org/system/files/sec19-kumar-deepak_0.pdf) | Large study, harm not measured |

Gaps: no study gives a crash rate for home devices under HTTP path probing; no published
lockout numbers for Uniview, Foscam or Hipcam/Microseven; no vendor states in so many words
that anonymous 401s are not counted (inference from "failed username/password"); the
Hipcam "2 connections in 5 s" limit is in AnyCam's own notes but no public source was
found; [IP Cam Talk](https://ipcamtalk.com/) returned HTTP 403 to the research tool.

## Missing a feed or a camera: evidence

Home Assistant install base, from [Home Assistant Analytics](https://analytics.home-assistant.io/)
on 2026-10-09: 552,580 installs share integration data, 421,117 share add-on data.

| Integration or add-on | Installs | Share |
|---|---|---|
| Reolink | 61,877 | 11.2% |
| Generic camera | 35,878 | 6.5% |
| ONVIF | 28,530 | 5.2% |
| Frigate (custom integration) | 28,520 | 5.2% |
| UniFi Protect | 25,054 | 4.5% |
| Tapo Control (custom) | 17,180 | 3.1% |
| Frigate add-ons (all variants) | about 15,600 | 3.7% |
| go2rtc add-on | 14,072 | 3.3% |
| MJPEG | 7,563 | 1.4% |
| motionEye add-on | 7,544 | 1.8% |
| Hikvision + hikvision_next | 4,001 + 3,091 | 1.3% |
| Dahua (custom) | 4,662 | 0.8% |
| Scrypted add-on | 4,268 | 1.0% |
| Android IP Webcam | 1,209 | 0.2% |

Cloud-only cameras have no local ONVIF or RTSP, so the camera test never fires on them:
Ring 34,786 installs, Nest 20,923, Blink 9,568.

| Product or case | What a port skip misses | Source |
|---|---|---|
| Reolink, 5 MP or less | HTTP-FLV on 80/443, which [Frigate](https://docs.frigate.video/configuration/camera_specific/) calls more reliable than RTSP for these models. AnyCam does not probe FLV today | [go2rtc](https://github.com/AlexxIT/go2rtc) |
| Reolink "ext" stream | A third stream over RTMP/FLV; RTSP has main and sub only | [go2rtc RTMP docs](https://github.com/AlexxIT/go2rtc/blob/master/internal/rtmp/README.md) |
| Amcrest / Dahua MJPEG | A low-resolution copy (640x480) | [HA Amcrest docs](https://www.home-assistant.io/integrations/amcrest/) |
| Tapo, Wyze, Reolink with ports off | Nothing: the camera test does not fire | [Reolink port settings](https://support.reolink.com/articles/900000621783-How-to-Configure-Reolink-Ports-Settings/), [wz_mini_hacks](https://github.com/gtxaspec/wz_mini_hacks/wiki/Firmware-Support) |
| motionEye, go2rtc, Frigate, MediaMTX, UniFi Protect | Not at risk: no ONVIF discovery answer and no port 554 | [motionEye](https://github.com/motioneye-project/motioneye), [Frigate](https://docs.frigate.video/frigate/installation/), [MediaMTX](https://github.com/bluenviron/mediamtx) |
| Synology Surveillance Station | At risk: one `Sms=N.unicast` path per camera on 554. The RTSP walk has no `Sms=` path | [Synology KB](https://kb.synology.com/en-au/SurveillanceStation/help/SurveillanceStation/camera_get_stream_path?version=9) |
| rpos (Raspberry Pi ONVIF server) | At risk: answers WS-Discovery, so a second camera server on the same Pi would be skipped | [rpos](https://github.com/BreeeZe/rpos) |
| CrystalHeeler's Oak-D add-on | MJPEG/H.264 on 8765 with no RTSP or ONVIF; skipped only if its host also passes the camera test | Project notes |

Gaps: no measured share of home cameras that offer a feed only on a web port while ONVIF or
RTSP is on; no data on how many hosts run a camera plus a second camera server; Home
Assistant Analytics counts installs, not cameras.

## Suggestions from the research

- Stop at the first sign of a lock: a 401 body with `lockStatus`, or Reolink error -105.
- At most 2 attempts with a login per host per scan.
- Send nothing to ports 9100–9107.
- Add `Sms=` paths for Synology Surveillance Station.
