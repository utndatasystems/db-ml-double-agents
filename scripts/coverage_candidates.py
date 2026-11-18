"""Expose stored identity evidence for review without changing resolution decisions."""
import json

try:
    from .audit_unresolved_entity_resolution import compatible_full_name, best_safe_affiliation_equivalence
except ImportError:
    from audit_unresolved_entity_resolution import compatible_full_name, best_safe_affiliation_equivalence


def strings(value):
    values = json.loads(value or '[]')
    return sorted({v for v in values if isinstance(v, str) and v.strip()})


class CandidateReview:
    def __init__(self, conn, ror=None):
        self.conn = conn
        self.ror = ror
        self.columns = {r[1] for r in conn.execute('PRAGMA table_info(dblp_people)')}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.nonperson_counts = (dict(conn.execute('SELECT name_key,COUNT(DISTINCT dblp_pid) FROM dblp_nonperson_candidates GROUP BY name_key'))
                                 if 'dblp_nonperson_candidates' in tables else {})
        self.resolved = {r[0] for r in conn.execute('SELECT DISTINCT resolved_dblp_pid FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL')}

    def credible_alternative(self, resolution, alternative):
        # Older databases cannot distinguish affiliation-word flags from exact
        # institution evidence. Retain those flags as unverified review items.
        if 'affiliations_json' not in self.columns:
            return True
        alias_select = 'aliases_json' if 'aliases_json' in self.columns else 'NULL'
        candidate = self.conn.execute('SELECT affiliations_json,'+alias_select+' FROM dblp_people WHERE dblp_pid=?',
                                      (alternative['dblp_pid'],)).fetchone()
        if not candidate or not any(compatible_full_name(resolution['representative_name'], name)
                                    for name in [alternative['canonical_name']] + strings(candidate[1])):
            return False
        service = strings(resolution['affiliations_json'])
        affiliations = strings(candidate[0])
        if self.ror:
            return bool(set(self.ror.match_affiliations(service)) & set(self.ror.match_affiliations(affiliations)))
        return bool(best_safe_affiliation_equivalence(service, affiliations)[0])

    def for_name(self, resolution):
        row = dict(resolution)
        metadata = ['affiliations_json', 'verified_orcids_json', 'snapshot_id']
        selects = [f'p.{key}' if key in self.columns else f'NULL AS {key}' for key in metadata]
        query = ('SELECT c.*,p.canonical_name,' + ','.join(selects)
                 + ' FROM dblp_candidates c LEFT JOIN dblp_people p USING(dblp_pid) WHERE c.name_key=?')
        by_pid = {}
        for candidate_row in self.conn.execute(query, (row['name_key'],)):
            c = dict(candidate_row)
            item = by_pid.setdefault(c['dblp_pid'], {
                'pid': c['dblp_pid'], 'label': c['canonical_name'] or c['dblp_pid'],
                'url': 'https://dblp.org/pid/' + c['dblp_pid'], 'matchTypes': set(), 'matchedAliases': set(),
                'affiliations': strings(c['affiliations_json']), 'orcids': strings(c['verified_orcids_json']),
                'snapshot': c['snapshot_id'], 'affiliationOverlap': 0,
                'alreadyResolved': c['dblp_pid'] in self.resolved})
            if c.get('match_type'):
                item['matchTypes'].add(c['match_type'])
            if c.get('matched_alias'):
                item['matchedAliases'].add(c['matched_alias'])
            item['affiliationOverlap'] = max(item['affiliationOverlap'], c.get('affiliation_similarity') or 0)
        affiliations = strings(row.get('affiliations_json'))
        candidates = list(by_pid.values())
        for c in candidates:
            c['matchTypes'] = sorted(c['matchTypes'])
            c['matchedAliases'] = sorted(c['matchedAliases'])
            if not affiliations or not c['affiliations']:
                c['affiliationOverlap'] = None
        current_pid = row.get('resolved_dblp_pid')
        for c in candidates:
            c['selected'] = c['pid'] == current_pid
        candidates.sort(key=lambda c: (not c['selected'], 'exact_alias' not in c['matchTypes'], -(c['affiliationOverlap'] or 0), c['label'].casefold(), c['pid']))
        status = row['status']
        reason = ('Several DBLP profiles match the normalized name. No unique identity passed the matching rules.'
                  if status == 'ambiguous_exact_name' else
                  'Several DBLP profiles match a relaxed name rule. More identity evidence is needed.'
                  if status.startswith('ambiguous') else
                  'A relaxed name match exists but has not passed the identity rules.')
        exact_count = sum('exact_alias' in c['matchTypes'] for c in candidates)
        if current_pid:
            reason = ('This exact-name match is accepted, but its selected profile has no recorded affiliation or ORCID. '
                      'Other name candidates exist. Review the evidence before changing the match; missing metadata is not proof of an error.')
        if status == 'ambiguous_exact_name' and exact_count < 2 and self.nonperson_counts.get(row['name_key']):
            reason = ('One real profile matches the exact name, but DBLP also marks this name as ambiguous. More identity evidence is needed.'
                      if exact_count else
                      'The exact-name DBLP record is a disambiguation page, not a person. Name variants need review.')
        return {'status': status, 'reason': reason, 'currentPid': current_pid, 'recordedNote': row.get('notes') or '',
                'excludedDisambiguationCount': self.nonperson_counts.get(row['name_key'], 0),
                'serviceAffiliations': affiliations, 'serviceVenues': strings(row.get('venues_json')),
                'recordedCandidateCount': row['all_candidate_count'],
                'exactCandidateCount': exact_count,
                'candidates': candidates}
