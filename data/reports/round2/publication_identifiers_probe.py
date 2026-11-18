"""Read-only production ownership diagnostics and tiny XML boundary scans."""
import csv
import gzip
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from scan_dblp_publications import TargetPerson, scan


def tiny_scan(targets, body):
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
        path = Path(folder)
        dtd = path / "fixture.dtd"
        dtd.write_text("", encoding="utf-8")
        xml = path / "fixture.xml.gz"
        with gzip.open(xml, "wb") as handle:
            handle.write(("<dblp>" + body + "</dblp>").encode())
        rows, stats, _ = scan(xml, dtd, targets)
        return {"rows": [{k: r[k] for k in ("dblp_pid", "dblp_key", "match_basis", "author_signature")} for r in rows], "stats": stats}


def main():
    conn = sqlite3.connect(f"file:{(ROOT / 'data/entity_resolution.sqlite').as_posix()}?mode=ro", uri=True)
    people = {r[0]: {"name": r[1], "orcids": json.loads(r[2]), "affiliations": json.loads(r[3])} for r in conn.execute("SELECT dblp_pid,canonical_name,verified_orcids_json,affiliations_json FROM dblp_people")}
    resolved = {r[0] for r in conn.execute("SELECT DISTINCT resolved_dblp_pid FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL")}
    cases = json.loads((ROOT / "data/reports/audit_publications_evidence.json").read_text(encoding="utf-8"))["orcidSuspiciousRows"]
    case_ids = {(r["dblp_pid"], r["dblp_key"]) for r in cases}
    counts = Counter()
    raw = []
    with (ROOT / "data/dblp_publications_for_db_people.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            counts[row["match_basis"]] += 1
            if (row["dblp_pid"], row["dblp_key"]) in case_ids:
                owners = [pid for pid, person in people.items() if row["author_orcid"] in person["orcids"]]
                raw.append({k: row[k] for k in ("dblp_pid", "dblp_key", "title", "scope", "match_basis", "author_orcid", "url")} | {"orcidOwners": owners, "orcidOwnerResolved": [pid for pid in owners if pid in resolved]})
    source = TargetPerson("source", "Short Name", ("Short Name",), ())
    target = TargetPerson("target", "Full Name", ("Full Name",), (), (("conf/test/Paper", "source", "Short Name"),))
    paper = '<inproceedings key="conf/test/Paper"><author pid="source">Short Name</author><title>Fixture</title><year>2025</year><booktitle>Test</booktitle></inproceedings>'
    review = tiny_scan([source, target], paper)
    ambiguous = tiny_scan([source], '<inproceedings key="conf/test/Paper"><author>Short Name</author><title>Fixture</title><year>2025</year><booktitle>Test</booktitle></inproceedings><www key="homepages/source"><author>Short Name</author></www><www key="homepages/other"><author>Short Name</author></www>')
    output = {"matchBasisCounts": counts, "cases": raw, "persons": {pid: people[pid] for pid in {r["dblp_pid"] for r in cases} | {p for r in raw for p in r["orcidOwners"]}}, "reviewedSourcePidsResolved": [{"sourcePid": r["source_pid"], "resolved": r["source_pid"] in resolved} for r in csv.DictReader((ROOT / "data/reviewed_publication_authors.csv").open())], "reviewedSameAuthorDualAssignmentFixture": review, "globalAmbiguityFixture": ambiguous}
    (Path(__file__).parent / "publication_identifiers_evidence.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"matchBasisCounts": counts, "caseCount": len(raw), "dualAssignmentFixture": review, "globalAmbiguityFixture": ambiguous}, indent=2))


if __name__ == "__main__":
    main()
