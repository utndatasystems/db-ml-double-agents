(() => {
  'use strict';

  const data = window.CROSSOVER_DATA || null;
  const matrixStats = window.CROSSOVER_MATRIX_STATS || null;
  const $ = (selector) => document.querySelector(selector);
  const fmt = new Intl.NumberFormat('en-US');

  const els = {
    summary: $('#summary-cards'),
    dataStatus: $('#data-status'),
    matrix: $('#matrix-wrap'),
    matrixNote: $('#matrix-note'),
    globalTimeline: $('#global-timeline'),
    timelineLegend: $('#timeline-legend'),
    body: $('#people-body'),
    empty: $('#empty-state'),
    results: $('#results-count'),
    search: $('#search-input'),
    dbVenue: $('#db-venue-filter'),
    mlVenue: $('#ml-venue-filter'),
    year: $('#year-filter'),
    role: $('#role-filter'),
    sort: $('#sort-filter'),
    resetFilters: $('#reset-filters'),
    emptyResetFilters: $('#empty-reset-filters'),
    pageSize: $('#page-size-filter'),
    pagination: $('#pagination'),
    pageSummary: $('#page-summary'),
    pageStatus: $('#page-status'),
    prevPage: $('#prev-page'),
    nextPage: $('#next-page'),
    dialog: $('#person-dialog'),
    dialogContent: $('#dialog-content'),
    dialogClose: $('#dialog-close'),
    timelineDialog: $('#timeline-dialog'),
    timelineDialogContent: $('#timeline-dialog-content'),
    timelineDialogClose: $('#timeline-dialog-close'),
  };

  const roleLabels = {
    program_committee_member: 'Program Committee',
    senior_program_committee_member: 'Senior Program Committee',
    program_committee_chair: 'PC Chair',
    associate_editor: 'Associate Editor',
    area_chair: 'Area Chair',
    review_board_member: 'Review Board',
    reviewer: 'Reviewer',
  };

  const publicationClassLabels = {
    main_ml: 'Main ML',
    database: 'Database',
    other_conference: 'Other conference',
    journal: 'Journal',
    workshop: 'Workshop',
    preprint: 'Preprint',
    other: 'Other',
  };

  const publicationClassOrder = [
    'main_ml', 'database', 'other_conference', 'journal', 'workshop', 'preprint', 'other'
  ];

  const mainVenueOrder = ['NeurIPS', 'ICML', 'ICLR'];
  const contextClassOrder = publicationClassOrder.filter((key) => key !== 'main_ml');

  const state = {
    search: '',
    dbVenue: '',
    mlVenue: '',
    year: '',
    role: '',
    sort: 'mlPapers',
    page: 1,
    pageSize: 'auto',
    timelineVisible: Object.fromEntries(mainVenueOrder.map((key) => [key, true])),
  };

  let dialogMainVenueVisibility = Object.fromEntries(mainVenueOrder.map((key) => [key, true]));
  let dialogContextVisibility = Object.fromEntries(contextClassOrder.map((key) => [key, true]));
  let dialogContextExpanded = false;

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#039;', '"': '&quot;'
    }[char]));
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, '&#096;');
  }

  function normalize(value) {
    return String(value || '').toLocaleLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
  }

  function displayPersonName(value) {
    return String(value ?? '').replace(/\s+0\d{3}$/, '');
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function uniqueSorted(values) {
    return [...new Set(values.filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b)));
  }

  function slug(value) {
    return String(value || '').toLocaleLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  }

  function labelRole(role) {
    return roleLabels[role] || String(role || '').replaceAll('_', ' ').replace(/\b\w/g, (m) => m.toUpperCase());
  }

  function allPeople() {
    return data ? list(data.people) : [];
  }

  function personAffiliations(person) {
    const cleaned = list(person?.displayAffiliations);
    return cleaned.length ? cleaned : list(person?.affiliations);
  }

  function personPrimaryAffiliation(person) {
    if (person?.primaryAffiliation) return String(person.primaryAffiliation);
    const current = list(person?.displayCurrentAffiliations);
    if (current.length) return current[0];
    return personAffiliations(person)[0] || '';
  }

  function eventAffiliations(event) {
    const cleaned = list(event?.displayAffiliations);
    if (cleaned.length) return cleaned;
    if (event?.displayAffiliation) return [event.displayAffiliation];
    if (event?.affiliation) return [event.affiliation];
    return [];
  }

  function selectedYear() {
    const year = Number(state.year);
    return state.year && Number.isFinite(year) ? year : null;
  }

  function paperScope(paper) {
    return String(paper?.scope || 'main').toLocaleLowerCase() === 'main' ? 'main' : 'side';
  }

  function publicationClass(paper) {
    const value = String(paper?.publicationClass || '').toLocaleLowerCase();
    if (value && publicationClassLabels[value]) return value;
    return paperScope(paper) === 'main' ? 'main_ml' : 'other';
  }

  function publicationClassLabel(paperOrClass) {
    const key = typeof paperOrClass === 'string' ? paperOrClass : publicationClass(paperOrClass);
    return publicationClassLabels[key] || 'Other';
  }

  function paperVenueLabel(paper) {
    return String(paper?.exactVenue || paper?.venue || paper?.venueLabel || paper?.booktitle || paper?.journal || 'Other DBLP publication');
  }

  function allPapersFor(person) {
    return list(person?.ml?.papers);
  }

  function mainPapersFor(person, year = null) {
    return allPapersFor(person).filter((paper) => {
      if (paperScope(paper) !== 'main') return false;
      const paperYear = Number(paper.year);
      return !year || (Number.isFinite(paperYear) && paperYear === year);
    });
  }

  function sidePapersFor(person, year = null) {
    return allPapersFor(person).filter((paper) => {
      if (paperScope(paper) !== 'side') return false;
      const paperYear = Number(paper.year);
      return !year || (Number.isFinite(paperYear) && paperYear === year);
    });
  }

  function mainVenueLabel(paper) {
    const venue = String(paper?.venue || '');
    if (venue.toLocaleLowerCase() === 'nips') return 'NeurIPS';
    return venue || 'Main ML';
  }

  function publicationFiltersActive() {
    return Boolean(state.mlVenue || selectedYear());
  }

  function paperMatchesExplorerFilters(paper, { ignoreYear = false } = {}) {
    if (paperScope(paper) !== 'main') return false;
    if (state.mlVenue && mainVenueLabel(paper) !== state.mlVenue) return false;
    const year = selectedYear();
    if (!ignoreYear && year && Number(paper.year) !== year) return false;
    return true;
  }

  function hasMain(person) {
    return mainPapersFor(person).length > 0;
  }

  function miniTimelineWindow() {
    const years = allPeople().flatMap((person) => mainPapersFor(person).map((paper) => Number(paper.year))).filter(Number.isFinite);
    const end = years.length ? Math.max(...years) : new Date().getFullYear();
    return { start: end - 25, end };
  }

  function resolvedPageSize() {
    if (state.pageSize !== 'auto') return Math.max(1, Number(state.pageSize) || 25);
    const available = Math.max(360, window.innerHeight - 390);
    return Math.max(5, Math.min(14, Math.floor(available / 66)));
  }

  function matches(person, { ignoreYear = false } = {}) {
    const q = normalize(state.search.trim());
    const nameHaystack = normalize([
      person.name,
      displayPersonName(person.name),
      ...list(person.aliases?.db),
      ...list(person.aliases?.publication),
      ...list(person.aliases?.ml),
    ].join(' '));
    const directNameMatch = Boolean(q && nameHaystack.includes(q));

    // Keep the default explorer focused on DB × main-ML crossovers, but let
    // an explicit researcher-name search surface any resolved DB researcher.
    if (!hasMain(person) && !directNameMatch) return false;

    const dbEvents = list(person?.db?.events).filter((event) => {
      if (state.dbVenue && event.venue !== state.dbVenue) return false;
      if (state.role && event.role !== state.role) return false;
      return true;
    });
    if ((state.dbVenue || state.role) && dbEvents.length === 0) return false;

    if (state.mlVenue || (!ignoreYear && selectedYear())) {
      const paperOk = mainPapersFor(person).some((paper) => paperMatchesExplorerFilters(paper, { ignoreYear }));
      if (!paperOk) return false;
    }

    if (!q) return true;
    const mainPapers = mainPapersFor(person);
    const haystack = normalize([
      person.name,
      displayPersonName(person.name),
      ...list(person.aliases?.db),
      ...list(person.aliases?.publication),
      ...list(person.aliases?.ml),
      ...list(person.affiliations),
      ...personAffiliations(person),
      ...list(person.db?.venues),
      ...mainPapers.map(mainVenueLabel),
      ...mainPapers.map((paper) => paper.title),
      ...list(person.db?.roles).map(labelRole),
    ].join(' '));
    return haystack.includes(q);
  }

  function filteredPeople() {
    return allPeople().filter(matches);
  }

  function displayContext(person) {
    const dbEvents = list(person?.db?.events);
    const papers = mainPapersFor(person).filter((paper) => paperMatchesExplorerFilters(paper));
    return {
      dbEvents,
      papers,
      dbVenues: uniqueSorted(dbEvents.map((event) => event.venue)),
      dbRoles: uniqueSorted(dbEvents.map((event) => event.role)),
      mlVenues: uniqueSorted(papers.map(mainVenueLabel)),
    };
  }

  function sortPeople(people) {
    const copy = [...people];
    const nameSort = (a, b) => displayPersonName(a.name).localeCompare(displayPersonName(b.name));
    const matchingMain = (person) => mainPapersFor(person).filter((paper) => paperMatchesExplorerFilters(paper));

    if (state.sort === 'name') return copy.sort(nameSort);
    if (state.sort === 'dbService') {
      return copy.sort((a, b) => list(b.db?.events).length - list(a.db?.events).length || nameSort(a, b));
    }
    if (state.sort === 'recent') {
      const latest = (person) => {
        const years = matchingMain(person).map((paper) => Number(paper.year)).filter(Number.isFinite);
        return years.length ? Math.max(...years) : 0;
      };
      return copy.sort((a, b) => latest(b) - latest(a) || matchingMain(b).length - matchingMain(a).length || nameSort(a, b));
    }
    return copy.sort((a, b) => matchingMain(b).length - matchingMain(a).length || nameSort(a, b));
  }

  function chip(text, kind = 'neutral', active = false) {
    const classes = ['chip', `chip-${kind}`, `chip-${slug(text)}`];
    if (active) classes.push('chip-accent');
    return `<span class="${classes.join(' ')}">${escapeHtml(text)}</span>`;
  }

  function mlChipClasses(venue) {
    const classes = ['chip', 'chip-ml', `chip-${slug(venue)}`];
    if (state.mlVenue) {
      classes.push(venue === state.mlVenue ? 'chip-accent' : 'chip-ml-muted');
    }
    return classes.join(' ');
  }

  function paperKey(paper, person = null) {
    return paper?.dblpKey || `${person?.dblpPid || ''}|${paperScope(paper)}|${paper?.year || ''}|${paper?.title || ''}`;
  }

  function scopePeople() {
    return allPeople().filter(hasMain);
  }

  function renderSummary() {
    const people = scopePeople();
    const mainKeys = new Set();
    const venuePeople = Object.fromEntries(mainVenueOrder.map((venue) => [venue, new Set()]));
    people.forEach((person) => {
      mainPapersFor(person).forEach((paper) => {
        mainKeys.add(paperKey(paper, person));
        const venue = mainVenueLabel(paper);
        if (venuePeople[venue]) venuePeople[venue].add(person.id);
      });
    });
    const cards = [
      { value: people.length, lines: ['DB × ML', 'researchers'] },
      { value: mainKeys.size, lines: ['main ML papers by DB', 'researchers'] },
      ...mainVenueOrder.map((venue) => ({
        value: venuePeople[venue].size,
        lines: [`DB researchers with ${venue}`, 'papers'],
      })),
    ];
    els.summary.innerHTML = cards.map(({ value, lines }) => `
      <div class="summary-card">
        <span class="summary-number">${fmt.format(value || 0)}</span>
        <span class="summary-label">${lines.map((line) => escapeHtml(line)).join('<br>')}</span>
      </div>
    `).join('');
    els.dataStatus.textContent = '';
  }

  function populateSelect(select, values, labeler = (x) => x) {
    if (!select) return;
    const current = select.value;
    const first = select.options[0];
    select.innerHTML = '';
    select.append(first);
    values.forEach((value) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = labeler(value);
      select.append(option);
    });
    select.value = current;
  }

  function renderFilters() {
    const people = scopePeople();
    populateSelect(els.dbVenue, uniqueSorted(people.flatMap((person) => list(person.db?.venues))));
    populateSelect(els.mlVenue, mainVenueOrder.filter((venue) => people.some((person) => mainPapersFor(person).some((paper) => mainVenueLabel(paper) === venue))));
    populateSelect(els.role, uniqueSorted(people.flatMap((person) => list(person.db?.roles))), labelRole);

    const candidatePeople = people.filter((person) => matches(person, { ignoreYear: true }));
    const years = uniqueSorted(candidatePeople.flatMap((person) => mainPapersFor(person)
      .filter((paper) => paperMatchesExplorerFilters(paper, { ignoreYear: true }))
      .map((paper) => Number(paper.year))
      .filter(Number.isFinite))).sort((a, b) => b - a);
    populateSelect(els.year, years, String);
    if (state.year && !years.includes(Number(state.year))) {
      state.year = '';
      els.year.value = '';
    }
  }

  function confirmedMatrix() {
    const people = allPeople();
    const dbVenues = matrixStats?.dbVenues || uniqueSorted(people.flatMap((person) => list(person.db?.venues)));
    const mlVenues = matrixStats?.mlVenues || uniqueSorted(people.flatMap((person) => mainPapersFor(person).map((paper) => paper.venue)));
    return { people, dbVenues, mlVenues };
  }

  function personInMatrixCell(person, dbVenue, mlVenue) {
    const dbOk = list(person.db?.events).some((event) => event.venue === dbVenue);
    if (!dbOk) return false;
    return mainPapersFor(person).some((paper) => paper.venue === mlVenue);
  }

  function renderMatrix() {
    const { people, dbVenues, mlVenues } = confirmedMatrix();
    if (!people.length || !mlVenues.length) {
      els.matrix.innerHTML = '<p class="empty-state">No crossover data yet.</p>';
      return;
    }

    if (els.matrixNote) els.matrixNote.textContent = 'All years · main ML only.';

    const head = `<tr><th scope="col">DB venue</th>${mlVenues.map((venue) => `<th scope="col"><span class="matrix-axis-label">${escapeHtml(venue)}</span></th>`).join('')}</tr>`;
    const rows = dbVenues.map((dbVenue) => {
      const denominator = people.filter((person) => list(person.db?.events).some((event) => event.venue === dbVenue)).length;
      const cells = mlVenues.map((mlVenue) => {
        const n = people.filter((person) => personInMatrixCell(person, dbVenue, mlVenue)).length;
        const pct = denominator ? 100 * n / denominator : 0;
        return `<td><div class="matrix-cell-static" title="${escapeAttr(`${n} of ${denominator} resolved ${dbVenue} researchers`)}"><span class="matrix-pct">${escapeHtml(pct.toFixed(1))}%</span><small>${fmt.format(n)} / ${fmt.format(denominator)} DB researchers</small></div></td>`;
      }).join('');
      return `<tr><th scope="row"><span class="matrix-axis-label">${escapeHtml(dbVenue)}</span></th>${cells}</tr>`;
    }).join('');

    els.matrix.innerHTML = `<table class="matrix"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
  }

  function venueClass(venue) {
    const normalized = String(venue || '').toLocaleLowerCase();
    if (normalized === 'neurips' || normalized === 'nips') return 'venue-neurips';
    if (normalized === 'icml') return 'venue-icml';
    if (normalized === 'iclr') return 'venue-iclr';
    return 'venue-other';
  }

  function publicationClassCss(className) {
    return `class-${String(className || 'other').replaceAll('_', '-')}`;
  }

  function nativeSegmentClass(paper) {
    const className = publicationClass(paper);
    if (className === 'main_ml') return `timeline-segment ${venueClass(paper?.venue)}`;
    return `timeline-segment ${publicationClassCss(className)}`;
  }

  function segmentClass(paper, highlightExplorer = true) {
    if (!highlightExplorer) return nativeSegmentClass(paper);
    if (state.mlVenue) {
      return `timeline-segment ${mainVenueLabel(paper) === state.mlVenue ? 'is-highlight' : 'is-muted'}`;
    }
    if (selectedYear() && Number(paper?.year) !== selectedYear()) return 'timeline-segment is-muted';
    return nativeSegmentClass(paper);
  }

  function aggregatePapers(people) {
    const map = new Map();
    people.forEach((person) => {
      allPapersFor(person).forEach((paper) => {
        const key = paperKey(paper, person);
        if (!map.has(key)) map.set(key, { paper, people: new Set() });
        map.get(key).people.add(displayPersonName(person.name));
      });
    });
    return [...map.values()].map((item) => ({ ...item, people: [...item.people].sort() }));
  }

  function timelineGroups(entries) {
    const byYear = new Map();
    entries.forEach((entry) => {
      const year = Number(entry.paper.year);
      if (!Number.isFinite(year)) return;
      if (!byYear.has(year)) byYear.set(year, []);
      byYear.get(year).push(entry);
    });
    return byYear;
  }

  function renderTimelineLegend() {
    if (!els.timelineLegend) return;
    els.timelineLegend.innerHTML = mainVenueOrder.map((venue) => {
      const on = state.timelineVisible[venue] !== false;
      const filterClass = state.mlVenue
        ? (state.mlVenue === venue ? ' is-venue-highlight' : ' is-venue-muted')
        : '';
      const swatchClass = state.mlVenue
        ? (state.mlVenue === venue ? 'is-highlight' : 'is-muted')
        : venueClass(venue);
      return `<button class="legend-item legend-toggle${on ? '' : ' is-off'}${filterClass}" type="button" data-timeline-venue="${escapeAttr(venue)}" aria-pressed="${on}"><i class="legend-swatch ${swatchClass}"></i>${escapeHtml(venue)}</button>`;
    }).join('');
  }

  function timelineColumnTooltip(year, yearEntries, mode = 'main') {
    const total = yearEntries.length;
    const lines = [`${year} · ${total} ${total === 1 ? 'paper' : 'papers'}`];
    if (mode === 'main') {
      mainVenueOrder.forEach((venue) => {
        const count = yearEntries.filter((entry) => mainVenueLabel(entry.paper) === venue).length;
        if (count) lines.push(`${venue}: ${count}`);
      });
    } else {
      contextClassOrder.forEach((className) => {
        const count = yearEntries.filter((entry) => publicationClass(entry.paper) === className).length;
        if (count) lines.push(`${publicationClassLabels[className]}: ${count}`);
      });
    }
    return lines.join('\n');
  }

  function globalTimelineMarkup(entries, options = {}) {
    const mode = options.mode || 'main';
    const visibility = options.visibility || (mode === 'main' ? state.timelineVisible : dialogContextVisibility);
    const highlightExplorer = options.highlightExplorer !== false;
    const filteredEntries = entries.filter((entry) => {
      if (mode === 'main') return paperScope(entry.paper) === 'main' && visibility[mainVenueLabel(entry.paper)] !== false;
      return paperScope(entry.paper) === 'side' && visibility[publicationClass(entry.paper)] !== false;
    });
    const groups = timelineGroups(filteredEntries);
    const dataYears = [...groups.keys()].sort((a, b) => a - b);
    const minYear = Number.isFinite(Number(options.minYear)) ? Number(options.minYear) : (dataYears.length ? Math.min(...dataYears) : null);
    const maxYear = Number.isFinite(Number(options.maxYear)) ? Number(options.maxYear) : (dataYears.length ? Math.max(...dataYears) : null);
    if (minYear === null || maxYear === null) return `<p class="empty-state">No ${mode === 'main' ? 'main ML' : 'other'} publication activity.</p>`;
    const years = Array.from({ length: maxYear - minYear + 1 }, (_, index) => minYear + index);
    const maxTotal = Math.max(...years.map((year) => (groups.get(year) || []).length), 1);

    return `<div class="timeline-scroll"><div class="timeline-chart" style="--timeline-years:${years.length}">${years.map((year) => {
      const yearEntries = groups.get(year) || [];
      const total = yearEntries.length;
      const barHeight = total ? Math.max(10, Math.round(16 + 146 * Math.sqrt(total / maxTotal))) : 0;
      const segments = [];

      if (mode === 'main') {
        mainVenueOrder.forEach((venue) => {
          const matching = yearEntries.filter((entry) => mainVenueLabel(entry.paper) === venue);
          if (!matching.length) return;
          const pct = total ? 100 * matching.length / total : 0;
          segments.push(`<span class="${segmentClass(matching[0].paper, highlightExplorer)}" style="height:${pct}%"></span>`);
        });
      } else {
        contextClassOrder.forEach((className) => {
          const matching = yearEntries.filter((entry) => publicationClass(entry.paper) === className);
          if (!matching.length) return;
          const pct = total ? 100 * matching.length / total : 0;
          segments.push(`<span class="${nativeSegmentClass(matching[0].paper)}" style="height:${pct}%"></span>`);
        });
      }

      const major = year % 5 === 0;
      const tooltip = timelineColumnTooltip(year, yearEntries, mode);
      return `
        <button class="timeline-year" type="button" data-timeline-year="${year}" data-timeline-mode="${mode}" ${total ? '' : 'disabled'} aria-label="${escapeAttr(`${year}: ${total} papers`)}" title="${escapeAttr(tooltip)}">
          <span class="timeline-count-label">${total ? fmt.format(total) : ''}</span>
          <span class="timeline-bar-area"><span class="timeline-stack" style="height:${barHeight}px">${segments.join('')}</span></span>
          <span class="timeline-year-label${major ? ' is-major' : ''}">${year}</span>
        </button>`;
    }).join('')}</div></div>`;
  }

  function renderGlobalTimeline() {
    if (!els.globalTimeline) return;
    renderTimelineLegend();
    const people = filteredPeople();
    const entries = aggregatePapers(people).filter((entry) => paperScope(entry.paper) === 'main');
    els.globalTimeline.innerHTML = globalTimelineMarkup(entries, { mode: 'main', visibility: state.timelineVisible });
    els.globalTimeline.querySelectorAll('[data-timeline-year]').forEach((button) => {
      button.addEventListener('click', () => {
        const year = Number(button.dataset.timelineYear);
        if (Number.isFinite(year)) openTimelineYear(year, people, { mode: 'main', visibility: state.timelineVisible });
      });
    });
    els.timelineLegend.querySelectorAll('[data-timeline-venue]').forEach((button) => {
      button.addEventListener('click', () => {
        const venue = button.dataset.timelineVenue;
        state.timelineVisible[venue] = state.timelineVisible[venue] === false;
        renderGlobalTimeline();
      });
    });
  }

  function miniTimelineSvg(person) {
    const allPapers = mainPapersFor(person).filter((paper) => Number.isFinite(Number(paper.year)));
    if (!allPapers.length) return '<span class="mini-timeline-empty">No main ML papers</span>';
    const careerYears = allPapers.map((paper) => Number(paper.year));
    const careerMin = Math.min(...careerYears);
    const careerMax = Math.max(...careerYears);
    const window = miniTimelineWindow();
    const range = Array.from({ length: window.end - window.start + 1 }, (_, index) => window.start + index);
    const byYear = new Map(range.map((year) => [year, []]));
    allPapers.forEach((paper) => {
      const year = Number(paper.year);
      if (byYear.has(year)) byYear.get(year).push(paper);
    });
    const maxTotal = Math.max(...range.map((year) => byYear.get(year).length), 1);
    const unit = 4;
    const width = range.length * unit;
    const height = 34;
    const baseline = 28;

    const bars = range.map((year, index) => {
      const yearPapers = byYear.get(year);
      if (!yearPapers.length) return '';
      const total = yearPapers.length;
      const totalHeight = Math.max(4, 24 * Math.sqrt(total / maxTotal));
      const buckets = new Map();
      yearPapers.forEach((paper) => {
        let key = mainVenueLabel(paper);
        if (state.mlVenue) key = mainVenueLabel(paper) === state.mlVenue ? 'highlight' : 'muted';
        else if (selectedYear() && Number(paper.year) !== selectedYear()) key = 'muted';
        buckets.set(key, (buckets.get(key) || 0) + 1);
      });
      let cursor = baseline;
      const rects = [];
      const order = [...mainVenueOrder, 'muted', 'highlight'];
      order.forEach((key) => {
        const count = buckets.get(key) || 0;
        if (!count) return;
        const segmentHeight = totalHeight * count / total;
        cursor -= segmentHeight;
        const klass = key === 'highlight' ? 'mini-highlight' : key === 'muted' ? 'mini-muted' : `mini-${slug(key)}`;
        rects.push(`<rect class="${klass}" x="${index * unit + 0.5}" y="${cursor.toFixed(2)}" width="3" height="${Math.max(0.8, segmentHeight).toFixed(2)}"></rect>`);
      });
      const tooltipEntries = yearPapers.map((paper) => ({ paper }));
      return `<g><title>${escapeHtml(timelineColumnTooltip(year, tooltipEntries, 'main'))}</title>${rects.join('')}</g>`;
    }).join('');

    return `
      <svg class="mini-timeline-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Main ML papers from ${careerMin} to ${careerMax}">
        <line class="mini-axis" x1="0" y1="28.5" x2="${width}" y2="28.5"></line>
        ${bars}
      </svg>
      <span class="mini-timeline-range">${careerMin}–${careerMax}</span>
    `;
  }

  function renderPeople() {
    const people = sortPeople(filteredPeople());
    const total = people.length;
    const pageSize = resolvedPageSize();
    if (els.pageSize && els.pageSize.value !== String(state.pageSize)) els.pageSize.value = String(state.pageSize);
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    state.page = Math.min(Math.max(1, Number(state.page) || 1), totalPages);

    const startIndex = total ? (state.page - 1) * pageSize : 0;
    const endIndex = Math.min(startIndex + pageSize, total);
    const visiblePeople = people.slice(startIndex, endIndex);

    els.results.textContent = `${fmt.format(total)} ${total === 1 ? 'researcher' : 'researchers'}`;
    els.empty.hidden = total > 0;
    els.pagination.hidden = total === 0;
    els.pageSummary.textContent = total ? `Showing ${fmt.format(startIndex + 1)}–${fmt.format(endIndex)} of ${fmt.format(total)}` : '';
    els.pageStatus.textContent = total ? `Page ${fmt.format(state.page)} of ${fmt.format(totalPages)}` : '';
    els.prevPage.disabled = state.page <= 1;
    els.nextPage.disabled = state.page >= totalPages;

    els.body.innerHTML = visiblePeople.map((person) => {
      const context = displayContext(person);
      const allMain = mainPapersFor(person);
      const venueCounts = Object.fromEntries(mainVenueOrder.map((venue) => [venue, allMain.filter((paper) => mainVenueLabel(paper) === venue).length]));
      const matchingCount = context.papers.length;
      const metricText = publicationFiltersActive()
        ? `${matchingCount} matching main ML ${matchingCount === 1 ? 'paper' : 'papers'}`
        : `${allMain.length} main ML ${allMain.length === 1 ? 'paper' : 'papers'}`;
      const mlChips = mainVenueOrder.filter((venue) => venueCounts[venue] > 0)
        .map((venue) => `<span class="${mlChipClasses(venue)}">${escapeHtml(venue)} ${fmt.format(venueCounts[venue])}</span>`)
        .join('');
      return `
        <tr tabindex="0" data-person-id="${escapeAttr(person.id)}" aria-label="Open ${escapeAttr(displayPersonName(person.name))} details">
          <td>
            <span class="person-name">${escapeHtml(displayPersonName(person.name))}</span>
            <span class="person-sub">${escapeHtml(personPrimaryAffiliation(person) || person.dblpPid || '')}</span>
          </td>
          <td>
            <div class="chips">${context.dbVenues.map((venue) => chip(venue, 'neutral')).join('')}</div>
            <div class="metric-inline">${fmt.format(context.dbEvents.length)} service records</div>
          </td>
          <td>
            <div class="chips">${mlChips}</div>
            <div class="metric-inline">${escapeHtml(metricText)}</div>
          </td>
          <td class="timeline-cell">
            <button class="mini-timeline-button" type="button" data-mini-person="${escapeAttr(person.id)}" aria-label="Open ${escapeAttr(displayPersonName(person.name))} ML publication timeline">
              ${miniTimelineSvg(person)}
            </button>
          </td>
        </tr>
      `;
    }).join('');

    els.body.querySelectorAll('tr[data-person-id]').forEach((row) => {
      const open = () => openPerson(row.dataset.personId);
      row.addEventListener('click', open);
      row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
      });
    });
    els.body.querySelectorAll('[data-mini-person]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        openPerson(button.dataset.miniPerson);
      });
    });
  }

  function eventList(events) {
    if (!events?.length) return '<li>No detailed service events available.</li>';
    return events.map((event) => `
      <li>
        <div class="event-top"><strong>${escapeHtml(event.venue)} · ${escapeHtml(labelRole(event.role))}</strong><span>${escapeHtml(event.year)}</span></div>
        <div class="event-sub">${escapeHtml(eventAffiliations(event).join(' · '))}${event.rounds?.length ? ` · ${escapeHtml(event.rounds.join(', '))}` : ''}</div>
      </li>
    `).join('');
  }

  function paperList(papers) {
    if (!papers?.length) return '<li>No detailed paper records available.</li>';
    return papers.map((paper) => {
      const title = escapeHtml(paper.title);
      const linked = paper.url ? `<a class="paper-title" href="${escapeAttr(paper.url)}" target="_blank" rel="noreferrer">${title}</a>` : `<span class="paper-title">${title}</span>`;
      const position = Number.isFinite(Number(paper.authorPosition)) ? ` · author #${Number(paper.authorPosition) + 1}` : '';
      return `<li>${linked}<div class="event-sub">${escapeHtml(paperVenueLabel(paper))} · ${escapeHtml(paper.year)} · ${escapeHtml(publicationClassLabel(paper))}${position}</div></li>`;
    }).join('');
  }

  function personTimelineMarkup(person, mode) {
    const papers = allPapersFor(person);
    const years = papers.map((paper) => Number(paper.year)).filter(Number.isFinite);
    const minYear = years.length ? Math.min(...years) : null;
    const maxYear = years.length ? Math.max(...years) : null;
    const entries = papers.map((paper) => ({ paper, people: [displayPersonName(person.name)] }));
    return globalTimelineMarkup(entries, {
      mode,
      visibility: mode === 'main' ? dialogMainVenueVisibility : dialogContextVisibility,
      highlightExplorer: mode === 'main',
      minYear,
      maxYear,
    }).replaceAll('timeline-scroll', 'timeline-scroll person-timeline-scroll');
  }

  function bindPersonTimeline(person) {
    els.dialogContent.querySelectorAll('[data-timeline-year]').forEach((button) => {
      button.addEventListener('click', () => {
        const year = Number(button.dataset.timelineYear);
        const mode = button.dataset.timelineMode || 'main';
        if (Number.isFinite(year)) openTimelineYear(year, [person], {
          mode,
          visibility: mode === 'main' ? dialogMainVenueVisibility : dialogContextVisibility,
        });
      });
    });
    els.dialogContent.querySelectorAll('[data-person-main-venue]').forEach((button) => {
      button.addEventListener('click', () => {
        const venue = button.dataset.personMainVenue;
        dialogMainVenueVisibility[venue] = dialogMainVenueVisibility[venue] === false;
        renderPersonDialog(person);
      });
    });
    els.dialogContent.querySelectorAll('[data-person-context-category]').forEach((button) => {
      button.addEventListener('click', () => {
        const key = button.dataset.personContextCategory;
        if (key === 'all') contextClassOrder.forEach((className) => { dialogContextVisibility[className] = true; });
        else dialogContextVisibility[key] = dialogContextVisibility[key] === false;
        renderPersonDialog(person);
      });
    });
    const contextDetails = els.dialogContent.querySelector('[data-context-timeline-details]');
    if (contextDetails) {
      contextDetails.addEventListener('toggle', () => {
        dialogContextExpanded = contextDetails.open;
      });
    }
  }

  function renderPersonDialog(person) {
    const dblpUrl = person.dblpPid ? `https://dblp.org/pid/${encodeURI(person.dblpPid)}` : '';
    const orcid = list(person.orcids)[0];
    const dbEvents = [...list(person.db?.events)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.venue).localeCompare(String(b.venue)));
    const mainPapers = [...mainPapersFor(person)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.title).localeCompare(String(b.title)));
    const contextPapers = [...sidePapersFor(person)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.title).localeCompare(String(b.title)));

    const mainCounts = Object.fromEntries(mainVenueOrder.map((venue) => [venue, mainPapers.filter((paper) => mainVenueLabel(paper) === venue).length]));
    const mainLegend = mainVenueOrder.map((venue) => {
      const on = dialogMainVenueVisibility[venue] !== false;
      const filterClass = state.mlVenue
        ? (state.mlVenue === venue ? ' is-venue-highlight' : ' is-venue-muted')
        : '';
      const swatchClass = state.mlVenue
        ? (state.mlVenue === venue ? 'is-highlight' : 'is-muted')
        : venueClass(venue);
      return `<button class="legend-item legend-toggle${on ? '' : ' is-off'}${filterClass}" type="button" data-person-main-venue="${escapeAttr(venue)}" aria-pressed="${on}"><i class="legend-swatch ${swatchClass}"></i>${escapeHtml(venue)} <strong>${fmt.format(mainCounts[venue])}</strong></button>`;
    }).join('');

    const contextCounts = Object.fromEntries(contextClassOrder.map((key) => [key, contextPapers.filter((paper) => publicationClass(paper) === key).length]));
    const allContextVisible = contextClassOrder.every((key) => dialogContextVisibility[key] !== false);
    const contextLegend = `<button class="legend-item legend-toggle${allContextVisible ? '' : ' is-off'}" type="button" data-person-context-category="all" aria-pressed="${allContextVisible}">All <strong>${fmt.format(contextPapers.length)}</strong></button>` + contextClassOrder.map((key) => {
      const on = dialogContextVisibility[key] !== false;
      return `<button class="legend-item legend-toggle${on ? '' : ' is-off'}" type="button" data-person-context-category="${key}" aria-pressed="${on}"><i class="legend-swatch ${publicationClassCss(key)}"></i>${escapeHtml(publicationClassLabels[key])} <strong>${fmt.format(contextCounts[key])}</strong></button>`;
    }).join('');

    const visibleContext = contextPapers.filter((paper) => dialogContextVisibility[publicationClass(paper)] !== false);
    const groupedContextLists = contextClassOrder.map((className) => {
      if (dialogContextVisibility[className] === false) return '';
      const classPapers = visibleContext.filter((paper) => publicationClass(paper) === className);
      if (!classPapers.length) return '';
      return `<details class="publication-group"><summary>${escapeHtml(publicationClassLabels[className])}<span>${fmt.format(classPapers.length)}</span></summary><ul class="paper-list">${paperList(classPapers)}</ul></details>`;
    }).join('');

    els.dialogContent.innerHTML = `
      <header class="detail-header">
        <p class="eyebrow">Researcher</p>
        <h2>${escapeHtml(displayPersonName(person.name))}</h2>
        <div class="detail-links">
          ${dblpUrl ? `<a href="${dblpUrl}" target="_blank" rel="noreferrer">DBLP ↗</a>` : ''}
          ${orcid ? `<a href="https://orcid.org/${escapeAttr(orcid)}" target="_blank" rel="noreferrer">ORCID ↗</a>` : ''}
        </div>
      </header>
      <section class="detail-timeline-section main-ml-timeline-section">
        <div class="detail-section-heading"><div><p class="eyebrow">Main ML</p><h3>${fmt.format(mainPapers.length)} papers</h3></div></div>
        <div class="person-timeline-filters" aria-label="Main ML venue filters">${mainLegend}</div>
        ${personTimelineMarkup(person, 'main')}
      </section>
      <details class="detail-timeline-section context-timeline-section context-timeline-details" data-context-timeline-details ${dialogContextExpanded ? 'open' : ''}>
        <summary class="context-timeline-summary">
          <div><p class="eyebrow">Other publications</p><h3>${fmt.format(contextPapers.length)} papers</h3></div>
          <span class="context-summary-toggle" aria-hidden="true"></span>
        </summary>
        <div class="context-timeline-body">
          <div class="person-timeline-filters" aria-label="Other publication filters">${contextLegend}</div>
          ${personTimelineMarkup(person, 'context')}
        </div>
      </details>
      <div class="detail-grid">
        <section class="detail-section">
          <h3>Database service</h3>
          <ul class="timeline-list">${eventList(dbEvents)}</ul>
        </section>
        <section class="detail-section">
          <h3>Main ML papers</h3>
          <ul class="paper-list">${paperList(mainPapers)}</ul>
          ${groupedContextLists ? `<h3 class="context-list-heading">Other publications</h3>${groupedContextLists}` : ''}
        </section>
        <div class="detail-meta">
          <div class="meta-block"><strong>Name variants</strong><span>${escapeHtml(uniqueSorted([...list(person.aliases?.db), ...list(person.aliases?.publication), ...list(person.aliases?.ml)].map(displayPersonName)).join(' · ') || '—')}</span></div>
          <div class="meta-block"><strong>Affiliations</strong><span>${escapeHtml(personAffiliations(person).join(' · ') || '—')}</span></div>
        </div>
      </div>
    `;
    bindPersonTimeline(person);
  }

  function openPerson(id) {
    const person = allPeople().find((candidate) => candidate.id === id);
    if (!person) return;
    dialogMainVenueVisibility = Object.fromEntries(mainVenueOrder.map((venue) => [venue, true]));
    dialogContextVisibility = Object.fromEntries(contextClassOrder.map((key) => [key, true]));
    dialogContextExpanded = false;
    renderPersonDialog(person);
    if (typeof els.dialog.showModal === 'function') els.dialog.showModal();
    else els.dialog.setAttribute('open', '');
  }

  function openTimelineYear(year, people, options = {}) {
    const mode = options.mode || 'main';
    const visibility = options.visibility || (mode === 'main' ? state.timelineVisible : dialogContextVisibility);
    const entries = aggregatePapers(people)
      .filter((entry) => Number(entry.paper.year) === year)
      .filter((entry) => mode === 'main'
        ? paperScope(entry.paper) === 'main' && visibility[mainVenueLabel(entry.paper)] !== false
        : paperScope(entry.paper) === 'side' && visibility[publicationClass(entry.paper)] !== false)
      .sort((a, b) => {
        if (mode === 'main') return mainVenueOrder.indexOf(mainVenueLabel(a.paper)) - mainVenueOrder.indexOf(mainVenueLabel(b.paper)) || String(a.paper.title).localeCompare(String(b.paper.title));
        return contextClassOrder.indexOf(publicationClass(a.paper)) - contextClassOrder.indexOf(publicationClass(b.paper)) || paperVenueLabel(a.paper).localeCompare(paperVenueLabel(b.paper)) || String(a.paper.title).localeCompare(String(b.paper.title));
      });

    els.timelineDialogContent.innerHTML = `
      <header class="detail-header compact-detail-header">
        <p class="eyebrow">${mode === 'main' ? 'Main ML' : 'Other publications'}</p>
        <h2>${year}</h2>
        <p class="timeline-dialog-summary">${fmt.format(entries.length)} ${entries.length === 1 ? 'paper' : 'papers'}</p>
      </header>
      <ul class="timeline-paper-results">
        ${entries.map((entry) => {
          const paper = entry.paper;
          const isMain = mode === 'main';
          const badgeClass = isMain
            ? `paper-badge ${state.mlVenue
              ? (mainVenueLabel(paper) === state.mlVenue ? 'is-highlight' : 'is-muted')
              : venueClass(mainVenueLabel(paper))}`
            : `paper-badge ${publicationClassCss(publicationClass(paper))}`;
          const badge = isMain ? mainVenueLabel(paper) : publicationClassLabel(paper);
          const title = paper.url ? `<a href="${escapeAttr(paper.url)}" target="_blank" rel="noreferrer">${escapeHtml(paper.title)}</a>` : escapeHtml(paper.title);
          return `<li class="timeline-paper-result"><span class="${badgeClass}">${escapeHtml(badge)}</span><div><strong>${title}</strong><span>${escapeHtml(paperVenueLabel(paper))} · ${escapeHtml(entry.people.join(', '))}</span></div></li>`;
        }).join('') || '<li class="empty-state">No papers in this year.</li>'}
      </ul>
    `;
    if (typeof els.timelineDialog.showModal === 'function') els.timelineDialog.showModal();
    else els.timelineDialog.setAttribute('open', '');
  }

  function renderEverything({ matrix = false, filters = false, summary = false } = {}) {
    if (filters) renderFilters();
    if (summary) renderSummary();
    if (matrix) renderMatrix();
    renderGlobalTimeline();
    renderPeople();
  }

  function resetFilters() {
    state.search = '';
    state.dbVenue = '';
    state.mlVenue = '';
    state.year = '';
    state.role = '';
    state.sort = 'mlPapers';
    state.page = 1;
    state.timelineVisible = Object.fromEntries(mainVenueOrder.map((venue) => [venue, true]));

    els.search.value = '';
    els.dbVenue.value = '';
    els.mlVenue.value = '';
    els.year.value = '';
    els.role.value = '';
    els.sort.value = 'mlPapers';

    renderEverything({ matrix: true, filters: true, summary: true });
  }

  function bind() {
    const rerenderFromFirstPage = ({ refreshFilters = false, matrix = false, summary = false } = {}) => {
      state.page = 1;
      if (refreshFilters) renderFilters();
      if (matrix) renderMatrix();
      if (summary) renderSummary();
      renderGlobalTimeline();
      renderPeople();
    };

    els.search.addEventListener('input', () => { state.search = els.search.value; rerenderFromFirstPage({ refreshFilters: true }); });
    els.dbVenue.addEventListener('change', () => { state.dbVenue = els.dbVenue.value; rerenderFromFirstPage({ refreshFilters: true }); });
    els.mlVenue.addEventListener('change', () => { state.mlVenue = els.mlVenue.value; rerenderFromFirstPage({ refreshFilters: true }); });
    els.year.addEventListener('change', () => { state.year = els.year.value; rerenderFromFirstPage(); });
    els.role.addEventListener('change', () => { state.role = els.role.value; rerenderFromFirstPage({ refreshFilters: true }); });
    els.sort.addEventListener('change', () => { state.sort = els.sort.value; rerenderFromFirstPage(); });
    els.resetFilters.addEventListener('click', resetFilters);
    if (els.emptyResetFilters) els.emptyResetFilters.addEventListener('click', resetFilters);
    els.pageSize.addEventListener('change', () => {
      state.pageSize = els.pageSize.value || 'auto';
      state.page = 1;
      renderPeople();
    });
    els.prevPage.addEventListener('click', (event) => {
      event.preventDefault();
      if (els.prevPage.disabled || state.page <= 1) return;
      state.page = Math.max(1, state.page - 1);
      renderPeople();
    });
    els.nextPage.addEventListener('click', (event) => {
      event.preventDefault();
      if (els.nextPage.disabled) return;
      state.page += 1;
      renderPeople();
    });
    els.dialogClose.addEventListener('click', () => els.dialog.close());
    els.dialog.addEventListener('click', (event) => { if (event.target === els.dialog) els.dialog.close(); });
    els.timelineDialogClose.addEventListener('click', () => els.timelineDialog.close());
    els.timelineDialog.addEventListener('click', (event) => { if (event.target === els.timelineDialog) els.timelineDialog.close(); });
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      if (state.pageSize !== 'auto') return;
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => renderPeople(), 120);
    });
  }

  function init() {
    if (!data || data.meta?.dataReady === false) {
      els.dataStatus.textContent = 'No generated dataset found. Run the DBLP publication scan and website exporter.';
    }
    renderSummary();
    renderFilters();
    renderMatrix();
    renderGlobalTimeline();
    bind();
    renderPeople();
  }

  init();
})();
