"""Read-only reconciliation of promoted exports against their pre-refresh manifests."""
import contextlib
import hashlib
import io
import json
import tempfile
from collections import Counter
from pathlib import Path

import numerical_probe

HERE = Path(__file__).parent
ROOT = HERE.parents[2]


def load_site(path):
    source = path.read_text(encoding="utf8")
    return json.loads(source[source.index("=")+1:].rstrip(";\n"))


def main_rows(site):
    return {(p["dblpPid"], r.get("workId"), r.get("dblpKey"), r["venue"], r["year"])
            for p in site["people"] for r in p["ml"]["papers"] if r["scope"] == "main"}


def main():
    # Reuse the initial independent CSV arithmetic without overwriting its evidence.
    initial = json.loads((HERE / "numerical_evidence.json").read_text())
    with tempfile.TemporaryDirectory(dir=HERE) as folder:
        numerical_probe.OUT = Path(folder)
        with contextlib.redirect_stdout(io.StringIO()): numerical_probe.main()
        final = json.loads((Path(folder) / "numerical_evidence.json").read_text())
    old = load_site(HERE / "previous_manifests/site_data.js")
    current = load_site(ROOT / "website/data/site_data.js")
    old_home = json.loads((HERE / "previous_manifests/home_summary.json").read_text())
    current_home = json.loads((ROOT / "website/data/home_summary.json").read_text())
    old_rows, current_rows = main_rows(old), main_rows(current)
    old_pids = {p["dblpPid"] for p in old["people"]}
    current_pids = {p["dblpPid"] for p in current["people"]}
    old_pc = old["people"] + old["additionalExplorerPeople"]
    current_pc = current["people"] + current["additionalExplorerPeople"]
    old_focus = Counter(p["focus"]["label"] for p in old_pc)
    current_focus = Counter(p["focus"]["label"] for p in current_pc)
    checks = {
        "initialNumericalEvidenceExactlyReproduced": initial == final,
        "entireHomeSummaryUnchanged": old_home == current_home,
        "allMainPidWorkKeyVenueYearMembershipUnchanged": old_rows == current_rows,
        "includedCohortPidsUnchanged": old_pids == current_pids,
        "allVenueYearDenominatorsUnchanged": old["mainMlVenueYearTotals"] == current["mainMlVenueYearTotals"],
        "allSeventyFiveAnnualPointsUnchanged": old_home["publicationTrend"] == current_home["publicationTrend"],
        "allTenFirstTimeCountsUnchanged": old_home["firstTimePublishers"] == current_home["firstTimePublishers"],
    }
    assert all(checks.values()), checks
    evidence = {"checks": checks, "finalCounts":final["counts"],
                "mainRowsRemoved":sorted(old_rows-current_rows), "mainRowsAdded":sorted(current_rows-old_rows),
                "includedPidsRemoved":sorted(old_pids-current_pids), "includedPidsAdded":sorted(current_pids-old_pids),
                "pcCandidates":{"before":len(old_pc),"after":len(current_pc)},
                "focusLabels":{"before":dict(old_focus),"after":dict(current_focus)},
                "summaryCounts":{"before":{k:old["summary"][k] for k in ("dbResolvedPeople","sitePeople","mainCrossoverPeople","uniqueMainPapers")},
                                 "after":{k:current["summary"][k] for k in ("dbResolvedPeople","sitePeople","mainCrossoverPeople","uniqueMainPapers")}},
                "mainPidScopeCount":len({row[0] for row in current_rows}),
                "independentFinalNumerics":final,
                "inputSha256":{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in [ROOT/"website/data/site_data.js", ROOT/"website/data/home_summary.json",
                                            ROOT/"data/dblp_publication_entities_for_db_people.csv"]}}
    (HERE / "numerical_final_evidence.json").write_text(json.dumps(evidence, indent=2)+"\n",encoding="utf8")
    print(json.dumps({k:v for k,v in evidence.items() if k != "independentFinalNumerics"},indent=2))


if __name__ == "__main__": main()
