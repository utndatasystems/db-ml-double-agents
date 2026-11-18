(() => {
  'use strict';
  const root = document.querySelector('#db-overlap');
  if (!root) return;
  const data = window.CROSSOVER_DATA, api = window.DBVenueOverlap;
  const $ = selector => document.querySelector(selector);
  const fmt = new Intl.NumberFormat('en-US');
  const pct = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const venueLabel = venue => venue === 'SIGMOD' ? 'SIGMOD/PODS' : venue;
  const nameFor = person => String(person.name || '').replace(/\s+0\d{3}$/, '');
  const affiliationFor = person => person.primaryAffiliation || person.displayAffiliations?.[0] || person.affiliations?.[0] || '';
  const searchKey = value => String(value).normalize('NFKD').replace(/\p{M}/gu, '').toLocaleLowerCase();
  const analysis = (root, prefix) => ({ root, prefix,
    state: { year: 'all', roles: 'service', sameYear: false, view: 'share' }, model: null });
  const matrixAnalysis = analysis(root, 'overlap');
  const distributionAnalysis = analysis($('#db-membership'), 'membership');
  let activeAnalysis = matrixAnalysis;
  const state = { selection: null, search: '', page: 0 };
  const dialog = $('#overlap-dialog');
  let filteredPeople = [];
  if (!api || !Array.isArray(data?.people)) {
    $('#overlap-error').hidden = false;
    $('#overlap-error').textContent = 'Committee data is currently unavailable.';
    $('#membership-error').hidden = false;
    $('#membership-error').textContent = 'Committee data is currently unavailable.';
    return;
  }
  const years = [...new Set(data.people.flatMap(p => (p.db?.events || []).map(e => Number(e.year))))]
    .filter(year => Number.isInteger(year) && year >= 1900).sort((a, b) => a - b);
  const period = (options = activeAnalysis.state) => options.year === 'all' ? years.length ? `${years[0]}\u2013${years.at(-1)}` : 'No recorded years' : options.year;
  const yearOptions = `<option value="all">All years (${period()})</option>` + years.map(year => `<option value="${year}">${year}</option>`).join('');
  const valueMarkup = (count, total, view) => view === 'share'
    ? `<strong>${total ? pct.format(count / total) : '\u2014'}</strong><small>${fmt.format(count)} people</small>`
    : `<strong>${fmt.format(count)}<span class="overlap-denominator">/${fmt.format(total)}</span></strong>`;
  function renderDistribution() {
    const { state, model } = distributionAnalysis;
    $('#overlap-distribution-title').textContent = state.sameYear
      ? 'How many DB venues does each person serve at in the same year?' : 'How many DB venues does each person serve at?';
    const maximum = state.view === 'share' ? Math.max(1, model.total) : Math.max(1, ...model.distribution.map(bin => bin.count));
    $('#overlap-distribution').innerHTML = (state.view === 'share' ? '' : '<p class="overlap-axis">Number of researchers</p>') + model.distribution.map(bin => {
      const label = `${bin.venues} ${bin.venues === 1 ? 'venue' : 'venues'}`;
      return `<button type="button" class="overlap-bar-row" data-overlap-bin="${bin.venues}" aria-label="${escape(`${label}: ${bin.count} of ${model.total} people${state.sameYear ? ', maximum in the same year' : ''}. Inspect people.`)}"><span class="overlap-bar-label">${label}</span><span class="overlap-bar-track" aria-hidden="true"><i style="width:${100 * bin.count / maximum}%"></i></span><span class="overlap-bar-value">${valueMarkup(bin.count, model.total, state.view)}</span></button>`;
    }).join('');
    $('#membership-note').hidden = !state.sameYear;
    $('#membership-note').textContent = state.sameYear
      ? 'Each senior DB researcher is grouped by the most venues they served at in a single year.'
      : '';
  }
  function renderMatrix() {
    const { state, model } = matrixAnalysis;
    const share = state.view === 'share';
    const maximum = Math.max(0, ...api.venueOrder.map(venue => model.rosters.get(venue).size));
    const labelMarkup = venue => venue === 'SIGMOD' ? '<span class="overlap-venue-token">SIGMOD</span><wbr>/PODS' : venue;
    $('#overlap-matrix-question').textContent = state.roles === 'service'
      ? 'Where else do DB conference organizers and reviewers serve?' : 'Where else do DB conference PC members serve?';
    $('#overlap-matrix-reading').hidden = share;
    $('#overlap-matrix-reading').textContent = share
      ? '' : 'Each cell counts people serving at both venues. Select a cell to see the people.';
    $('#overlap-matrix').innerHTML = `<table class="overlap-matrix-table" aria-labelledby="overlap-matrix-title" aria-describedby="overlap-matrix-reading"><thead><tr><th class="overlap-matrix-corner" scope="col"><span>From \u2193</span><span>To \u2192</span></th>${api.venueOrder.map(venue => `<th scope="col">${labelMarkup(venue)}</th>`).join('')}</tr></thead><tbody>${api.venueOrder.map(row => `<tr><th scope="row"><span>${labelMarkup(row)}</span><small>${fmt.format(model.rosters.get(row).size)} people</small></th>${api.venueOrder.map(column => {
      const cell = model.cells.get(row).get(column), diagonal = row === column;
      const intensity = share ? cell.share || 0 : maximum ? cell.count / maximum : 0;
      const value = share ? cell.share === null ? '\u2014' : pct.format(cell.share) : fmt.format(cell.count);
      const detail = diagonal
        ? `${venueLabel(row)}: ${fmt.format(cell.total)} people, full row group. Inspect people.`
        : `${fmt.format(cell.count)} of ${fmt.format(cell.total)} ${venueLabel(row)} members also served at ${venueLabel(column)}${state.sameYear ? ' in a shared year' : ''}${cell.share === null ? '' : ` (${pct.format(cell.share)})`}. Inspect shared people.`;
      return `<td><button type="button" class="overlap-matrix-cell${diagonal ? ' is-diagonal' : ''}" data-overlap-row="${row}" data-overlap-column="${column}" style="--overlap-intensity:${100 * intensity}%" aria-label="${escape(detail)}">${value}</button></td>`;
    }).join('')}</tr>`).join('')}</tbody></table>`;
    $('#overlap-color-maximum').textContent = share ? '100%' : `${fmt.format(maximum)} people`;
    $('.overlap-color-key > span:first-child').textContent = share ? '0%' : '0';
  }
  function selectedItems() {
    if (!state.selection) return [];
    const model = activeAnalysis.model;
    if (state.selection.kind === 'focus') return (data.populationFocus?.candidates || [])
      .filter(person => person.focus.label === state.selection.label
        && (!state.selection.reason || api.limitedEvidenceReason(person.focus) === state.selection.reason))
      .map(person => ({pid: person.dblpPid, person, events: person.db.events,
        venues: new Map([...new Set(person.db.events.map(e => e.venue))].map(v => [v, new Set(person.db.events.filter(e => e.venue === v).map(e => e.year))]))}));
    const items = state.selection.kind === 'pair'
      ? api.sharedPeople(model, state.selection.row, state.selection.column)
      : [...model.members.values()].filter(item => item.venueCount === state.selection.count);
    return items.sort((a, b) => nameFor(a.person).localeCompare(nameFor(b.person)) || a.pid.localeCompare(b.pid));
  }
  function renderPeople() {
    const items = selectedItems(), selection = state.selection;
    const { model, state: options } = activeAnalysis;
    const query = searchKey(state.search.trim());
    filteredPeople = items.filter(item => searchKey(`${nameFor(item.person)} ${affiliationFor(item.person)}`).includes(query));
    if (selection.kind === 'focus') {
      const reason = api.limitedEvidenceGroups.find(group => group.key === selection.reason);
      $('#overlap-selection-title').textContent = reason ? reason.label : `${selection.label} committee members`;
      $('#overlap-selection-note').textContent = reason
        ? `${fmt.format(items.length)} people with limited evidence. ${reason.description}`
        : `${fmt.format(items.length)} people. Counts use all recorded publication years and the same conference-paper policy for every person.`;
    } else if (selection.kind === 'pair') {
      const cell = model.cells.get(selection.row).get(selection.column);
      const reverse = model.cells.get(selection.column).get(selection.row);
      const diagonal = selection.row === selection.column;
      $('#overlap-selection-title').textContent = diagonal ? `${venueLabel(selection.row)} members` : `${venueLabel(selection.row)} \u00d7 ${venueLabel(selection.column)}`;
      $('#overlap-selection-note').textContent = diagonal
        ? `${fmt.format(cell.total)} people \u00b7 ${period()} \u00b7 Full venue membership for the selected scope.`
        : `${fmt.format(cell.count)} shared people \u00b7 ${cell.share === null ? '\u2014' : pct.format(cell.share)} of ${venueLabel(selection.row)} (${fmt.format(cell.count)}/${fmt.format(cell.total)}) \u00b7 ${reverse.share === null ? '\u2014' : pct.format(reverse.share)} of ${venueLabel(selection.column)} (${fmt.format(reverse.count)}/${fmt.format(reverse.total)}). ${period()}${options.sameYear ? '; shared service year required.' : '.'}`;
    } else {
      $('#overlap-selection-title').textContent = `${selection.count} DB ${selection.count === 1 ? 'venue' : 'venues'}${options.sameYear ? ' in the same year' : ''}`;
      $('#overlap-selection-note').textContent = `${fmt.format(items.length)} people \u00b7 ${period()}${options.sameYear ? ' \u00b7 Maximum number of venues served in a single year.' : '.'}`;
    }
    const pageSize = 30, pages = Math.max(1, Math.ceil(filteredPeople.length / pageSize));
    state.page = Math.min(state.page, pages - 1);
    const start = state.page * pageSize, page = filteredPeople.slice(start, start + pageSize);
    $('#overlap-result-count').textContent = filteredPeople.length
      ? `${fmt.format(start + 1)}\u2013${fmt.format(start + page.length)} of ${fmt.format(filteredPeople.length)} people${query ? ' matching your search' : ''}` : 'No people match this selection.';
    $('#overlap-download').disabled = !filteredPeople.length;
    $('#overlap-people').innerHTML = page.map(item => {
      const venues = selection.kind === 'pair' ? [...new Set([selection.row, selection.column])] : [...item.venues.keys()];
      const shared = selection.kind === 'pair' ? new Set(api.sharedYears(item, selection.row, selection.column)) : null;
      const events = item.events.filter(event => venues.includes(event.venue) && (!options.sameYear || !shared || shared.has(Number(event.year))))
        .sort((a, b) => b.year - a.year || a.venue.localeCompare(b.venue) || a.role.localeCompare(b.role));
      return `<article class="overlap-person"><div class="overlap-person-name">${item.person.id ? `<button type="button" data-overlap-profile="${escape(item.person.id)}">${escape(nameFor(item.person))}</button>` : `<a href="https://dblp.org/pid/${escape(item.pid)}" target="_blank" rel="noreferrer">${escape(nameFor(item.person))} \u2197</a>`}<p>${escape(affiliationFor(item.person))}</p></div><div class="overlap-person-years">${venues.map(venue => `<span><strong>${venueLabel(venue)}</strong> ${[...item.venues.get(venue)].sort((a, b) => a - b).join(', ')}</span>`).join('')}</div><details class="overlap-person-sources"><summary>Service sources</summary><ul>${events.map(event => `<li>${event.sourceUrl ? `<a href="${escape(event.sourceUrl)}" target="_blank" rel="noreferrer">${escape(`${venueLabel(event.venue)} ${event.year} \u00b7 ${event.role.replaceAll('_', ' ')}`)} \u2197</a>` : escape(`${venueLabel(event.venue)} ${event.year}`)}</li>`).join('')}</ul></details></article>`;
    }).join('') || '<p class="empty-state">No people match this selection.</p>';
    if (selection.kind === 'focus') $('#overlap-people').querySelectorAll('.overlap-person').forEach((article, index) => {
      const focus = page[index].person.focus;
      const note = document.createElement('p'); note.className = 'focus-person-counts';
      const reason = api.limitedEvidenceGroups.find(group => group.key === api.limitedEvidenceReason(focus));
      note.textContent = `${fmt.format(focus.dbPapers)} DB / ${fmt.format(focus.mlPapers)} ML conference papers across ${focus.publicationYears} publication years.${focus.basis === 'publication_and_service' || reason ? ` DB papers span ${focus.dbPublicationYears} years; PC service spans ${focus.serviceYears} years.` : ''} ${reason ? reason.description : focus.reason}`;
      article.insertBefore(note, article.querySelector('.overlap-person-sources'));
    });
    $('#overlap-pagination').innerHTML = pages > 1 ? `<button type="button" data-overlap-page="${state.page - 1}" ${state.page ? '' : 'disabled'}>Previous</button><span>Page ${state.page + 1} of ${pages}</span><button type="button" data-overlap-page="${state.page + 1}" ${state.page + 1 < pages ? '' : 'disabled'}>Next</button>` : '';
  }
  function openSelection(selection, analysis = matrixAnalysis) {
    activeAnalysis = analysis;
    state.selection = selection; state.search = ''; state.page = 0;
    $('#overlap-search').value = '';
    renderPeople();
    dialog.showModal();
    dialog.scrollTop = 0;
  }
  function updateModel(analysis) {
    const { state, prefix } = analysis;
    analysis.model = api.buildModel(data.people, state);
    const scope = state.roles === 'research' ? 'Research PC and review-board members; organizer-only roles excluded.'
      : state.roles === 'reviewers' ? 'Research, industry and demo PC members; organizer-only roles excluded.' : 'All recorded organizing and reviewing roles.';
    $(`#${prefix}-scope`).textContent = `${period(state)} \u00b7 ${scope} Shadow PCs excluded.`;
    if (analysis === matrixAnalysis) renderMatrix();
    else renderDistribution();
  }
  [distributionAnalysis, matrixAnalysis].forEach(analysis => {
    const { root, prefix, state } = analysis;
    $(`#${prefix}-year`).innerHTML = yearOptions;
    $(`#${prefix}-year`).addEventListener('change', event => { state.year = event.target.value; updateModel(analysis); });
    $(`#${prefix}-roles`).addEventListener('change', event => { state.roles = event.target.value; updateModel(analysis); });
    $(`#${prefix}-match`).addEventListener('change', event => { state.sameYear = event.target.value === 'same-year'; updateModel(analysis); });
    root.addEventListener('click', event => {
      const view = event.target.closest('[data-overlap-view]');
      if (view) {
        state.view = view.dataset.overlapView;
        root.querySelectorAll('[data-overlap-view]').forEach(button => { const active = button === view; button.classList.toggle('is-active', active); button.setAttribute('aria-pressed', String(active)); });
        if (analysis === matrixAnalysis) renderMatrix();
        else renderDistribution();
      }
      const bin = event.target.closest('[data-overlap-bin]');
      if (bin) openSelection({ kind: 'count', count: Number(bin.dataset.overlapBin) }, analysis);
      const cell = event.target.closest('[data-overlap-row][data-overlap-column]');
      if (cell) openSelection({ kind: 'pair', row: cell.dataset.overlapRow, column: cell.dataset.overlapColumn }, analysis);
    });
    updateModel(analysis);
  });
  $('#overlap-dialog-close').addEventListener('click', () => dialog.close());
  document.addEventListener('dbml:show-focus-list', event => openSelection({kind: 'focus', label: event.detail.label, reason: event.detail.reason || null}));
  $('#overlap-search').addEventListener('input', event => { state.search = event.target.value; state.page = 0; renderPeople(); });
  dialog.addEventListener('click', event => {
    const page = event.target.closest('[data-overlap-page]');
    if (page) { state.page = Number(page.dataset.overlapPage); renderPeople(); dialog.scrollTop = 0; }
    const profile = event.target.closest('[data-overlap-profile]');
    if (profile) { dialog.close(); document.dispatchEvent(new CustomEvent('dbml:open-person', { detail: { id: profile.dataset.overlapProfile } })); }
  });
  $('#overlap-download').addEventListener('click', () => {
    const selection = state.selection;
    const csv = selection.kind === 'focus' ? api.toFocusCsv(filteredPeople) : selection.kind === 'pair' ? api.toCsv(filteredPeople, selection.row, selection.column, activeAnalysis.state) : api.toMembershipCsv(filteredPeople, activeAnalysis.state);
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `db-membership-${selection.kind}-${activeAnalysis.state.year}.csv`; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
})();
