"""
PC tool: builds airports.txt, the airport lookup table flight_tracker.py uses on the board.

Source: OurAirports' public-domain airports.csv. Keeps every airport that has an IATA code, plus
every large/medium airport, worldwide. Writes one line per airport:

    IDENT,IATA,LOCAL,lat,lon,Name

IDENT is OurAirports' ident (usually the ICAO code, e.g. KMSO), IATA is e.g. MSO, LOCAL is the
national code (the FAA LID in the US). Empty fields stay empty. Names are cut to 30 characters,
with commas removed.

Usage:
    .venv\\Scripts\\python.exe make_airports.py              (downloads airports.csv)
    .venv\\Scripts\\python.exe make_airports.py -i airports.csv
    mpremote connect COM12 cp airports.txt :airports.txt
"""

import argparse
import csv
import io
import unicodedata
import urllib.request

URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("-i", "--input", help="local airports.csv instead of downloading it")
    ap.add_argument("-o", "--output", default="airports.txt")
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        print("Downloading", URL)
        req = urllib.request.Request(URL, headers={"User-Agent": "fnk0104n-flight-tracker"})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8")

    lines = []
    for row in csv.DictReader(io.StringIO(text)):
        iata = row["iata_code"].strip().upper()
        if row["type"] == "closed":
            continue
        if not iata and row["type"] not in ("large_airport", "medium_airport"):
            continue
        ident = row["ident"].strip().upper()
        local = row["local_code"].strip().upper()
        # ASCII only: the board's fonts have no accented glyphs. Dashes become "-", accents
        # are dropped (e.g. "Seattle–Tacoma" -> "Seattle-Tacoma", "Zürich" -> "Zurich").
        name = row["name"].replace(",", " ")
        for dash in "‐‑‒–—":
            name = name.replace(dash, "-")
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        name = " ".join(name.split())[:30]
        lat = float(row["latitude_deg"])
        lon = float(row["longitude_deg"])
        lines.append("%s,%s,%s,%.4f,%.4f,%s" % (ident, iata, local, lat, lon, name))

    with open(args.output, "w", encoding="ascii", errors="replace", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    size = sum(len(s) + 1 for s in lines)
    print("Wrote %d airports to %s (%d bytes)" % (len(lines), args.output, size))
    print("Copy to the board with:  mpremote connect COM12 cp %s :airports.txt" % args.output)


if __name__ == "__main__":
    main()
