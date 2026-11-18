/* Run with Node, or call runDbVenueOverlapTests(DBVenueOverlap) in a browser. */
function runDbVenueOverlapTests(api) {
  let checks = 0;
  function equal(actual, expected, message) {
    checks++;
    if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`${message}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
  }
  const event = (venue, year, role = 'pc_member') => ({ venue, year, role, sourceUrl: `https://example.test/${venue}/${year}` });
  const person = (pid, events, name = 'Same Name') => ({ dblpPid: pid, name, db: { events }, ml: { mainPaperCount: 0 } });
  const people = [
    person('one', [event('SIGMOD', 2022), event('SIGMOD', 2022), event('VLDB', 2023)]),
    person('two', [event('SIGMOD', 2023), event('VLDB', 2023), event('ICDE', 2023, 'area_chair')]),
    person('three', [event('SIGMOD', 2023, 'general_chair'), event('VLDB', 2023)]),
    person('four', [event('ICDE', 2023, 'industrial_pc_member'), event('VLDB', 2023, 'demo_pc_member')]),
    person('five', [event('EDBT', 2023, 'award_committee_member')]),
    person('six', [event('SIGMOD', 2023)]),
    person('seven', [event('SIGMOD', 'bad year'), event('VLDB', null)]),
    { name: 'Unresolved', db: { events: [event('SIGMOD', 2023)] } },
  ];
  const full = api.buildModel(people);
  equal(full.total, 4, 'Distinct matched reviewers, including people without ML papers');
  equal(full.rosters.get('SIGMOD').size, 3, 'Duplicate service records count once');
  equal(full.cells.get('SIGMOD').get('VLDB').count, 2, 'Across-year intersection');
  equal(full.cells.get('VLDB').get('SIGMOD').count, 2, 'Counts are symmetric');
  equal(full.cells.get('SIGMOD').get('VLDB').share, 2 / 3, 'Share uses row denominator');
  equal(full.cells.get('VLDB').get('SIGMOD').share, 2 / 3, 'Other row denominator');
  equal(full.cells.get('EDBT').get('VLDB').share, null, 'No roster has no percentage');
  equal(full.cells.get('SIGMOD').get('SIGMOD').share, 1, 'Diagonal is full roster');
  equal(full.multiple, 2, 'Multi-venue people counted once');
  equal(full.distribution.map(bin => bin.count), [2, 1, 1, 0, 0], 'Exclusive venue-count distribution');
  const same = api.buildModel(people, { sameYear: true });
  equal(same.cells.get('SIGMOD').get('VLDB').count, 1, 'Cross-year service excluded in same-year mode');
  equal(same.cells.get('SIGMOD').get('VLDB').total, 3, 'Same-year mode retains selected-period row population');
  equal(same.multiple, 1, 'Same-year multi-venue people');
  equal(same.distribution.map(bin => bin.count), [3, 0, 1, 0, 0], 'Same-year distribution uses maximum concurrent venues');
  equal(api.sharedYears(same.members.get('two'), 'SIGMOD', 'VLDB'), [2023], 'Shared years');
  const single = api.buildModel(people, { year: 2022 });
  equal(single.total, 1, 'Year filters apply to service, not publications');
  equal(single.cells.get('SIGMOD').get('VLDB').count, 0, 'Service from another year is excluded');
  const reviewers = api.buildModel(people, { roles: 'reviewers' });
  equal(reviewers.total, 5, 'Industry and demo reviewers are optional');
  equal(reviewers.cells.get('ICDE').get('VLDB').count, 2, 'Research and technical reviewer overlap');
  const service = api.buildModel(people, { roles: 'service' });
  equal(service.total, 6, 'Organizer option includes chairs and award roles');
  equal(service.cells.get('SIGMOD').get('VLDB').count, 3, 'Chairs excluded by default but included explicitly');
  const asym = api.buildModel([...people, person('extra', [event('VLDB', 2023)])]);
  equal(asym.cells.get('SIGMOD').get('VLDB').share, 2 / 3, 'Directional share, SIGMOD row');
  equal(asym.cells.get('VLDB').get('SIGMOD').share, 2 / 4, 'Directional share, VLDB row');
  const aliases = api.buildModel([...people, person('one', [event('CIDR', 2023)], 'An Alias')]);
  equal(aliases.total, 4, 'Two aliases of one PID remain one person');
  equal(aliases.cells.get('SIGMOD').get('CIDR').count, 1, 'Alias evidence joins by PID');
  equal(api.roleMatches('award_committee_member', 'research'), false, 'Awards are not assumed to be reviewers');
  equal(api.roleMatches('editor_in_chief', 'research'), false, 'Leadership is not assumed to be reviewing');
  equal(api.roleMatches('pods_pc_member', 'research'), true, 'PODS PC is included');
  equal(api.roleMatches('shadow_pc_member', 'service'), false, 'Shadow PCs excluded even in all-service scope');
  equal(api.buildModel([person('shadow', [{...event('SIGMOD', 2023), track:'shadow'}])], {roles:'service'}).total, 0, 'Shadow tracks excluded');
  const items = api.sharedPeople(full, 'SIGMOD', 'VLDB');
  equal(items.map(p => p.pid), ['one', 'two'], 'People share names but retain distinct identities');
  const csv = api.toCsv(items, 'SIGMOD', 'VLDB', { roles: 'research', sameYear: true });
  equal(csv.split('\r\n').length, 4, 'CSV exports only selected people plus header');
  equal(csv.includes('"Same year"'), true, 'CSV records comparison scope');
  const hostile = { ...items[0], person: { ...items[0].person, name: '=HYPERLINK("url")' } };
  equal(api.toCsv([hostile], 'SIGMOD', 'VLDB').includes('"\'=HYPERLINK(""url"")"'), true, 'CSV treats text as text');
  const membershipCsv = api.toMembershipCsv([full.members.get('two')], {sameYear:true});
  equal(membershipCsv.includes('"Number of venues"'), true, 'Distribution CSV identifies venue-count measure');
  equal(membershipCsv.includes('"Maximum in one year"'), true, 'Distribution CSV identifies same-year definition');
  const limited = (dbPapers, mlPapers, dbPublicationYears, serviceYears) => ({ label: 'Limited evidence', dbPapers, mlPapers, dbPublicationYears, serviceYears });
  equal(api.limitedEvidenceGroups.map(g => g.key), ['no-papers', 'ratio', 'db-evidence', 'pc-service'], 'Limited evidence follows the qualification checks');
  equal(api.limitedEvidenceReason(limited(0, 0, 0, 2)), 'no-papers', 'Zero papers takes precedence over other missing evidence');
  equal(api.limitedEvidenceReason(limited(0, 2, 0, 1)), 'ratio', 'ML-only records fail the ratio, rather than the zero-paper category');
  equal(api.limitedEvidenceReason(limited(2, 2, 1, 1)), 'ratio', 'Ratio failure takes precedence over spread and service failures');
  equal(api.limitedEvidenceReason(limited(1, 0, 1, 1)), 'db-evidence', 'One DB paper is insufficient for the fallback');
  equal(api.limitedEvidenceReason(limited(2, 0, 1, 2)), 'db-evidence', 'Repeated service cannot replace two DB publication years');
  equal(api.limitedEvidenceReason(limited(2, 1, 2, 1)), 'pc-service', 'The exact two-to-one boundary can qualify after another PC year');
  equal(api.limitedEvidenceReason(limited(5, 0, 2, 1)), 'pc-service', 'Enough papers in only two years still needs the fallback');
  equal(api.limitedEvidenceReason({ ...limited(2, 1, 2, 2), label: 'DB-focused' }), null, 'Included fallback profiles never enter the segments');
  equal(api.limitedEvidenceReason({ ...limited(1, 3, 1, 1), label: 'ML-focused' }), null, 'Other focus labels keep their own rows');
  equal(api.limitedEvidenceReason({ label: 'Limited evidence' }), null, 'Unknown metadata does not become a zero-paper claim');
  return checks;
}
/* The two questions have independent controls, drilldowns and CSV context. */
async function runDbVenueOverlapIntegrationTests(doc) {
  let checks = 0;
  const assert = (ok, message) => { if (!ok) throw Error(message); checks++; };
  const win = doc.defaultView, api = win.DBVenueOverlap, people = win.CROSSOVER_DATA.people;
  const fmt = new Intl.NumberFormat('en-US');
  const distribution = doc.querySelector('#db-membership'), matrix = doc.querySelector('#db-overlap');
  const signature = root => root.innerHTML;
  const change = (id, value) => { const input = doc.getElementById(id); input.value = value; input.dispatchEvent(new win.Event('change', {bubbles:true})); };
  const defaultDistribution = signature(distribution);
  assert(!doc.querySelector('#overlap-distribution-summary'), 'Extra two-or-more-venues statement is removed');
  for (const [root, prefix] of [[distribution,'membership'],[matrix,'overlap']]) {
    assert(doc.getElementById(prefix+'-year').value === 'all' && doc.getElementById(prefix+'-roles').value === 'service' && doc.getElementById(prefix+'-match').value === 'period', 'Default uses all years, all roles and the whole period: '+prefix);
    const values=root.querySelectorAll(prefix==='membership'?'.overlap-bar-value > strong':'.overlap-matrix-cell');
    assert([...values].every(value=>/^\d+\.\d%$/.test(value.textContent)), 'Every percentage has one decimal: '+prefix);
  }
  assert(doc.querySelector('#overlap-matrix-title [data-provenance-metric="db_service_overlap"]'), 'Matrix explanation sits beside its question');
  assert(!matrix.querySelector('.eyebrow [data-provenance-metric="db_service_overlap"]'), 'Eyebrow does not carry a chart-specific explanation');
  for (const [id, value] of [['overlap-year','2023'], ['overlap-roles','reviewers'], ['overlap-match','same-year']]) {
    change(id, value);
    assert(signature(distribution) === defaultDistribution, 'Matrix setting leaves venue-count chart unchanged: '+id);
  }
  matrix.querySelector('[data-overlap-view="count"]').click();
  assert(signature(distribution) === defaultDistribution, 'Matrix measurement leaves venue-count chart unchanged');
  const matrixOptions = {year:'2023', roles:'reviewers', sameYear:true};
  const matrixModel = api.buildModel(people, matrixOptions);
  for (const row of api.venueOrder) for (const column of api.venueOrder) {
    assert(matrix.querySelector(`[data-overlap-row="${row}"][data-overlap-column="${column}"]`).textContent === fmt.format(matrixModel.cells.get(row).get(column).count), 'Matrix count matches its independent settings: '+row+'/'+column);
  }
  const matrixSnapshot = signature(matrix);
  for (const [id, value] of [['membership-year','2024'], ['membership-roles','service'], ['membership-match','same-year']]) {
    change(id, value);
    assert(signature(matrix) === matrixSnapshot, 'Venue-count setting leaves matrix unchanged: '+id);
  }
  distribution.querySelector('[data-overlap-view="count"]').click();
  assert(signature(matrix) === matrixSnapshot, 'Venue-count measurement leaves matrix unchanged');
  const distributionOptions = {year:'2024', roles:'service', sameYear:true};
  const distributionModel = api.buildModel(people, distributionOptions);
  for (const bin of distributionModel.distribution) {
    const value = distribution.querySelector(`[data-overlap-bin="${bin.venues}"] .overlap-bar-value > strong`).childNodes[0].textContent;
    assert(value === fmt.format(bin.count), 'Venue-count bars match their independent settings: '+bin.venues);
  }
  const dialog = doc.querySelector('#overlap-dialog');
  let blob, filename;
  const createUrl = win.URL.createObjectURL, click = win.HTMLAnchorElement.prototype.click;
  win.URL.createObjectURL = value => { blob = value; return createUrl.call(win.URL, value); };
  win.HTMLAnchorElement.prototype.click = function() { if (this.download) filename = this.download; else click.call(this); };
  const exportMatches = async (expected, year) => {
    doc.querySelector('#overlap-download').click();
    assert((await blob.text()).replace(/^\uFEFF/, '') === expected.replace(/^\uFEFF/, ''), 'CSV uses the selected chart model and settings');
    assert(filename.endsWith('-'+year+'.csv'), 'CSV filename uses the selected chart year');
  };
  try {
    const bin = distributionModel.distribution.find(bin => bin.count);
    distribution.querySelector(`[data-overlap-bin="${bin.venues}"]`).click();
    assert(dialog.open && doc.querySelector('#overlap-result-count').textContent.includes('of '+fmt.format(bin.count)+' people'), 'Venue-count drilldown has its own exact population');
    assert(doc.querySelector('#overlap-selection-note').textContent.includes('2024'), 'Venue-count popup uses its own year');
    const items = [...distributionModel.members.values()].filter(item => item.venueCount === bin.venues).sort((a,b)=>a.person.name.replace(/\s+0\d{3}$/, '').localeCompare(b.person.name.replace(/\s+0\d{3}$/, '')) || a.pid.localeCompare(b.pid));
    await exportMatches(api.toMembershipCsv(items, distributionOptions), '2024');
    dialog.close();
    const cell = matrixModel.cells.get('SIGMOD').get('VLDB');
    matrix.querySelector('[data-overlap-row="SIGMOD"][data-overlap-column="VLDB"]').click();
    assert(dialog.open && doc.querySelector('#overlap-result-count').textContent.includes('of '+fmt.format(cell.count)+' people'), 'Matrix drilldown has its own exact population');
    assert(doc.querySelector('#overlap-selection-note').textContent.includes('2023'), 'Matrix popup uses its own year');
    const shared = api.sharedPeople(matrixModel, 'SIGMOD', 'VLDB').sort((a,b)=>a.person.name.replace(/\s+0\d{3}$/, '').localeCompare(b.person.name.replace(/\s+0\d{3}$/, '')) || a.pid.localeCompare(b.pid));
    await exportMatches(api.toCsv(shared, 'SIGMOD', 'VLDB', matrixOptions), '2023');
    dialog.close();
    change('overlap-roles', 'service');
    assert(doc.querySelector('#overlap-matrix-title [data-provenance-metric="db_service_overlap"]'), 'Explanation survives a dynamic heading change');
    const button = doc.querySelector('#overlap-matrix-title [data-provenance-metric]');
    button.dispatchEvent(new win.PointerEvent('pointerover', {bubbles:true, pointerType:'mouse'}));
    assert(doc.querySelector('.metric-context-panel[data-metric-id="db_service_overlap"]'), 'Relocated explanation opens on hover');
    doc.dispatchEvent(new win.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  } finally {
    win.URL.createObjectURL = createUrl;
    win.HTMLAnchorElement.prototype.click = click;
    if (dialog.open) dialog.close();
  }
  return checks;
}

if (typeof module === 'object' && module.exports) {
  const api = require('../website/deploy/assets/db-venue-overlap-model.js');
  console.log(`${runDbVenueOverlapTests(api)} overlap checks passed`);
}
