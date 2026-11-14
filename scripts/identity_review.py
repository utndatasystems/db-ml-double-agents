"""Read-only identity review selection shared by the pipeline and coverage export."""


def sparse_exact_review_groups(conn):
    """Return accepted sparse profiles with real competing name variants.

    Inspect actual candidates rather than a cached count. Missing identity
    metadata is a review trigger, not evidence that a person was misidentified.
    Callers may paginate or bound this deterministic list without changing it.
    """
    required = {
        'name_resolution': {'name_key', 'representative_name', 'resolved_dblp_pid', 'status'},
        'name_groups': {'name_key', 'affiliations_json', 'venues_json', 'observation_count'},
        'dblp_people': {'dblp_pid', 'affiliations_json', 'verified_orcids_json'},
        'dblp_candidates': {'name_key', 'dblp_pid', 'match_type'},
    }
    for table, columns in required.items():
        available = {row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')}
        if not columns <= available:
            # Legacy inputs cannot measure this review criterion. Exporters
            # should distinguish unavailable evidence from a measured zero.
            return []
    cursor = conn.execute("""
        SELECT r.name_key,r.representative_name,r.resolved_dblp_pid,r.status,
               g.affiliations_json,g.venues_json,g.observation_count,
               (SELECT COUNT(DISTINCT c.dblp_pid) FROM dblp_candidates c
                JOIN dblp_people other ON other.dblp_pid=c.dblp_pid
                WHERE c.name_key=r.name_key AND c.dblp_pid<>r.resolved_dblp_pid
                  AND c.match_type IN ('first_last_variant','initial_last_variant')) AS real_variant_count
        FROM name_resolution r JOIN name_groups g USING(name_key)
        JOIN dblp_people p ON p.dblp_pid=r.resolved_dblp_pid
        WHERE r.status='resolved_unique_exact'
          AND p.affiliations_json='[]' AND p.verified_orcids_json='[]'
          AND EXISTS (SELECT 1 FROM dblp_candidates c JOIN dblp_people other ON other.dblp_pid=c.dblp_pid
                      WHERE c.name_key=r.name_key AND c.dblp_pid<>r.resolved_dblp_pid
                        AND c.match_type IN ('first_last_variant','initial_last_variant'))
        ORDER BY r.representative_name COLLATE NOCASE,r.name_key
    """)
    fields = [x[0] for x in cursor.description]
    return [dict(zip(fields, row)) for row in cursor]
