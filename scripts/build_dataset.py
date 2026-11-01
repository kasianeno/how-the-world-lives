"""Build data/world.parquet from Our World in Data.

Downloads four OWID grapher tables, joins them on country code and year,
keeps 1990-2023 and countries/territories with more than 100,000 people in 2023,
adds ISO codes and centroid coordinates, and writes a compact Parquet file.

Usage:
    python -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt
    .venv/bin/python scripts/build_dataset.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import duckdb
import pycountry

YEAR_FROM, YEAR_TO = 1990, 2023
MIN_POPULATION = 100_000  # in YEAR_TO

OWID = "https://ourworldindata.org/grapher/{}.csv?v=1&csvType=full&useColumnShortNames=true"
TABLES = {
    "life": "life-expectancy",                                  # life_expectancy_0
    "population": "population",                                 # population_historical
    "gdp": "gdp-per-capita-worldbank",                          # ny_gdp_pcap_pp_kd
    "continents": "continents-according-to-our-world-in-data",  # owid_region
}
CENTROIDS = "https://raw.githubusercontent.com/gavinr/world-countries-centroids/master/dist/countries.csv"
# The centroid file has no separate rows for these two; coordinates added by hand.
EXTRA_CENTROIDS = {"HKG": (114.17, 22.32), "MAC": (113.55, 22.19)}

OUT = Path(__file__).resolve().parent.parent / "data" / "world.parquet"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "how-the-world-lives/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> None:
    work = OUT.parent / ".cache"
    work.mkdir(parents=True, exist_ok=True)
    for name, slug in TABLES.items():
        path = work / f"{name}.csv"
        if not path.exists():
            print(f"downloading {slug}")
            path.write_bytes(fetch(OWID.format(slug)))
    centroids = work / "centroids.csv"
    if not centroids.exists():
        print("downloading centroids")
        centroids.write_bytes(fetch(CENTROIDS))

    con = duckdb.connect()
    con.execute(f"""
        CREATE TABLE joined AS
        SELECT l.code                              AS iso3,
               l.entity                            AS country,
               l.year::SMALLINT                    AS "year",
               ROUND(l.life_expectancy_0, 2)       AS life_expectancy,
               ROUND(g.ny_gdp_pcap_pp_kd, 0)       AS gdp_per_capita,
               p.population_historical::BIGINT    AS population,
               c.owid_region                       AS continent
        FROM read_csv_auto('{work / "life.csv"}') l
        JOIN read_csv_auto('{work / "population.csv"}') p USING (code, year)
        LEFT JOIN read_csv_auto('{work / "gdp.csv"}') g USING (code, year)
        JOIN read_csv_auto('{work / "continents.csv"}') c ON c.code = l.code
        WHERE l.code IS NOT NULL
          AND l.code NOT LIKE 'OWID%'          -- drop OWID aggregates (World, regions)
          AND l.year BETWEEN {YEAR_FROM} AND {YEAR_TO}
    """)

    keep = [r[0] for r in con.execute(
        f'SELECT iso3 FROM joined WHERE "year" = {YEAR_TO} AND population >= {MIN_POPULATION}'
    ).fetchall()]
    codes = []
    for iso3 in keep:
        c = pycountry.countries.get(alpha_3=iso3)
        if c is None:
            print(f"skipping {iso3}: no ISO record", file=sys.stderr)
            continue
        codes.append((iso3, c.alpha_2, c.numeric))
    con.execute("CREATE TABLE iso (iso3 VARCHAR, iso2 VARCHAR, iso_num VARCHAR)")
    con.executemany("INSERT INTO iso VALUES (?, ?, ?)", codes)

    con.execute(f"""
        CREATE TABLE cen AS
        SELECT ISO AS iso2, any_value(longitude) AS lon, any_value(latitude) AS lat
        FROM read_csv_auto('{centroids}', nullstr = '')   -- keep Namibia's "NA" code
        GROUP BY 1
    """)

    con.execute("""
        CREATE TABLE world AS
        SELECT j.country, i.iso3, i.iso2, i.iso_num, j.continent, j."year",
               j.life_expectancy, j.gdp_per_capita, j.population,
               ROUND(cen.lon, 2) AS lon, ROUND(cen.lat, 2) AS lat
        FROM joined j
        JOIN iso i USING (iso3)
        LEFT JOIN cen USING (iso2)
    """)
    for iso3, (lon, lat) in EXTRA_CENTROIDS.items():
        con.execute("UPDATE world SET lon = ?, lat = ? WHERE iso3 = ?", [lon, lat, iso3])

    con.execute(f"""
        COPY (SELECT * FROM world ORDER BY country, "year")
        TO '{OUT}' (FORMAT parquet, COMPRESSION zstd)
    """)
    n, k, g = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT country), COUNT(gdp_per_capita) FROM world"
    ).fetchone()
    print(f"wrote {OUT.relative_to(OUT.parent.parent)}: {n:,} rows, {k} countries, {n - g:,} rows without GDP")


if __name__ == "__main__":
    main()
