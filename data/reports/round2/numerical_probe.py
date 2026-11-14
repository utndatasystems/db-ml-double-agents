"""Independent small numerical/temporal audit; production inputs are read only."""
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent


def fit_exponential(points):
    first, last = points[0]["year"], points[-1]["year"]
    def error(k):
        weights = [math.exp(k * (p["year"] - first) / (last - first)) for p in points]
        a = sum(w * p["firstTime"] for w, p in zip(weights, points)) / sum(w*w for w in weights)
        return sum((p["firstTime"] - a*w)**2 for w, p in zip(weights, points)), a
    # Independently bracket with a finer grid, then golden-section minimization.
    ks = [-20 + i / 20 for i in range(801)]
    best = min(range(len(ks)), key=lambda i: error(ks[i])[0])
    low, high = ks[best-1], ks[best+1]
    phi = (math.sqrt(5)-1)/2
    for _ in range(100):
        left, right = high-phi*(high-low), low+phi*(high-low)
        if error(left)[0] < error(right)[0]: high = right
        else: low = left
    k = (low+high)/2
    return {"rate": math.expm1(k/(last-first)), "squaredError": error(k)[0], "amplitude": error(k)[1]}


def main():
    source = (ROOT / "website/data/site_data.js").read_text(encoding="utf8")
    data = json.loads(source[source.index("=")+1:].rstrip(";\n"))
    home = json.loads((ROOT / "website/data/home_summary.json").read_text())
    people = data["people"]
    cohort = {p["dblpPid"] for p in people}
    works = defaultdict(list)
    pairs = set()
    first = {}
    by_venue_year = defaultdict(set)
    future = []
    csv_rows = []
    for row in csv.DictReader((ROOT / "data/dblp_publication_entities_for_db_people.csv").open(encoding="utf-8-sig", newline="")):
        if row["dblp_pid"] not in cohort or row["scope"] != "main": continue
        year, venue, work, pid = int(row["year"]), row["venue"], row["work_id"], row["dblp_pid"]
        csv_rows.append(row)
        works[work].append(row)
        pairs.add((pid, work))
        by_venue_year[venue, year].add(work)
        first[pid] = min(first.get(pid, year), year)
        if year > 2026: future.append({k:row[k] for k in ("dblp_pid", "dblp_key", "year")})
    checks = []
    for item in home["publicationTrend"]["series"]:
        for point in item["points"]:
            expected = len(by_venue_year[item["venue"], point["year"]])
            assert expected == point["papers"], (item["venue"], point, expected)
            denominator = data["mainMlVenueYearTotals"].get(item["venue"], {}).get(str(point["year"]))
            share = 100 * expected / denominator if denominator else None
            assert share == point["share"]
            checks.append((item["venue"], point["year"]))
    for point in home["firstTimePublishers"]["points"]:
        assert sum(y == point["year"] for y in first.values()) == point["firstTime"]
    ambiguous_display = [{"workId":work, "keys":sorted({r["dblp_key"] for r in rows}),
                          "pids":sorted({r["dblp_pid"] for r in rows})}
                         for work, rows in works.items() if len({r["dblp_key"] for r in rows}) > 1]
    rates = []
    pooled = []
    for item in home["publicationTrend"]["series"]:
        available = [p for p in item["points"] if p["year"] >= 2006 and p["share"] is not None and p["share"] > 0]
        start, end = available[0], item["points"][-1]
        rates.append({"venue":item["venue"], "startYear":start["year"], "endYear":end["year"],
                      "startShare":start["share"], "endShare":end["share"],
                      "rate":(end["share"]/start["share"])**(1/(end["year"]-start["year"]))-1})
        window = item["points"][-5:]
        pooled.append({"venue":item["venue"], "period":[p["year"] for p in window],
                       "papers":sum(p["papers"] for p in window), "total":sum(p["total"] for p in window),
                       "share":100*sum(p["papers"] for p in window)/sum(p["total"] for p in window)})
    cumulative = []
    for venue in ("NeurIPS", "ICML", "ICLR"):
        numer = sum(len(ids) for (v,y),ids in by_venue_year.items() if v == venue and y <= 2025 and str(y) in data["mainMlVenueYearTotals"][v])
        denom = sum(n for y,n in data["mainMlVenueYearTotals"][venue].items() if int(y) <= 2025)
        cumulative.append({"venue":venue, "through":2025, "numerator":numer, "denominator":denom, "share":100*numer/denom})
    def main_papers(p): return [r for r in p["ml"]["papers"] if r["scope"] == "main"]
    exported = [r for p in people for r in main_papers(p)]
    main_2026 = [r for r in csv_rows if r["year"] == "2026"]
    evidence = {
        "counts":{"people":len(cohort), "mainAuthors":len(first), "mainWorks":len(works), "mainAuthorships":len(pairs),
                  "csvMainRows":len(csv_rows), "exportMainRows":len(exported), "exportDistinctSelectedKeys":len({r["dblpKey"] for r in exported}),
                  "annualPointsMatchedToCanonicalCsv":len(checks)},
        "mainWorksByVenue":dict(Counter(rows[0]["venue"] for rows in works.values())),
        "duplicateWorkDisplayKeys":ambiguous_display, "afterCurrentYearMainRecords":future,
        "currentYear2026":{"rows":len(main_2026), "works":len({r["work_id"] for r in main_2026}),
                           "authors":len({r["dblp_pid"] for r in main_2026}),
                           "firstTimeAuthors":sum(y == 2026 for y in first.values()), "venues":dict(Counter(r["venue"] for r in main_2026))},
        "annual2025":{s["venue"]:s["points"][-1] for s in home["publicationTrend"]["series"]},
        "pooled2021To2025":pooled, "currentWindowCagr":rates, "cumulativeRelative2025":cumulative,
        "firstTimePoints":home["firstTimePublishers"]["points"], "independentFirstTimeFit":fit_exponential(home["firstTimePublishers"]["points"]),
        "endpointFirstTimeCagr":(52/1)**(1/9)-1,
    }
    (OUT / "numerical_evidence.json").write_text(json.dumps(evidence, indent=2)+"\n",encoding="utf8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__": main()
