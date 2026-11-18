/* Distinct resolved people, never counts of service records or paper authors. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DBVenueOverlap = api;
})(globalThis, () => {
  'use strict';
  const venueOrder = ['SIGMOD', 'VLDB', 'ICDE', 'EDBT', 'CIDR'];
  // Match review roles explicitly. Award committees and program/general chairs
  // are intentionally not inferred to be reviewers from a seniority label.
  const researchRoles = new Set([
    'pc_member', 'program_committee_member', 'review_board_member',
    'senior_program_committee_member', 'associate_editor', 'area_chair',
    'associate_chair', 'pods_pc_member',
  ]);
  const technicalRoles = new Set(['industrial_pc_member', 'demo_pc_member']);
  function roleMatches(role, scope = 'research') {
    const token = String(role || '').trim().toLowerCase().replace(/[\s/-]+/g, '_');
    if (token.includes('shadow')) return false;
    if (scope === 'service') return true;
    return researchRoles.has(token) || (scope === 'reviewers' && technicalRoles.has(token));
  }

  function buildModel(people, { year = 'all', roles = 'research', sameYear = false } = {}) {
    const members = new Map();
    for (const person of people) {
      const pid = String(person.dblpPid || '').trim();
      if (!pid) continue;
      for (const event of person.db?.events || []) {
        const eventYear = Number(event.year);
        if (!Number.isInteger(eventYear) || eventYear < 1900 || !venueOrder.includes(event.venue)) continue;
        if (String(event.track || '').toLowerCase().includes('shadow')) continue;
        if (year !== 'all' && eventYear !== Number(year)) continue;
        if (!roleMatches(event.role, roles)) continue;
        if (!members.has(pid)) members.set(pid, { pid, person, venues: new Map(), events: [], eventKeys: new Set() });
        const item = members.get(pid);
        if (!item.venues.has(event.venue)) item.venues.set(event.venue, new Set());
        item.venues.get(event.venue).add(eventYear);
        const eventKey = JSON.stringify([event.venue, eventYear, event.role, event.sourceUrl || '']);
        if (!item.eventKeys.has(eventKey)) {
          item.eventKeys.add(eventKey);
          item.events.push(event);
        }
      }
    }
    const rosters = new Map(venueOrder.map(venue => [venue, new Set()]));
    for (const item of members.values()) for (const venue of item.venues.keys()) rosters.get(venue).add(item.pid);
    const cells = new Map();
    for (const row of venueOrder) {
      const columns = new Map();
      for (const column of venueOrder) {
        const pids = new Set();
        for (const pid of rosters.get(row)) {
          const item = members.get(pid), columnYears = item.venues.get(column);
          if (!columnYears) continue;
          if (!sameYear || [...item.venues.get(row)].some(y => columnYears.has(y))) pids.add(pid);
        }
        const total = rosters.get(row).size;
        columns.set(column, { pids, count: pids.size, total, share: total ? pids.size / total : null });
      }
      cells.set(row, columns);
    }
    const distribution = venueOrder.map((_, index) => ({ venues: index + 1, count: 0 }));
    for (const item of members.values()) {
      const perYear = new Map();
      for (const years of item.venues.values()) for (const year of years) perYear.set(year, (perYear.get(year) || 0) + 1);
      item.venueCount = sameYear ? Math.max(...perYear.values()) : item.venues.size;
      distribution[item.venueCount - 1].count++;
    }
    const multiple = distribution.filter(bin => bin.venues > 1).reduce((sum, bin) => sum + bin.count, 0);
    return { members, rosters, cells, distribution, total: members.size, multiple, sameYear };
  }

  function sharedPeople(model, row, column) {
    const pids = model.cells.get(row)?.get(column)?.pids || new Set();
    return [...pids].map(pid => model.members.get(pid)).sort((a, b) =>
      String(a.person.name).localeCompare(String(b.person.name)) || a.pid.localeCompare(b.pid)
    );
  }
  function sharedYears(item, row, column) {
    const other = item.venues.get(column) || new Set();
    return [...(item.venues.get(row) || [])].filter(y => other.has(y)).sort((a, b) => a - b);
  }
  function csvField(value) {
    let text = String(value ?? '');
    if (/^[=+\-@]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  }
  function toCsv(items, row, column, options = {}) {
    const rows = [['Researcher', 'DBLP PID', 'Affiliation', 'Row venue', 'Column venue',
      'Row service years', 'Column service years', 'Shared years', 'Role scope', 'Match', 'Source URLs']];
    for (const item of items) {
      const sources = [...new Set(item.events.filter(e => e.venue === row || e.venue === column).map(e => e.sourceUrl).filter(Boolean))];
      rows.push([item.person.name, item.pid, item.person.primaryAffiliation || '', row, column,
        [...(item.venues.get(row) || [])].sort().join('; '), [...(item.venues.get(column) || [])].sort().join('; '),
        sharedYears(item, row, column).join('; '), options.roles || 'research',
        options.sameYear ? 'Same year' : 'Selected period', sources.join('; ')]);
    }
    return '\uFEFF' + rows.map(fields => fields.map(csvField).join(',')).join('\r\n') + '\r\n';
  }
  function toMembershipCsv(items, options = {}) {
    const rows = [['Researcher', 'DBLP PID', 'Affiliation', 'Number of venues', 'Venues', 'Service years', 'Role scope', 'Match', 'Source URLs']];
    for (const item of items) rows.push([
      item.person.name, item.pid, item.person.primaryAffiliation || '', item.venueCount,
      [...item.venues.keys()].join('; '), [...new Set(item.events.map(e => e.year))].sort().join('; '),
      options.roles || 'research', options.sameYear ? 'Maximum in one year' : 'Selected period',
      [...new Set(item.events.map(e => e.sourceUrl).filter(Boolean))].join('; '),
    ]);
    return '\uFEFF' + rows.map(fields => fields.map(csvField).join(',')).join('\r\n') + '\r\n';
  }
  const limitedEvidenceGroups = Object.freeze([
    { key: 'no-papers', label: 'No qualifying papers', description: 'No papers meet the selected DB or ML conference-paper rules.' },
    { key: 'ratio', label: 'DB:ML ratio below 2:1', description: 'DB papers are fewer than twice the ML papers. The publication-evidence minimum is also unmet.' },
    { key: 'db-evidence', label: 'Too little DB publication evidence', description: 'The 2:1 ratio is met, but the fallback needs two DB papers in two distinct publication years.' },
    { key: 'pc-service', label: 'Only one PC service year', description: 'The fallback\u2019s DB paper requirements are met. Qualifying PC service is recorded in only one year.' },
  ].map(Object.freeze));
  // Explain existing Limited evidence decisions; this never changes inclusion.
  // First matching reason wins, so the segments form an exclusive partition.
  function limitedEvidenceReason(focus) {
    if (focus?.label !== 'Limited evidence') return null;
    const { dbPapers, mlPapers, dbPublicationYears, serviceYears } = focus;
    if (![dbPapers, mlPapers, dbPublicationYears, serviceYears].every(Number.isFinite)) return null;
    if (dbPapers + mlPapers === 0) return 'no-papers';
    if (dbPapers < 2 * mlPapers) return 'ratio';
    if (dbPapers < 2 || dbPublicationYears < 2) return 'db-evidence';
    if (serviceYears < 2) return 'pc-service';
    return null;
  }
  function toFocusCsv(items) {
    const rows = [['Researcher', 'DBLP PID', 'Profile', 'DB conference papers', 'ML conference papers', 'Publication years', 'DB publication years', 'PC service years', 'Included', 'Basis', 'Reason']];
    for (const item of items) {
      const focus = item.person.focus;
      rows.push([item.person.name, item.pid, focus.label, focus.dbPapers, focus.mlPapers, focus.publicationYears, focus.dbPublicationYears, focus.serviceYears, focus.included, focus.basis, focus.reason]);
    }
    return '\uFEFF' + rows.map(fields => fields.map(csvField).join(',')).join('\r\n') + '\r\n';
  }
  return Object.freeze({ venueOrder, roleMatches, buildModel, sharedPeople, sharedYears, toCsv, toMembershipCsv, toFocusCsv, limitedEvidenceGroups, limitedEvidenceReason });
});
