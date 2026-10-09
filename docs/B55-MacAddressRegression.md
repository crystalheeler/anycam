# B55: the MAC address check before any probe was lost in 2.0.6

Build plan item B55. Found 2026-10-09 while CrystalHeeler reviewed the B54 flow
charts (`docs/B54_Flow_Comparison.html`). Status: later (CrystalHeeler, 2026-10-09).

We used to do this. A MAC-first check ran from 1.2.2 to 2.0.5. 2.0.6 deleted it by
accident, and the 2.1.0 rebuild left it out. Since then, the MAC address plays only a
small, late part.

## What 1.2.2 did

From `CHANGELOG.md`: `is_camera_positive()` checked the MAC address right after the
multicast answers, before any slower probe.

- A camera maker's MAC prefix counted as a definite camera.
- A non-camera maker's prefix (Cisco, Apple, HP, Ubiquiti, Synology) rejected the device
  before any probe ran.

## How it was lost

- **2.0.6** removed what its changelog calls duplicate sections. `camera_discovery.py`
  went from 10,633 to 6,910 lines, and `run_scan` and `_probe_host_port` were deleted
  with the rest.
- **2.1.0** rebuilt both functions "from session transcripts". The rebuilt
  `_probe_host_port` has no call to `is_camera_positive()`.
- **3.0.0-rc1.3** listed `is_camera_positive` as unused code. CrystalHeeler chose to keep
  it in the code, but nothing has called it since 2.1.0.

This fits project rule 8 (CLAUDE.md): it is a regression in our own code.

## What the MAC address does today (3.7.5-rc2.0)

Box numbers are the sections of `docs/B54_Flow_Comparison.html`.

- **Box 2:** the ARP sweep collects each host's MAC address.
- **Box 3:** nmap adds the maker name, from its own table or from AnyCam's copy of the
  maker list published by the [Institute of Electrical and Electronics Engineers (IEEE)](https://standards.ieee.org/products-programs/regauth/).
- **Box 4:** the camera / not camera / uncertain classifier (`classify_device`) ignores
  the MAC address. It reads only the hostname, the service names and the port numbers.
- **Box 5:** the "Brand identification" step (`_identify_camera_brand`) runs once for
  every port. The maker name is 1 of 10 text fields it searches for a brand name. That is
  the only place it counts.
- **Box 7:** the appliance pass checks MAC addresses, but only for 2 appliance prefixes.
  It never blocks a probe.

So the MAC address can name the brand, but only after the port loop has started. No MAC
result ever stops a probe.

## Limits of a MAC-based brand check

- **Rebranded cameras:** Amcrest and Lorex are Dahua inside, so their prefix can name
  either maker.
- **Wi-Fi modules:** many low-cost cameras carry the module maker's prefix (a chip
  vendor), which names no camera brand.
- **The ICMP fallback** in box 2 finds hosts without MAC addresses, so those hosts get no
  MAC check.

## Proposal

Bring the check back at the start of box 4, before the ONVIF step and the port loop.

1. A non-camera maker's prefix skips the host, unless WS-Discovery, SSDP or mDNS says it
   is a camera.
2. A camera brand's prefix sets the brand before port 554, so B54's brand-paths-first rule
   has a brand from the first request.
3. Any other prefix changes nothing: the scan continues as today.

## Code references

- `anycam_probe.py`: `is_camera_positive()` (unused since 2.1.0), its MAC step "1b. OUI
  camera-positive".
- `anycam_brand.py`: `oui_is_camera()`, `lookup_oui()`, `_CAMERA_OUI_VENDORS`,
  `_NON_CAMERA_OUI_VENDORS`, `_identify_camera_brand()` (MAC maker name in its search
  text).
- `anycam_scan.py`: `classify_device()` (no MAC input); the nmap result parser that fills
  `mac_addr` and `mac_vendor`.
- Git: the 2.0.6 commit removed the call; the 2.1.0 commit rebuilt `_probe_host_port`
  without it.
