/* Browser integration checks. Pass the document of a rendered All page. */
async function runPopulationFocusTests(doc) {
  let checks = 0;
  const assert = (condition, message) => { if (!condition) throw new Error(message); checks++; };
  const win = doc.defaultView, api = win.DBVenueOverlap, policy = win.CROSSOVER_DATA.populationFocus;
  const limited = policy.candidates.filter(p => p.focus.label === 'Limited evidence');
  const row = doc.querySelector('.focus-group-3');
  const segments = [...row.querySelectorAll('[data-focus-reason]')];
  const fill = row.querySelector('.focus-limited-fill'), track = row.querySelector('.focus-segment-track');
  const format = new Intl.NumberFormat('en-US');
  assert(doc.querySelectorAll('#focus-groups > [data-focus-group]').length === policy.groups.length, 'Keep the four existing population rows');
  assert(segments.length === 4, 'Four inline segments, with no expansion');
  assert(!row.querySelector('button button'), 'Segment controls are not nested buttons');
  assert(Math.abs(fill.getBoundingClientRect().width - track.getBoundingClientRect().width * limited.length / policy.candidateCount) < 1, 'Limited evidence keeps its original width and denominator');
  let total = 0;
  for (const [index, group] of api.limitedEvidenceGroups.entries()) {
    const members = limited.filter(p => api.limitedEvidenceReason(p.focus) === group.key);
    const segment = segments[index];
    total += members.length;
    assert(segment.dataset.focusReason === group.key, 'Segment order: ' + group.key);
    assert(Math.abs(segment.getBoundingClientRect().width - fill.getBoundingClientRect().width * members.length / limited.length) < 1, 'Segment width follows its data count: ' + group.key);
    if (index) assert(win.getComputedStyle(segment).borderLeftWidth === '1px', 'Thin divider between segments');
    segment.dispatchEvent(new win.Event('pointerenter'));
    const tooltip = doc.querySelector('#focus-segment-tooltip');
    assert(!tooltip.hidden && tooltip.textContent.includes(group.label) && tooltip.textContent.includes(format.format(members.length)), 'Hover explains the group and count: ' + group.key);
    assert(!segment.hasAttribute('title'), 'Only one hover tooltip');
    const bounds = tooltip.getBoundingClientRect();
    assert(bounds.left >= 0 && bounds.right <= doc.documentElement.clientWidth + 1, 'Tooltip fits the viewport');
    segment.focus({ preventScroll: true });
    assert(segment.getAttribute('aria-describedby') === tooltip.id, 'Keyboard focus exposes the same explanation');
    segment.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert(tooltip.hidden, 'Escape dismisses the explanation');
    segment.click();
    const dialog = doc.querySelector('#overlap-dialog');
    assert(dialog.open && doc.querySelector('#overlap-selection-title').textContent === group.label, 'Segment opens its researcher group');
    assert(doc.querySelector('#overlap-result-count').textContent.includes(`of ${format.format(members.length)} people`), 'Researcher count matches the segment');
    assert(doc.querySelector('#overlap-selection-note').textContent.includes(group.description), 'Touch users see the explanation in the opened list');
    const oldCreate = win.URL.createObjectURL, oldClick = win.HTMLAnchorElement.prototype.click;
    let exported;
    try {
      win.URL.createObjectURL = function (blob) { exported = blob; return oldCreate.call(this, blob); };
      win.HTMLAnchorElement.prototype.click = function () { if (!this.download) oldClick.call(this); };
      doc.querySelector('#overlap-download').click();
      const csv = await exported.text();
      assert(csv.trim().split('\r\n').length === members.length + 1 && members.every(p => csv.includes(`"${p.dblpPid}"`)), 'CSV contains exactly the selected group');
    } finally {
      win.URL.createObjectURL = oldCreate;
      win.HTMLAnchorElement.prototype.click = oldClick;
    }
    dialog.close();
  }
  assert(total === limited.length, 'Each limited-evidence profile belongs to exactly one segment');
  row.querySelector('.focus-row-action').click();
  assert(doc.querySelector('#overlap-result-count').textContent.includes(`of ${format.format(limited.length)} people`), 'Full limited-evidence list remains accessible');
  doc.querySelector('#overlap-dialog').close();
  doc.querySelector('[data-focus-group="0"]').click();
  assert(doc.querySelector('#overlap-result-count').textContent.includes(`of ${format.format(policy.includedCount)} people`), 'DB-focused list remains unchanged');
  doc.querySelector('#overlap-dialog').close();
  assert(row.getBoundingClientRect().right <= doc.documentElement.clientWidth + 1
    && doc.querySelector('#population-focus').scrollWidth <= doc.querySelector('#population-focus').clientWidth + 1, 'Population rows fit without widening the section');
  return checks;
}
