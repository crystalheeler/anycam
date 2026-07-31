# Source gap: 2.4.0-rc2.2 through 2.5.0-rc1.7

Release zips for this window were not recoverable when this repository was
reconstructed. The local archive ends at 2.4.0-rc2 and resumes at
2.5.0-rc1.8; everything between existed only in transient working
directories that had been cleared.

The audit reports in this folder document each release in that window.
They cover the changes, rationale, and risks for:

- 2.4.0-rc2.2, rc2.3, rc2.4, rc2.5, rc2.6, rc2.8, rc2.9
- 2.4.0-rc3.0, rc3.3, rc3.5
- 2.4.0-rc4.0
- 2.4.0 (final)
- 2.5.0-rc1.0
- 2.5.0-rc1.7

Notable work landed in this window, including the camera lockout
protections (the `host_skip_layer1_alt` gate and its tightening via
`_alt_skip_via_lockout`), Aggressive Cooldown Detection, and DVR channel
enumeration.

If the missing zips ever surface, they can be grafted in as commits dated
between the surrounding builds.
