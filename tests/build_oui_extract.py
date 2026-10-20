"""Rebuild the camera MAC prefix list from the IEEE registry (3.8.3, B64).

Reads Wireshark's copy of the IEEE registry (the "manuf" file, all MA-L, MA-M
and MA-S blocks; https://www.wireshark.org/download/automated/data/manuf),
picks every block of the camera-only makers below, and writes:

  tests/data/oui_camera_extract.txt   the registry lines, for test AS (owners)
  stdout                              the _CAMERA_OUI_VENDORS block for anycam_brand.py

Run:  python -I tests/build_oui_extract.py <path to manuf>

A maker that also makes routers, phones, computers or other non-camera network
devices is left out on purpose (Ubiquiti, TP-Link, D-Link, Bosch, Sony, Panasonic,
Samsung Electronics, Wyze, Ring, EZVIZ, Verkada, Kedacom, Speco, Honeywell): the
list says "camera" for every device with the prefix.
"""
import sys
from pathlib import Path

# brand label -> the registry's company names (exact)
MAKERS = {
    "Hikvision": ["Hangzhou Hikvision Digital Technology Co.,Ltd.", "Prama Hikvision India Private Limited"],
    "Dahua": ["Zhejiang Dahua Technology Co., Ltd.", "Zhejiang Dahua Technologyco.,Ltd"],
    "Axis": ["Axis Communications AB"],
    "Hanwha / Samsung Techwin": ["Samsung Techwin Co.,Ltd", "Hanwha Vision VietNam",
                                 "Hanwha Vision Vietnam Company Limited"],
    "Reolink": ["Reolink Innovation Limited"],
    "Uniview (UNV)": ["Zhejiang Uniview Technologies Co., Ltd.", "Zhejiang Uniview Technologies Co.,Ltd."],
    "Vivotek": ["Vivotek, Inc."],
    "GeoVision": ["GeoVision Inc."],
    "Mobotix": ["Mobotix AG"],
    "ACTi": ["ACTi Corporation"],
    "Lorex": ["Lorex Technology Inc."],
    "Amcrest": ["Amcrest Technologies"],
    "Swann": ["Swann Communications Pty Ltd", "Swann communications Pty Ltd"],
    "Night Owl": ["Night Owl SP"],
    "i-PRO": ["i-PRO Co., Ltd."],
    "FLIR": ["FLIR Systems", "FLIR Systems Inc"],
    "Tiandy": ["Tiandy Technologies Co.,LTD"],
    "Arecont Vision": ["Arecont Vision"],
    "Digital Watchdog": ["Digital Watchdog"],
    "March Networks": ["March Networks Corporation", "March Networks S.p.A."],
    "Sunell": ["Sunell Electronics Co."],
    "TVT": ["Tvt Co.,Ltd", "Tvt Co., Ltd"],
    "Arlo": ["Arlo Technology"],
    "INSTAR": ["INSTAR Deutschland GmbH"],
    "IQinVision": ["IQinVision"],
    "OpenEye": ["Openeye"],
    "Luxonis / OAK": ["Luxonis Holding Corporation"],
    "Hipcam/Microseven": ["Microseven Systems, LLC"],
}

EXTRACT = Path(__file__).resolve().parent / "data" / "oui_camera_extract.txt"


def block_prefix(field: str) -> str:
    """The registry's "44:A9:2C:30/28" as the prefix AnyCam matches: "44:A9:2C:3"."""
    addr, _, bits = field.partition("/")
    hexes = addr.replace(":", "").upper()
    n = int(bits) // 4 if bits else 6
    hexes = hexes[:n]
    return ":".join(hexes[i:i + 2] for i in range(0, len(hexes), 2))


def main(manuf: str) -> None:
    owner = {name: brand for brand, names in MAKERS.items() for name in names}
    rows = []
    for line in open(manuf, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3 and parts[2].strip() in owner:
            rows.append((parts[0].strip(), parts[2].strip()))
    missing = [n for n in owner if n not in {r[1] for r in rows}]
    if missing:
        sys.exit(f"not in the registry: {missing}")
    EXTRACT.parent.mkdir(exist_ok=True)
    EXTRACT.write_text("# IEEE registry lines (via Wireshark's manuf file) for the camera makers in\n"
                       "# anycam_brand._CAMERA_OUI_VENDORS. Made by tests/build_oui_extract.py.\n"
                       + "".join(f"{p}\t{n}\n" for p, n in sorted(rows)), encoding="utf-8", newline="\n")
    print("_CAMERA_OUI_VENDORS: dict[str, str] = {")
    for brand in MAKERS:
        prefixes = sorted(block_prefix(p) for p, n in rows if owner[n] == brand)
        print(f"    # {brand}: {len(prefixes)}")
        for i in range(0, len(prefixes), 5):
            print("    " + " ".join(f'"{p}": "{brand}",' for p in prefixes[i:i + 5]))
    print("}")
    print(f"# {len(rows)} blocks", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
