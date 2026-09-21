(() => {
  'use strict';

  const data = window.CROSSOVER_DATA || null;
  const $ = (selector) => document.querySelector(selector);
  const fmt = new Intl.NumberFormat('en-US');

  const els = {
    summary: $('#summary-cards'),
    dataStatus: $('#data-status'),
    matrix: $('#matrix-wrap'),
    body: $('#people-body'),
    empty: $('#empty-state'),
    results: $('#results-count'),
    search: $('#search-input'),
    dbVenue: $('#db-venue-filter'),
    mlVenue: $('#ml-venue-filter'),
    role: $('#role-filter'),
    sort: $('#sort-filter'),
    dialog: $('#person-dialog'),
    dialogContent: $('#dialog-content'),
    dialogClose: $('#dialog-close'),
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

  const state = {
    search: '',
    dbVenue: '',
    mlVenue: '',
    role: '',
    sort: 'mlPapers',
  };

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#039;', '"': '&quot;'
    }[char]));
  }

  function normalize(value) {
    return String(value || '').toLocaleLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function uniqueSorted(values) {
    return [...new Set(values.filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b)));
  }

  function labelRole(role) {
    return roleLabels[role] || String(role || '').replaceAll('_', ' ').replace(/\b\w/g, (m) => m.toUpperCase());
  }

  function allPeople() {
    return data ? list(data.people) : [];
  }

  function matches(person) {
    if (state.dbVenue && !list(person.db?.venues).includes(state.dbVenue)) return false;
    if (state.mlVenue && !list(person.ml?.venues).includes(state.mlVenue)) return false;
    if (state.role && !list(person.db?.roles).includes(state.role)) return false;

    const q = normalize(state.search.trim());
    if (!q) return true;
    const haystack = normalize([
      person.name,
      ...list(person.aliases?.db),
      ...list(person.aliases?.ml),
      ...list(person.affiliations),
      ...list(person.db?.venues),
      ...list(person.ml?.venues),
      ...list(person.db?.roles).map(labelRole),
    ].join(' '));
    return haystack.includes(q);
  }

  function sortPeople(people) {
    const copy = [...people];
    const nameSort = (a, b) => a.name.localeCompare(b.name);
    if (state.sort === 'name') return copy.sort(nameSort);
    if (state.sort === 'dbService') {
      return copy.sort((a, b) => (b.db?.serviceEventCount || 0) - (a.db?.serviceEventCount || 0) || nameSort(a, b));
    }
    if (state.sort === 'breadth') {
      return copy.sort((a, b) => ((b.db?.venues?.length || 0) + (b.ml?.venues?.length || 0)) - ((a.db?.venues?.length || 0) + (a.ml?.venues?.length || 0)) || nameSort(a, b));
    }
    if (state.sort === 'recent') {
      return copy.sort((a, b) => (b.ml?.lastYear || 0) - (a.ml?.lastYear || 0) || (b.ml?.paperCount || 0) - (a.ml?.paperCount || 0) || nameSort(a, b));
    }
    return copy.sort((a, b) => (b.ml?.paperCount || 0) - (a.ml?.paperCount || 0) || nameSort(a, b));
  }

  function chip(text, accent = false) {
    return `<span class="chip${accent ? ' chip-accent' : ''}">${escapeHtml(text)}</span>`;
  }

  function renderSummary() {
    const s = data?.summary || {};
    const people = list(data?.people);
    const multiMlVenuePeople = people.filter((p) => list(p.ml?.venues).length >= 2).length;
    const cards = [
      [s.confirmedPeople ?? people.length, 'cross-community researchers'],
      [s.confirmedMlPapers ?? 0, 'ML papers by crossover researchers'],
      [s.confirmedOrcidPeople ?? 0, 'researchers with ORCID'],
      [multiMlVenuePeople, 'active in 2+ ML venues'],
    ];
    els.summary.innerHTML = cards.map(([value, label]) => `
      <div class="summary-card">
        <span class="summary-number">${fmt.format(value || 0)}</span>
        <span class="summary-label">${escapeHtml(label)}</span>
      </div>
    `).join('');

    const generated = data?.meta?.generatedAt ? new Date(data.meta.generatedAt).toLocaleString() : 'unknown';
    els.dataStatus.textContent = '';
  }

  function populateSelect(select, values, labeler = (x) => x) {
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
    const people = list(data?.people);
    populateSelect(els.dbVenue, uniqueSorted(people.flatMap((p) => list(p.db?.venues))));
    populateSelect(els.mlVenue, uniqueSorted(people.flatMap((p) => list(p.ml?.venues))));
    populateSelect(els.role, uniqueSorted(people.flatMap((p) => list(p.db?.roles))), labelRole);
  }

  function confirmedMatrix() {
    const people = list(data?.people);
    const dbVenues = uniqueSorted(people.flatMap((p) => list(p.db?.venues)));
    const mlVenues = uniqueSorted(people.flatMap((p) => list(p.ml?.venues)));
    return { people, dbVenues, mlVenues };
  }

  function renderMatrix() {
    const { people, dbVenues, mlVenues } = confirmedMatrix();
    if (!people.length) {
      els.matrix.innerHTML = '<p class="empty-state">Run the data exporter to populate the matrix.</p>';
      return;
    }
    const head = `<tr><th scope="col">DB venue</th>${mlVenues.map((v) => `<th scope="col">${escapeHtml(v)}</th>`).join('')}</tr>`;
    const rows = dbVenues.map((db) => {
      const cells = mlVenues.map((ml) => {
        const n = people.filter((p) => list(p.db?.venues).includes(db) && list(p.ml?.venues).includes(ml)).length;
        return `<td><button class="matrix-cell" data-db="${escapeHtml(db)}" data-ml="${escapeHtml(ml)}" type="button">${fmt.format(n)}<small>people</small></button></td>`;
      }).join('');
      return `<tr><th scope="row">${escapeHtml(db)}</th>${cells}</tr>`;
    }).join('');
    els.matrix.innerHTML = `<table class="matrix"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
    els.matrix.querySelectorAll('.matrix-cell').forEach((button) => {
      button.addEventListener('click', () => {
        state.dbVenue = button.dataset.db || '';
        state.mlVenue = button.dataset.ml || '';
        els.dbVenue.value = state.dbVenue;
        els.mlVenue.value = state.mlVenue;
        renderPeople();
        document.querySelector('#explore')?.scrollIntoView({ behavior: 'smooth' });
      });
    });
  }

  function yearsText(person) {
    const db = list(person.db?.years);
    const ml = list(person.ml?.years);
    const all = [...db, ...ml].filter((x) => Number.isFinite(Number(x))).map(Number);
    if (!all.length) return '—';
    const min = Math.min(...all), max = Math.max(...all);
    return min === max ? String(min) : `${min}–${max}`;
  }

  function renderPeople() {
    const people = sortPeople(allPeople().filter(matches));
    els.results.textContent = `${fmt.format(people.length)} ${people.length === 1 ? 'researcher' : 'researchers'}`;
    els.empty.hidden = people.length > 0;
    els.body.innerHTML = people.map((person) => {
      return `
        <tr tabindex="0" data-person-id="${escapeHtml(person.id)}" aria-label="Open ${escapeHtml(person.name)} details">
          <td>
            <span class="person-name">${escapeHtml(person.name)}</span>
            <span class="person-sub">${escapeHtml(list(person.affiliations)[0] || person.dblpPid || '')}</span>
          </td>
          <td>
            <div class="chips">${list(person.db?.venues).map((v) => chip(v, state.dbVenue === v)).join('')}</div>
            <div class="metric-inline">${fmt.format(person.db?.serviceEventCount || 0)} service events · ${list(person.db?.roles).map(labelRole).slice(0, 2).join(', ') || 'role unavailable'}</div>
          </td>
          <td>
            <div class="chips">${list(person.ml?.venues).map((v) => chip(v, state.mlVenue === v)).join('')}</div>
            <div class="metric-inline">${fmt.format(person.ml?.paperCount || 0)} papers</div>
          </td>
          <td>${escapeHtml(yearsText(person))}</td>
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
  }

  function eventList(events) {
    if (!events?.length) return '<li>No detailed service events available.</li>';
    return events.map((event) => `
      <li>
        <div class="event-top"><strong>${escapeHtml(event.venue)} · ${escapeHtml(labelRole(event.role))}</strong><span>${escapeHtml(event.year)}</span></div>
        <div class="event-sub">${escapeHtml(event.affiliation || '')}${event.rounds?.length ? ` · ${escapeHtml(event.rounds.join(', '))}` : ''}</div>
      </li>
    `).join('');
  }

  function paperList(papers) {
    if (!papers?.length) return '<li>No detailed paper records available.</li>';
    return papers.map((paper) => {
      const title = escapeHtml(paper.title);
      const linked = paper.url ? `<a class="paper-title" href="${escapeHtml(paper.url)}" target="_blank" rel="noreferrer">${title}</a>` : `<span class="paper-title">${title}</span>`;
      return `<li>${linked}<div class="event-sub">${escapeHtml(paper.venue)} ${escapeHtml(paper.year)} · author #${escapeHtml((paper.authorPosition ?? 0) + 1)}</div></li>`;
    }).join('');
  }

  function openPerson(id) {
    const person = list(data?.people).find((p) => p.id === id);
    if (!person) return;
    const dblpUrl = person.dblpPid ? `https://dblp.org/pid/${encodeURI(person.dblpPid)}` : '';
    const orcid = list(person.orcids)[0];
    const dbEvents = [...list(person.db?.events)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.venue).localeCompare(String(b.venue)));
    const papers = [...list(person.ml?.papers)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.title).localeCompare(String(b.title)));

    els.dialogContent.innerHTML = `
      <header class="detail-header">
        <p class="eyebrow">Cross-community researcher</p>
        <h2>${escapeHtml(person.name)}</h2>
        <div class="detail-links">
          ${dblpUrl ? `<a href="${dblpUrl}" target="_blank" rel="noreferrer">DBLP ↗</a>` : ''}
          ${orcid ? `<a href="https://orcid.org/${escapeHtml(orcid)}" target="_blank" rel="noreferrer">ORCID ↗</a>` : ''}
        </div>
      </header>
      <div class="detail-grid">
        <section class="detail-section">
          <h3>Database service</h3>
          <ul class="timeline-list">${eventList(dbEvents)}</ul>
        </section>
        <section class="detail-section">
          <h3>ML publications</h3>
          <ul class="paper-list">${paperList(papers)}</ul>
        </section>
        <div class="detail-meta">
          <div class="meta-block"><strong>Name variants</strong><span>${escapeHtml(uniqueSorted([...list(person.aliases?.db), ...list(person.aliases?.ml)]).join(' · ') || '—')}</span></div>
          <div class="meta-block"><strong>Affiliations</strong><span>${escapeHtml(list(person.affiliations).join(' · ') || '—')}</span></div>
        </div>
      </div>
    `;
    if (typeof els.dialog.showModal === 'function') els.dialog.showModal();
    else els.dialog.setAttribute('open', '');
  }


  function bind() {
    els.search.addEventListener('input', () => { state.search = els.search.value; renderPeople(); });
    els.dbVenue.addEventListener('change', () => { state.dbVenue = els.dbVenue.value; renderPeople(); });
    els.mlVenue.addEventListener('change', () => { state.mlVenue = els.mlVenue.value; renderPeople(); });
    els.role.addEventListener('change', () => { state.role = els.role.value; renderPeople(); });
    els.sort.addEventListener('change', () => { state.sort = els.sort.value; renderPeople(); });
    els.dialogClose.addEventListener('click', () => els.dialog.close());
    els.dialog.addEventListener('click', (event) => {
      if (event.target === els.dialog) els.dialog.close();
    });
  }

  function init() {
    if (!data || data.meta?.dataReady === false) {
      els.dataStatus.textContent = 'No generated dataset found. Run scripts/build_site_data.py against your data directory.';
    }
    renderSummary();
    renderFilters();
    renderMatrix();
    bind();
    renderPeople();
  }

  init();
})();
