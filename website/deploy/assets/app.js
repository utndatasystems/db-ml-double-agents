(() => {
  'use strict';

  function expandServiceEvents(payload) {
    const events = payload?.browserServiceEvents;
    if (!Array.isArray(events)) return payload;
    for (const person of [...(payload.people || []), ...(payload.additionalExplorerPeople || []),
      ...(payload.populationFocus?.candidates || [])]) {
      if (!Array.isArray(person.db?.eventRefs)) continue;
      person.db.events = person.db.eventRefs.map(index => {
        if (!Number.isInteger(index) || !events[index]) throw new Error('Invalid service record reference');
        // Independent objects preserve the previous data model when profile
        // evidence is loaded or a caller modifies a record.
        return JSON.parse(JSON.stringify(events[index]));
      });
      delete person.db.eventRefs;
    }
    delete payload.browserServiceEvents;
    return payload;
  }

  let data = expandServiceEvents(window.CROSSOVER_DATA) || null;
  const matrixStats = window.CROSSOVER_MATRIX_STATS || null;
  let provenance = data?.provenance || {};
  const $ = (selector) => document.querySelector(selector);
  const fmt = new Intl.NumberFormat('en-US');
  const shareFmt = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 3 });
  const shareLabelFmt = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });

  const els = {
    summary: $('#summary-cards'),
    dataStatus: $('#data-status'),
    dbVenueRates: $('#db-venue-rates'),
    globalTimeline: $('#global-timeline'),
    timelineActionNote: $('#timeline-action-note'),
    timelineLegend: $('#timeline-legend'),
    mlParticipantsLegend: $('#ml-participants-legend'),
    mlParticipantsChart: $('#ml-participants-chart'),
    mlPortfolioChart: $('#ml-portfolio-chart'),
    mlAuthorshipChart: $('#ml-authorship-chart'),
    body: $('#people-body'),
    empty: $('#empty-state'),
    emptyTitle: $('#empty-title'),
    resultsNoteTitle: $('#results-note-title'),
    discoveryNote: $('#explorer-discovery-note'),
    results: $('#results-count'),
    search: $('#search-input'),
    dbVenue: $('#db-venue-filter'),
    mlVenue: $('#ml-venue-filter'),
    year: $('#year-filter'),
    explorerControls: $('#explorer-controls'),
    resetFilters: $('#reset-filters'),
    emptyResetFilters: $('#empty-reset-filters'),
    chartSelectionBanner: $('#chart-selection-banner'),
    chartSelectionLabel: $('#chart-selection-label'),
    clearChartSelection: $('#clear-chart-selection'),
    explorerFilterStatus: $('#explorer-filter-status'),
    explorerPopulation: $('#explorer-population'),
    editExplorerFilters: $('#edit-explorer-filters'),
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
    methodologyDialog: $('#methodology-dialog'),
    methodologyDialogContent: $('#methodology-dialog-content'),
    methodologyDialogClose: $('#methodology-dialog-close'),
    footerDataRefresh: $('#footer-data-refresh'),
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
    main_ml: 'NeurIPS, ICML or ICLR',
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
  const landingView = new URLSearchParams(window.location.search).get('view');

  const state = {
    search: '',
    dbVenue: '',
    mlVenue: '',
    year: '',
    sort: 'mlPapers',

    page: 1,
    pageSize: 'auto',
    timelineVisible: Object.fromEntries(mainVenueOrder.map((key) => [key, true])),
    participantVisible: { first: true, returning: landingView !== 'first-time-authors' },
    participantPeriod: landingView === 'first-time-authors' ? 'home' : 'all',
    timelineNormalized: false,
    publicationScale: landingView === 'publication-share' ? 'relative' : 'absolute',
    publicationView: 'annual',
    dbVenueRatesView: 'percentage',
    explorerCohort: 'crossovers',
    chartDrilldown: null,
  };

  let dialogMainVenueVisibility = Object.fromEntries(mainVenueOrder.map((key) => [key, true]));
  let dialogMainScale = 'absolute';
  let dialogMainView = 'annual';
  let dialogContextVisibility = Object.fromEntries(contextClassOrder.map((key) => [key, true]));
  let dialogContextExpanded = false;
  let dialogMainAuthorship = 'all';
  let dialogContextAuthorship = 'all';
  const publicationSearchByPerson = new Map();
  let publicationSearchState = 'idle';
  let publicationSearchPromise = null;
  let activePersonId = '';
  const personDetailPromises = new Map();
  const personEvidencePromises = new Map();

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

  function normalizeSearch(value) {
    return normalize(value)
      .replace(/[^\p{L}\p{N}]+/gu, ' ')
      .replace(/\s+/g, ' ')
      .trim();
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

  function linearBarHeight(value, maxValue, maxHeight) {
    const numericValue = Number(value);
    const numericMax = Number(maxValue);
    const numericHeight = Number(maxHeight);
    if (!(numericValue > 0) || !(numericMax > 0) || !(numericHeight > 0)) return 0;
    return numericHeight * numericValue / numericMax;
  }

  function mainVenuePublishedTotal(venue, year) {
    const value = Number(data?.mainMlVenueYearTotals?.[venue]?.[String(year)]);
    return Number.isFinite(value) && value >= 0 ? value : 0;
  }

  function visibleMainVenuePublishedTotal(year, visibility) {
    return mainVenueOrder.reduce((sum, venue) => (
      visibility?.[venue] === false ? sum : sum + mainVenuePublishedTotal(venue, year)
    ), 0);
  }

  function timelineScaleToggleMarkup(normalized, attribute) {
    const hint = 'Normalize each year by all papers published at the currently visible NeurIPS, ICML and ICLR venues in that year.';
    return `<button class="timeline-scale-toggle${normalized ? ' is-on' : ''}" type="button" role="switch" aria-checked="${normalized}" ${attribute} title="${escapeAttr(hint)}"><span class="timeline-scale-switch" aria-hidden="true"><i></i></span><span>Normalize by conference size</span></button>`;
  }

  function slug(value) {
    return String(value || '').toLocaleLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  }

  function labelRole(role) {
    return roleLabels[role] || String(role || '').replaceAll('_', ' ').replace(/\b\w/g, (m) => m.toUpperCase());
  }

  function provenanceSource(sourceId) {
    return provenance?.sources?.[sourceId] || null;
  }

  function directDownloadName(url) {
    try {
      const parsed = new URL(url, window.location.href);
      const filename = decodeURIComponent(parsed.pathname.split('/').filter(Boolean).pop() || '');
      return /\.(?:zip|gz|tgz|bz2|xz|7z|csv|tsv|json|xml|sqlite|db|parquet)$/i.test(filename) ? filename : '';
    } catch {
      return '';
    }
  }

  function sourceAnchorAttrs(url) {
    const filename = directDownloadName(url);
    return filename
      ? ` data-confirm-source-download="${escapeAttr(filename)}"`
      : '';
  }

  function sourceLinkMarkup(sourceId) {
    const source = provenanceSource(sourceId);
    if (!source) return '';
    const label = source.release ? `${source.label} · ${source.release}` : source.label;
    if (source.url) {
      return `<a href="${escapeAttr(source.url)}" target="_blank" rel="noreferrer"${sourceAnchorAttrs(source.url)}>${escapeHtml(label)} ↗</a>`;
    }
    return `<span>${escapeHtml(label)}</span>`;
  }

  function eventSourceArrowMarkup(event) {
    const sourceIds = list(event?.provenance?.sourceIds);
    const source = sourceIds.map((sourceId) => provenanceSource(sourceId)).find((candidate) => candidate?.url) || null;
    const url = event?.sourceUrl || source?.url || '';
    if (!url) return '';
    const label = source?.label || `${event?.venue || 'Database community service'} ${event?.year || ''}`.trim();
    return `<a class="source-arrow-link" href="${escapeAttr(url)}" target="_blank" rel="noreferrer"${sourceAnchorAttrs(url)} aria-label="Open source page for ${escapeAttr(label)}" title="Open source page">↗</a>`;
  }

  function provenanceDetailsMarkup({ sourceIds = [], rows = [], summary = 'About this item', className = '' } = {}) {
    const sources = uniqueSorted(sourceIds).map(sourceLinkMarkup).filter(Boolean);
    if (!sources.length && !rows.length) return '';
    const extraClass = className ? ` ${escapeAttr(className)}` : '';
    return `
      <details class="provenance-disclosure${extraClass}">
        <summary><span class="info-icon" aria-hidden="true">i</span><span class="sr-only">${escapeHtml(summary)}</span></summary>
        <div class="provenance-disclosure-body">
          ${rows.map((row) => `<div class="provenance-row"><strong>${escapeHtml(row.label)}</strong><span>${row.html ? row.value : escapeHtml(row.value)}</span></div>`).join('')}
          ${sources.length ? `<div class="provenance-row"><strong>Sources</strong><span>${sources.join('<br>')}</span></div>` : ''}
        </div>
      </details>`;
  }

  function projectReviewRows(reviews) {
    const seen = new Set();
    return list(reviews).flatMap((review) => {
      const key = JSON.stringify([review.reviewFile, review.recordKey, review.personName]);
      if (seen.has(key)) return [];
      seen.add(key);
      const label = {
        identity: 'Identity match', authorship: 'Paper authorship',
        category: 'Publication category', affiliation: 'Affiliation check',
      }[review.kind] || 'Project review';
      const method = review.method === 'ai_assisted' ? 'AI-assisted project review'
        : review.method === 'human' ? 'Human project review' : 'Project review';
      const explanation = {
        identity: 'Our link between a committee name and this DBLP profile.',
        authorship: `Our assignment of this paper to ${review.personName || 'this researcher'}.`,
        category: `Our category assignment: ${publicationClassLabels[review.category] || review.category || 'other'}.`,
        affiliation: review.usedForDisplay ? 'Reviewed publication evidence sets the displayed affiliation.'
          : 'Reviewed publication evidence; it does not set the displayed affiliation.',
      }[review.kind] || 'A decision made by this project.';
      const evidence = list(review.evidenceUrls).filter((url) => {
        try { return new URL(url).protocol === 'https:'; } catch { return false; }
      }).map((url, index) => `<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">Evidence${list(review.evidenceUrls).length > 1 ? ` ${index + 1}` : ''} ↗</a>`).join(' · ');
      const date = review.reviewedOn ? ` · ${escapeHtml(review.reviewedOn)}` : '';
      const reason = review.reason ? `<details class="project-review-reason"><summary>Review reason</summary><p>${escapeHtml(review.reason)}</p></details>` : '';
      return [{ label, value: `${escapeHtml(method)}${date}<br>${escapeHtml(explanation)}${evidence ? `<br>${evidence}` : ''}${reason}`, html: true }];
    });
  }

  function publicationProvenanceMarkup(paper) {
    const rows = [];
    const displayedKey = String(paper?.dblpKey || paper?.canonicalDblpKey || '').trim();
    if (displayedKey) {
      rows.push({
        label: 'Source',
        value: `<a href="https://dblp.org/rec/${escapeAttr(displayedKey)}" target="_blank" rel="noreferrer">DBLP record ↗</a>`,
        html: true,
      });
    }
    const versionLinks = new Map();
    const addVersion = (url, label) => {
      if (url && !versionLinks.has(url)) {
        versionLinks.set(url, `<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">${escapeHtml(label)} ↗</a>`);
      }
    };
    const resolvedVersions = list(paper?.resolvedVersions);
    resolvedVersions.forEach((version) => {
      const key = String(version?.dblpKey || '').trim();
      if (key && key === displayedKey) return;
      const arxivId = String(version?.arxivId || '').trim();
      const kind = version?.isPreprint ? 'Preprint' : 'Published version';
      const year = version?.year ? ` · ${version.year}` : '';
      const title = String(version?.title || '').trim();
      const titleSuffix = title && title !== String(paper?.title || '').trim() ? ` · ${title}` : '';
      if (arxivId) {
        addVersion(`https://arxiv.org/abs/${arxivId}`, `${kind}${year}${titleSuffix}`);
      } else if (key) {
        addVersion(`https://dblp.org/rec/${key}`, `${kind}${year}${titleSuffix}`);
      }
    });
    list(paper?.versionDblpKeys).forEach((key) => {
      if (key !== displayedKey && !resolvedVersions.some((version) => version?.dblpKey === key)) {
        addVersion(`https://dblp.org/rec/${key}`, 'Other DBLP version');
      }
    });
    if (versionLinks.size) rows.push({ label: 'Merged versions', value: [...versionLinks.values()].join('<br>'), html: true });

    const evidenceIds = uniqueSorted([
      ...list(paper?.arxivIds),
      ...list(paper?.provenance?.sourceIds).map(provenanceSource).filter((item) => item?.provider === 'arXiv').map((item) => item.recordId).filter(Boolean),
    ]).filter((id) => !versionLinks.has(`https://arxiv.org/abs/${id}`));
    if (evidenceIds.length) {
      rows.push({
        label: 'arXiv evidence',
        value: evidenceIds.map((id) => `<a href="https://arxiv.org/abs/${escapeAttr(id)}" target="_blank" rel="noreferrer">arXiv:${escapeHtml(id)} ↗</a>`).join('<br>'),
        html: true,
      });
    }
    const reviews = list(paper?.provenance?.projectReviews);
    rows.push(...projectReviewRows(reviews));
    if (paper?.categoryMapping && !reviews.some((review) => review.kind === 'category')) {
      const url = paper.categoryMapping.evidenceUrl;
      rows.push({ label: 'Category assignment', value: `Project venue-to-category mapping, not a DBLP category.${url ? `<br><a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">Venue or proceedings evidence ↗</a>` : ''}`, html: true });
    }
    return provenanceDetailsMarkup({
      sourceIds: displayedKey ? [] : list(paper?.provenance?.sourceIds).filter((id) => id === 'dblp_snapshot'),
      rows,
      summary: 'Publication sources and merged versions',
      className: 'entry-provenance',
    });
  }

  function eventProvenanceMarkup(event) {
    const p = event?.provenance || {};
    const rows = [];
    const originalAffiliations = uniqueSorted(list(p.affiliations).filter((item) => item.changed).map((item) => item.raw).filter(Boolean));
    if (originalAffiliations.length) {
      rows.push({ label: 'Affiliation as listed', value: originalAffiliations.join(' · ') });
    }
    const sourceIds = list(p.sourceIds);
    if (event?.sourceUrl && !sourceIds.some((id) => provenanceSource(id)?.url === event.sourceUrl)) {
      rows.push({ label: 'Source', value: `<a href="${escapeAttr(event.sourceUrl)}" target="_blank" rel="noreferrer"${sourceAnchorAttrs(event.sourceUrl)}>Conference service page ↗</a>`, html: true });
    }
    return provenanceDetailsMarkup({
      sourceIds,
      rows,
      summary: 'Source for this database-service record',
      className: 'entry-provenance',
    });
  }

  function researcherAffiliationEvidence(person) {
    const primary = person?.provenance?.primaryAffiliation || {};
    const selected = personPrimaryAffiliation(person);
    const current = list(primary.currentAffiliations).join(' · ');
    const service = list(primary.latestServiceAffiliations).join(' · ');
    const year = primary.latestServiceYear ? ` (${primary.latestServiceYear})` : '';
    switch (primary.decisionKey) {
      case 'automatic_publication_affiliation':
        return `From publisher author information (${primary.automaticAffiliationDate}).${primary.hasConflict ? ' Multiple affiliations are listed.' : ''}`;
      case 'verified_publication_affiliation':
        return `Verified in this researcher’s ${primary.publicationAffiliationYear} publication.${primary.hasConflict ? ' Other affiliation records differ.' : ''}`;
      case 'service_corroborates_dblp_current':
        return `Listed by DBLP and on conference service pages${year}.${primary.hasConflict ? ' Other service records list different affiliations.' : ''}`;
      case 'single_dblp_current':
        return 'Listed as current by DBLP.';
      case 'multiple_dblp_current_first':
        return `DBLP lists multiple current affiliations: ${current}. Service records do not identify a single choice.`;
      case 'dblp_current_over_stale_service':
        return `Listed as current by DBLP. Service pages${year} instead list ${service || 'a different affiliation'}.`;
      case 'latest_service_only':
        return `From the latest conference service page${year}; DBLP gives no current affiliation.`;
      case 'latest_service_only_conflict':
        return `Service pages${year} list conflicting affiliations: ${service}. DBLP gives no current affiliation.`;
      case 'known_affiliation_fallback':
        return 'From an earlier record; no current or dated affiliation is available.';
      case 'no_affiliation':
        return 'No affiliation is available.';
      default:
        return selected ? 'Latest affiliation recorded in the source data.' : '';
    }
  }

  function researcherOtherAffiliations(person) {
    const records = list(person?.provenance?.affiliations);
    const displayByRaw = new Map(records.map((item) => [normalizeSearch(item.raw), item.display || item.raw]));
    const labelFor = (value) => String(displayByRaw.get(normalizeSearch(value)) || value || '').trim();
    const primary = person?.provenance?.primaryAffiliation || {};
    const selected = new Set([
      ...personPrimaryAffiliation(person).split(' · '),
      ...(primary.decisionKey === 'automatic_publication_affiliation' ? list(primary.automaticAffiliationSource?.rawAffiliations) : []),
    ].map(labelFor).map(normalizeSearch).filter(Boolean));
    const found = new Map();
    const add = (value, sourceIds = [], year = null) => {
      const label = labelFor(value), key = normalizeSearch(label);
      if (!key || selected.has(key)) return;
      if (!found.has(key)) found.set(key, { label, years: new Set(), sources: new Set() });
      const item = found.get(key);
      if (Number.isInteger(Number(year)) && Number(year) > 0) item.years.add(Number(year));
      sourceIds.forEach((id) => {
        const source = provenanceSource(id);
        if (id === 'dblp_snapshot') item.sources.add('DBLP');
        else if (source?.kind === 'official_service_page') {
          item.sources.add('Service records');
          if (Number.isInteger(source.year)) item.years.add(source.year);
        }
      });
    };
    records.forEach((item) => add(item.display || item.raw, list(item.sourceIds)));
    personAffiliations(person).forEach((label) => add(label));
    [...list(person?.displayCurrentAffiliations), ...list(person?.displayFormerAffiliations)].forEach((label) => add(label, ['dblp_snapshot']));
    list(person?.db?.events).forEach((event) => {
      eventAffiliations(event).forEach((label) => add(label, list(event?.provenance?.sourceIds), event.year));
    });
    return [...found.values()].sort((a, b) =>
      Math.max(0, ...b.years) - Math.max(0, ...a.years) || a.label.localeCompare(b.label)
    ).map((item) => {
      const years = [...item.years].sort((a, b) => a - b);
      const period = years.length ? years.length === 1 ? String(years[0]) : `${years[0]}–${years.at(-1)}` : '';
      const evidence = [...item.sources].map((source) => source === 'Service records' && period ? `${source}, ${period}` : source);
      if (period && !item.sources.has('Service records')) evidence.push(`Service records, ${period}`);
      return `<div class="researcher-affiliation"><span class="researcher-affiliation-name">${escapeHtml(item.label)}</span>${evidence.length ? `<small class="researcher-affiliation-source">${evidence.length > 1 ? 'Sources' : 'Source'}: ${escapeHtml(evidence.join(' · '))}</small>` : ''}</div>`;
    }).join('');
  }

  function researcherInfoMarkup(person) {
    if (person.evidenceUrl && !person._evidenceLoaded) {
      const message = person._evidenceState === 'error'
        ? 'Sources could not be loaded. <button type="button" data-retry-person-evidence>Retry</button>'
        : 'Loading affiliation and researcher sources…';
      return provenanceDetailsMarkup({
        rows: [{ label: 'Sources', value: message, html: true }],
        summary: 'Researcher and affiliation sources', className: 'title-provenance',
      });
    }
    const primary = person?.provenance?.primaryAffiliation || {};
    const rows = [];
    if (person?.dblpPid) {
      rows.push({ label: 'Researcher source', value: `<a href="https://dblp.org/pid/${escapeAttr(person.dblpPid)}" target="_blank" rel="noreferrer">DBLP profile ↗</a>`, html: true });
    }
    rows.push(...projectReviewRows(person?.provenance?.identity?.projectReviews));
    const primaryAffiliation = personPrimaryAffiliation(person);
    if (primaryAffiliation) rows.push({ label: 'Last Known Affiliation', value: primaryAffiliation });
    const affiliationEvidence = researcherAffiliationEvidence(person);
    if (affiliationEvidence) rows.push({ label: 'Affiliation evidence', value: affiliationEvidence });
    rows.push(...projectReviewRows(primary.projectReviews));
    const otherAffiliations = researcherOtherAffiliations(person);
    if (otherAffiliations) rows.push({ label: 'Other Found Affiliations', value: otherAffiliations, html: true });
    if (['conflicting_latest_publications', 'same_year_service_conflict'].includes(primary.automaticAffiliationStatus)
        && primary.decisionKey !== 'verified_publication_affiliation') {
      rows.push({ label: 'Affiliation check', value: 'Recent records disagree; the displayed affiliation remains a fallback.' });
    }
    return provenanceDetailsMarkup({
      sourceIds: list(primary.sourceIds).filter((id) => ['official_service_page', 'publication_affiliation', 'automatic_publication_affiliation'].includes(provenanceSource(id)?.kind)),
      rows,
      summary: 'Researcher and affiliation sources',
      className: 'title-provenance',
    });
  }

  function metricContext(metricId) {
    if (metricId === 'db_focus') {
      return {
        label: 'Who counts as a senior DB researcher?',
        description: 'At least twice as many DB as ML conference papers. Requires five papers across three years, or two DB papers in two years plus PC service in two years. Workshops and ordinary journals do not count. The charts follow this same group through their publication history, including papers before their committee service.',
      };
    }
    if (metricId === 'db_service_overlap') {
      return {
        label: 'How to read the overlap',
        description: 'Each cell shows people from the row venue who also served at the column venue. Percentages use the row venue’s members, so reversing the venues can give a different percentage. Each person counts once. “In the same year” requires service at both venues in a common year.',
      };
    }
    if (metricId === 'db_venue_participation') {
      return {
        label: 'Share of members with a paper',
        description: 'Of the senior DB researchers at each DB venue, how many have a main-conference paper at NeurIPS, ICML or ICLR anywhere in their recorded publication history? Percentage divides that number by all senior DB researchers at the DB venue. Absolute shows both counts. A person may appear at several DB venues.',
      };
    }
    if (metricId === 'person_publication_relative') {
      const filterNote = dialogMainAuthorship === 'all' ? '' : ' Only papers matching the selected author position are included.';
      return {
        label: 'What does Relative mean?',
        description: dialogMainView === 'cumulative'
          ? `The percentage of each conference’s main-conference papers authored by this researcher, up to and including the selected year. For example, 10 of a conference’s 100 accumulated papers = 10%.${filterNote}`
          : `The percentage of each conference’s main-conference papers authored by this researcher that year. For example, 2 of a conference’s 20 papers = 10%.${filterNote}`,
        sourceKinds: ['bibliographic_snapshot'],
      };
    }
    if (metricId === 'publication_relative') {
      return {
        label: 'What does Relative mean?',
        description: state.publicationView === 'cumulative'
          ? 'Relative shows the percentage of a venue’s main-conference papers with these DB researchers as authors, accumulated up to and including the selected year. For example, if they authored 20 of the venue’s 1,000 accumulated papers, Relative is 2%.'
          : 'Relative shows the percentage of a venue’s main-conference papers with these DB researchers as authors. For example, if they authored 20 of the venue’s 1,000 papers that year, Relative is 2%.',
        sourceKinds: ['bibliographic_snapshot'],
      };
    }
    if (metricId === 'derived-results') {
      return {
        label: 'Headline statistics',
        description: 'Headline cards count researchers with documented database community service and main-conference papers at NeurIPS, ICML or ICLR. Other publications are contextual only. This observes publication/service overlap and does not infer a field change.',
        sourceKinds: ['official_service_page', 'bibliographic_snapshot'],
      };
    }
    if (metricId === 'publication_activity') {
      return {
        label: 'Main-conference publication activity',
        description: 'Counts main-conference papers at NeurIPS, ICML and ICLR involving senior DB researchers. A paper with several senior DB researchers counts once. Annual shows one year; Cumulative includes papers through that year. The same group is followed throughout its publication history, including before committee service.',
        sourceKinds: ['bibliographic_snapshot'],
      };
    }
    if (metricId === 'ml_participants') {
      return {
        label: 'Senior DB researchers publishing each year',
        description: 'Each senior DB researcher counts once per year with a main-conference paper at NeurIPS, ICML or ICLR. First time means their earliest recorded paper across these three venues, not their first research paper. Published before means they also published at one of these venues earlier.',
        sourceKinds: ['official_service_page', 'bibliographic_snapshot'],
      };
    }
    if (metricId === 'ml_portfolio') {
      const excluded = allPeople().filter((person) => mainMlPortfolioShare(person) === null).length;
      return {
        label: 'How publication share is calculated',
        description: `For each researcher, divide their main-conference papers at NeurIPS, ICML or ICLR by all their publications. For example, 10 of 200 publications = 5%.${excluded ? ` ${fmt.format(excluded)} researchers are excluded because publication counts are missing or invalid.` : ''}`,
        sourceKinds: ['bibliographic_snapshot'],
      };
    }
    if (metricId === 'crossover_authorship') {
      const counts = crossoverAuthorshipDistribution();
      return {
        label: 'How author positions are counted',
        description: `Each researcher–paper pair is assigned Sole, then First, then Last, otherwise Middle. A paper with two senior DB researchers contributes two authorships. Records without a valid author position or total author count are left out${counts.excluded ? ` (${fmt.format(counts.excluded)} authorships)` : ''}. Selecting a role opens distinct researchers, so someone may appear in several role lists. Author order does not necessarily measure contribution.`,
        sourceKinds: ['bibliographic_snapshot'],
      };
    }
    if (metricId === 'person_publication_annual' || metricId === 'person_publication_cumulative') {
      const cumulative = metricId === 'person_publication_cumulative';
      return {
        label: cumulative ? 'Cumulative main-conference papers for this researcher' : 'Annual main-conference papers for this researcher',
        description: `Three grouped columns for NeurIPS, ICML and ICLR. ${cumulative
          ? 'Absolute counts this researcher’s main papers through each year. Relative = 100 × this researcher’s main papers through that year / all main papers at the same venue through that year. Both running totals use only years with available conference totals, starting with the earliest available totals, including years before this researcher’s first paper. This is a ratio of running counts, not a sum or average of annual percentages.'
          : 'Absolute counts this researcher’s main papers in each year. Relative = 100 × this researcher’s main papers at a venue in that year / all main papers at that venue in that year. For example, 2 of 1,000 papers = 0.2%.'} Each venue has its own denominator; percentages should not be added across venues. Years without conference totals are unavailable, not zero. ${cumulative ? 'Selecting a column opens papers through that year; Relative includes only years with conference totals.' : 'Selecting a column opens papers from that year only.'}`,
        sourceKinds: ['bibliographic_snapshot'],
      };
    }
    if (metricId === 'publication_annual' || metricId === 'publication_cumulative') {
      const cumulative = metricId === 'publication_cumulative';
      const description = cumulative
        ? 'Absolute counts unique main-conference papers involving the full observed senior DB researcher cohort at each venue through the indicated year. Relative = 100 × cohort papers through that year / all main papers at that venue through that year. Both running totals use only years with available conference totals, starting with the earliest available totals, including years before the first cohort paper. It does not add or average annual percentages. Years without conference totals are excluded from both running totals and shown as unavailable. Each venue has its own denominator; percentages should not be added across venues. Explorer filters do not affect this chart. A point opens papers through that year; Relative includes only years with conference totals.'
        : 'Absolute counts unique main-conference papers involving the full observed senior DB researcher cohort in each year and venue. A paper involving multiple cohort researchers counts once. Relative = 100 × cohort papers at a venue in a year / all main papers at that venue in that year. For example, 20 of 1,000 papers = 2%. Each venue has its own denominator; percentages should not be added across venues. Missing conference totals appear as unavailable, not zero. Explorer filters do not affect this chart. Select a point to inspect that venue’s papers for the year.';
      return {
        label: cumulative ? 'Cumulative ML publication activity' : 'Annual ML publication activity',
        description,
        sourceKinds: ['bibliographic_snapshot'],
      };
    }
    const metric = provenance?.metrics?.[metricId];
    if (!metric) return null;
    return {
      label: metric.label || metricId,
      description: metric.description || '',
      sourceKinds: list(metric.sourceKinds),
    };
  }

  let activeInfoAnchor = null;
  let activeInfoPinned = false;
  let infoCloseTimer = null;
  let restoringInfoFocus = false;
  const infoAnchorSelector = '[data-provenance-metric], .provenance-disclosure > summary, .authorship-info > summary';
  const infoPanelSelector = '.metric-context-panel, .provenance-disclosure-body, .authorship-popover';

  function activeInfoPanel() {
    const details = activeInfoAnchor?.closest('.provenance-disclosure, .authorship-info');
    return details?.querySelector('.provenance-disclosure-body, .authorship-popover')
      || document.querySelector('.metric-context-panel');
  }

  function cancelInfoClose() {
    window.clearTimeout(infoCloseTimer);
  }

  function scheduleInfoClose() {
    cancelInfoClose();
    const anchor = activeInfoAnchor;
    infoCloseTimer = window.setTimeout(() => {
      const panel = activeInfoPanel();
      if (!anchor || activeInfoAnchor !== anchor || activeInfoPinned
        || anchor.matches(':hover, :focus-visible') || panel?.matches(':hover')
        || panel?.contains(document.activeElement)) return;
      closeMetricInfoPanels();
    }, 180);
  }

  function positionFloatingInfo(anchor, panel) {
    if (!anchor?.isConnected || !panel?.isConnected) return;
    const viewportPadding = 12;
    const anchorGap = 8;
    // The reserved scrollbar gutter is outside the page's visible content width.
    const viewportWidth = Math.min(window.innerWidth, document.documentElement.getBoundingClientRect().width);
    panel.style.left = `${viewportPadding}px`;
    panel.style.top = `${viewportPadding}px`;
    const anchorRect = anchor.getBoundingClientRect();
    const spaceAbove = Math.max(0, anchorRect.top - anchorGap - viewportPadding);
    const spaceBelow = Math.max(0, window.innerHeight - anchorRect.bottom - anchorGap - viewportPadding);
    panel.style.maxHeight = `min(70vh, 560px, ${Math.floor(Math.max(spaceAbove, spaceBelow))}px)`;
    const panelRect = panel.getBoundingClientRect();
    const maxLeft = Math.max(viewportPadding, viewportWidth - viewportPadding - panelRect.width);
    const centeredLeft = anchorRect.left + (anchorRect.width / 2) - (panelRect.width / 2);
    const left = Math.min(maxLeft, Math.max(viewportPadding, centeredLeft));
    const below = anchorRect.bottom + anchorGap;
    const above = anchorRect.top - anchorGap - panelRect.height;
    const maxTop = Math.max(viewportPadding, window.innerHeight - viewportPadding - panelRect.height);
    let top = below;
    if (below + panelRect.height > window.innerHeight - viewportPadding && above >= viewportPadding) top = above;
    top = Math.min(maxTop, Math.max(viewportPadding, top));
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
  }

  function closeMetricInfoPanels() {
    cancelInfoClose();
    activeInfoPinned = false;
    document.querySelectorAll('.metric-context-panel').forEach((panel) => panel.remove());
    document.querySelectorAll('[data-provenance-metric][aria-expanded="true"]').forEach((button) => {
      button.setAttribute('aria-expanded', 'false');
      button.removeAttribute('aria-describedby');
    });
    document.querySelectorAll('.provenance-disclosure[open], .authorship-info[open]').forEach((details) => { details.open = false; });
    activeInfoAnchor = null;
  }

  function repositionOpenInfo() {
    if (!activeInfoAnchor?.isConnected) {
      closeMetricInfoPanels();
      return;
    }
    const panel = activeInfoPanel();
    if (panel) positionFloatingInfo(activeInfoAnchor, panel);
  }

  function toggleDisclosureInfo(summary) {
    const details = summary.closest('.provenance-disclosure, .authorship-info');
    if (!details) return;
    const wasOpen = details.open && activeInfoAnchor === summary;
    closeMetricInfoPanels();
    if (wasOpen) return;
    details.open = true;
    activeInfoAnchor = summary;
    const panel = details.querySelector('.provenance-disclosure-body, .authorship-popover');
    if (panel) positionFloatingInfo(summary, panel);
  }

  function toggleMetricInfo(button, metricId) {
    const shouldOpen = activeInfoAnchor !== button || !document.querySelector('.metric-context-panel');
    closeMetricInfoPanels();
    if (!shouldOpen) return;

    const context = metricId === 'publication_point'
      ? { label: button.dataset.pointLabel, description: button.dataset.pointDetail }
      : metricContext(metricId);
    if (!context) return;
    const panel = document.createElement('div');
    panel.className = 'metric-context-panel';
    panel.dataset.metricId = metricId;
    panel.id = `metric-info-${metricId}`;
    panel.setAttribute('role', 'tooltip');
    if (['publication_relative', 'person_publication_relative', 'ml_portfolio', 'publication_point'].includes(metricId)) {
      panel.id = metricId === 'publication_point' ? 'publication-point-explanation'
        : metricId === 'ml_portfolio' ? 'publication-share-explanation' : `${metricId === 'person_publication_relative' ? 'person-' : ''}publication-relative-explanation`;
    }
    button.setAttribute('aria-describedby', panel.id);
    const methodologyMetric = (metricId === 'publication_annual' || metricId === 'publication_cumulative' || metricId === 'publication_relative' || metricId === 'ml_participants'
      || metricId === 'person_publication_annual' || metricId === 'person_publication_cumulative'
      || metricId === 'crossover_authorship')
      ? 'publication_activity' : metricId;
    const methodologyLabel = metricId === 'crossover_matrix'
      ? 'How this matrix is calculated →'
      : methodologyMetric === 'publication_activity'
        ? 'How publication activity is calculated →'
        : 'See definition and sources →';
    const briefMetrics = ['publication_relative', 'person_publication_relative', 'ml_portfolio', 'publication_point',
      'publication_activity', 'ml_participants', 'crossover_authorship', 'db_focus', 'db_service_overlap', 'db_venue_participation'];
    const more = metricId === 'db_focus'
      ? '<a class="metric-methodology-link" href="#population-focus">See the selection rules →</a>'
      : briefMetrics.includes(metricId) ? ''
        : `<button class="metric-methodology-link" type="button" data-open-methodology-metric="${escapeAttr(methodologyMetric)}">${escapeHtml(methodologyLabel)}</button>`;
    panel.innerHTML = `
      <strong>${escapeHtml(context.label)}</strong>
      <span>${escapeHtml(context.description)}</span>
      ${more}
    `;
    // A modal dialog makes elements outside it inert: keep its info panel inside the dialog.
    (button.closest('dialog[open]') || document.body).appendChild(panel);
    activeInfoAnchor = button;
    button.setAttribute('aria-expanded', 'true');
    positionFloatingInfo(button, panel);
  }

  function showInfo(anchor) {
    cancelInfoClose();
    if (activeInfoAnchor === anchor && activeInfoPanel()) return;
    if (anchor.matches('[data-provenance-metric]')) toggleMetricInfo(anchor, anchor.dataset.provenanceMetric);
    else toggleDisclosureInfo(anchor);
  }

  function pinInfo(anchor) {
    if (activeInfoAnchor === anchor && activeInfoPinned) {
      closeMetricInfoPanels();
    } else {
      showInfo(anchor);
      activeInfoPinned = activeInfoAnchor === anchor;
    }
  }

  function bindInfoInteractions() {
    // Delegation also covers paper lists and researcher details loaded later.
    document.addEventListener('pointerover', (event) => {
      if (!['mouse', 'pen'].includes(event.pointerType)) return;
      const anchor = event.target.closest(infoAnchorSelector);
      if (anchor && !anchor.contains(event.relatedTarget)) showInfo(anchor);
      else if (event.target.closest(infoPanelSelector) === activeInfoPanel()) cancelInfoClose();
    });
    document.addEventListener('pointerout', (event) => {
      if (!['mouse', 'pen'].includes(event.pointerType)) return;
      const anchor = event.target.closest(infoAnchorSelector);
      const panel = event.target.closest(infoPanelSelector);
      const region = anchor === activeInfoAnchor ? anchor : panel === activeInfoPanel() ? panel : null;
      if (region && !region.contains(event.relatedTarget)) scheduleInfoClose();
    });
    document.addEventListener('focusin', (event) => {
      const anchor = event.target.closest(infoAnchorSelector);
      if (anchor && !restoringInfoFocus) showInfo(anchor);
      else if (event.target.closest(infoPanelSelector) === activeInfoPanel()) cancelInfoClose();
    });
    document.addEventListener('focusout', (event) => {
      if (!activeInfoAnchor) return;
      if (event.target.closest(infoAnchorSelector) === activeInfoAnchor
        || event.target.closest(infoPanelSelector) === activeInfoPanel()) scheduleInfoClose();
    });
  }
  function allPeople() {
    return data ? list(data.people) : [];
  }

  function explorerPeople() {
    return [...allPeople(), ...list(data?.additionalExplorerPeople)];
  }

  let pcEventLookupData = null;
  let pcEventLookup = new Map();
  function pcEventsFor(person) {
    if (!data?.populationFocus) return list(person?.db?.events);
    if (pcEventLookupData !== data) {
      // The exporter already applies Home's role, year and Shadow-PC policy.
      pcEventLookup = new Map(list(data.populationFocus.candidates)
        .map(candidate => [candidate.dblpPid, list(candidate.db?.events)]));
      pcEventLookupData = data;
    }
    return pcEventLookup.get(person?.dblpPid) || [];
  }

  const dbVenueLabel = venue => venue === 'SIGMOD' ? 'SIGMOD/PODS' : venue;

  function sharedPublicationEndYear() {
    const years = mainVenueOrder.map(venue => Object.entries(data?.mainMlVenueYearTotals?.[venue] || {})
      .filter(([year, total]) => /^\d{4}$/.test(year) && Number.isFinite(total) && total > 0)
      .map(([year]) => Number(year)));
    const common = years[0].filter(year => years.every(covered => covered.includes(year)));
    return common.length ? Math.max(...common) : null;
  }

  function latestCoverageNote(year) {
    const venues = mainVenueOrder.filter(venue => mainVenuePublishedTotal(venue, year) > 0);
    return venues.length && venues.length < mainVenueOrder.length
      ? `${year} coverage: ${selectionListLabel(venues)} only.` : '';
  }

  function focusBadgeMarkup(person) {
    const focus = person?.focus;
    if (!focus || focus.included !== false) return '';
    const limited = focus.label === 'Limited evidence';
    const label = 'Not considered DB-focused';
    const counts = `${fmt.format(focus.dbPapers)} DB · ${fmt.format(focus.mlPapers)} ML conference papers`;
    const missing = [Number(focus.dbPapers) + Number(focus.mlPapers) < 5 ? '5 qualifying papers' : '',
      Number(focus.publicationYears) < 3 ? '3 publication years' : ''].filter(Boolean);
    const explanation = limited
      ? `${counts} across ${fmt.format(focus.publicationYears)} publication years. The publication route requires ${missing.join(' and ')}. The service fallback is not met.`
      : `${counts}. Inclusion requires at least twice as many qualifying DB papers as ML papers.`;
    return `<details class="provenance-disclosure focus-badge"><summary aria-label="${escapeAttr(label + ': why this person is outside the study cohort')}"><svg class="focus-badge-icon" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M7.1 2.1a1 1 0 0 1 1.8 0l5.6 10a1 1 0 0 1-.9 1.5H2.4a1 1 0 0 1-.9-1.5Z"/><path d="M8 5.5v3.3M8 11h0"/></svg><span>${label}</span><svg class="focus-badge-icon" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="8" cy="8" r="6.2"/><path d="M8 7.2v4M8 4.7h0"/></svg></summary><div class="provenance-disclosure-body"><strong>Outside the study cohort</strong><p>${escapeHtml(explanation)}</p><button type="button" class="metric-methodology-link" data-open-methodology>See the inclusion rules</button></div></details>`;
  }

  function personDetailUrl(person) {
    return String(person?.ml?.detailUrl || '');
  }

  function personDetailsReady(person) {
    return !personDetailUrl(person) || person?._publicationDetailsLoaded === true;
  }

  function personContextPaperCount(person) {
    if (personDetailsReady(person)) return sidePapersFor(person).length;
    return Math.max(0, Number(person?.ml?.sidePaperCount) || 0);
  }

  function personDetailStateMarkup(person) {
    if (person?._publicationDetailsState === 'error') {
      return `<p class="detail-loading-error">Other publication details could not be loaded.<button type="button" data-retry-person-details>Retry</button></p>`;
    }
    return `<div class="detail-loading-preview" aria-busy="true">${window.LoadingPreview?.markup('columns') || ''}<p class="detail-loading-state" role="status">Loading other publication details…</p></div>`;
  }

  async function loadPersonDetails(person, { retry = false } = {}) {
    const url = personDetailUrl(person);
    if (!url || personDetailsReady(person)) return person;
    const key = String(person.id || url);
    if (!retry && personDetailPromises.has(key)) return personDetailPromises.get(key);

    person._publicationDetailsState = 'loading';
    const promise = fetch(url, { cache: 'force-cache' })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((detail) => {
        if (detail?.personId && String(detail.personId) !== String(person.id)) {
          throw new Error('Publication detail identity mismatch');
        }
        const eager = list(person?.ml?.papers);
        const seen = new Set(eager.map((paper) => `${paper?.workId || ''}|${paper?.dblpKey || ''}|${paper?.title || ''}`));
        const deferred = list(detail?.papers).filter((paper) => {
          const keyValue = `${paper?.workId || ''}|${paper?.dblpKey || ''}|${paper?.title || ''}`;
          if (seen.has(keyValue)) return false;
          seen.add(keyValue);
          return true;
        });
        person.ml.papers = [...eager, ...deferred];
        person._publicationDetailsLoaded = true;
        person._publicationDetailsState = 'ready';
        return person;
      })
      .catch((error) => {
        person._publicationDetailsState = 'error';
        throw error;
      })
      .finally(() => {
        personDetailPromises.delete(key);
      });
    personDetailPromises.set(key, promise);
    return promise;
  }

  function refreshOpenPersonAfterDetails(person) {
    if (activePersonId !== person.id) return;
    if (!(els.dialog.open || els.dialog.hasAttribute('open'))) return;
    renderPersonDialog(person);
  }

  function startPersonDetailLoad(person, options = {}) {
    loadPersonDetails(person, options)
      .then(() => refreshOpenPersonAfterDetails(person))
      .catch(() => refreshOpenPersonAfterDetails(person));
  }

  function refreshPersonEvidence(person) {
    if (activePersonId !== person.id || !els.dialog.open) return;
    // Update evidence in place: an open tooltip, keyboard focus, chart
    // selections and the popup's scroll position must survive the download.
    const updateDisclosure = (current, markup) => {
      if (!current) return;
      const template = document.createElement('template');
      template.innerHTML = markup;
      const fresh = template.content.querySelector('.provenance-disclosure-body');
      const body = current.querySelector('.provenance-disclosure-body');
      if (body && fresh) body.replaceChildren(...fresh.childNodes);
    };
    updateDisclosure(els.dialogContent.querySelector('.title-provenance'), researcherInfoMarkup(person));
    const events = [...list(person.db?.events)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.venue).localeCompare(String(b.venue)));
    els.dialogContent.querySelectorAll('.timeline-list > li').forEach((item, index) => {
      if (events[index]) updateDisclosure(item.querySelector('.entry-provenance'), eventProvenanceMarkup(events[index]));
    });
    repositionOpenInfo();
  }

  function startPersonEvidenceLoad(person, { retry = false } = {}) {
    if (!person.evidenceUrl || person._evidenceLoaded) return Promise.resolve(person);
    if (!retry && personEvidencePromises.has(person.id)) return personEvidencePromises.get(person.id);
    person._evidenceState = 'loading';
    refreshPersonEvidence(person);
    const promise = fetch(person.evidenceUrl, { cache: 'force-cache' })
      .then(response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then(detail => {
        if (detail?.personId !== person.id || !detail.provenance || !Array.isArray(detail.events)) {
          throw new Error('Researcher evidence identity mismatch');
        }
        person.provenance = detail.provenance;
        person.db.events = detail.events;
        person._evidenceLoaded = true;
        person._evidenceState = 'ready';
        return person;
      })
      .catch(() => { person._evidenceState = 'error'; })
      .finally(() => {
        personEvidencePromises.delete(person.id);
        refreshPersonEvidence(person);
      });
    personEvidencePromises.set(person.id, promise);
    return promise;
  }

  function broaderPublicationSearchText(person) {
    return publicationSearchByPerson.get(String(person?.id || '')) || '';
  }

  async function ensurePublicationSearchIndex() {
    const url = String(data?.meta?.publicationSearchUrl || '');
    if (!url || publicationSearchState === 'ready') return;
    if (publicationSearchPromise) return publicationSearchPromise;
    publicationSearchState = 'loading';
    publicationSearchPromise = fetch(url, { cache: 'force-cache' })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        Object.entries(payload?.people || {}).forEach(([personId, titles]) => {
          publicationSearchByPerson.set(String(personId), normalizeSearch(titles));
        });
        publicationSearchState = 'ready';
      })
      .catch((error) => {
        publicationSearchState = 'error';
        throw error;
      })
      .finally(() => {
        publicationSearchPromise = null;
      });
    return publicationSearchPromise;
  }

  function startPublicationSearchLoad() {
    const query = normalizeSearch(state.search.trim());
    if (query.length < 3 || publicationSearchState === 'ready' || publicationSearchState === 'loading') return;
    if (!data?.meta?.publicationSearchUrl) return;
    ensurePublicationSearchIndex()
      .then(() => {
        if (normalizeSearch(state.search.trim()).length < 3) return;
        renderFilters();
        renderPeople();
      })
      .catch(() => renderPeople());
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
    if (paperScope(paper) === 'main') return 'main_ml';
    const value = String(paper?.publicationClass || '').toLocaleLowerCase();
    // Backward compatibility for generated data from the short-lived satellite experiment:
    // keep those side papers as ordinary publication context rather than an ML tier.
    if (value === 'satellite') {
      const metadata = [paper?.booktitle, paper?.venueLabel, paper?.exactVenue].filter(Boolean).join(' ');
      return /\bworkshops?\b/i.test(metadata) ? 'workshop' : 'other_conference';
    }
    if (value && publicationClassLabels[value]) return value;
    return 'other';
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
    return venue || 'Main-conference';
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
    const years = allPeople().flatMap((person) =>
      mainPapersFor(person).map((paper) => Number(paper.year))
    ).filter(Number.isFinite);
    const end = years.length ? Math.max(...years) : new Date().getFullYear();
    return { start: end - 25, end };
  }

  function resolvedPageSize() {
    if (state.pageSize !== 'auto') return Math.max(1, Number(state.pageSize) || 25);
    const available = Math.max(360, window.innerHeight - 390);
    return Math.max(5, Math.min(14, Math.floor(available / 66)));
  }

  function matches(person, { ignoreYear = false } = {}) {
    const q = normalizeSearch(state.search.trim());

    // The default view is the study's DB-focused ML publishers. All/search can
    // include other matched PC members without changing any chart's population.
    if (state.explorerCohort === 'crossovers' && (person.focus?.included === false || !hasMain(person))) return false;
    if (state.chartDrilldown && !matchesChartDrilldown(person)) return false;

    const dbEvents = pcEventsFor(person).filter((event) => {
      if (state.dbVenue && event.venue !== state.dbVenue) return false;
      return true;
    });
    if (state.dbVenue && dbEvents.length === 0) return false;

    if (state.mlVenue || (!ignoreYear && selectedYear())) {
      const paperOk = mainPapersFor(person).some((paper) => paperMatchesExplorerFilters(paper, { ignoreYear }));
      if (!paperOk) return false;
    }

    if (!q) return true;
    const mainPapers = mainPapersFor(person);
    const haystack = normalizeSearch([
      person.name,
      displayPersonName(person.name),
      ...list(person.aliases?.db),
      ...list(person.aliases?.publication),
      ...list(person.aliases?.ml),
      ...list(person.affiliations),
      ...personAffiliations(person),
      personPrimaryAffiliation(person),
      ...list(person.db?.events).flatMap(eventAffiliations),
      ...list(person.db?.venues),
      ...mainPapers.map(mainVenueLabel),
      ...mainPapers.map((paper) => paper.title),
      ...list(person.db?.roles).map(labelRole),
    ].join(' '));
    return haystack.includes(q) || broaderPublicationSearchText(person).includes(q);
  }

  function matchesChartDrilldown(person) {
    const selection = state.chartDrilldown;
    if (!selection) return true;
    if (person.focus?.included === false) return false;
    if (selection.kind === 'ml-status') {
      return selection.value === 'with' ? hasMain(person) : !hasMain(person);
    }
    if (selection.kind === 'venue-service' || selection.kind === 'venue-ml') {
      const served = pcEventsFor(person).some((event) => event.venue === selection.value);
      return served && (selection.kind === 'venue-service' || hasMain(person));
    }
    if (selection.kind === 'ml-year') {
      return mainPapersFor(person).some((paper) => Number(paper.year) === Number(selection.value));
    }
    if (selection.kind === 'ml-first-year') {
      return firstMainMlYear(person) === Number(selection.value);
    }
    if (selection.kind === 'ml-returning-year') {
      const firstYear = firstMainMlYear(person);
      const year = Number(selection.value);
      return firstYear !== null && firstYear < year && mainMlPublicationYears(person).includes(year);
    }
    if (selection.kind === 'portfolio') {
      const share = mainMlPortfolioShare(person);
      return share !== null && portfolioBins()[selection.value]?.matches(share) === true;
    }
    if (selection.kind === 'authorship-role') {
      return ['sole', 'first', 'last', 'other'].includes(selection.value)
        && mainPapersFor(person).some((paper) => crossoverAuthorshipRole(paper) === selection.value);
    }
    return false;
  }

  function mainMlPortfolioShare(person) {
    const values = [person?.ml?.paperCount, person?.ml?.mainPaperCount];
    if (values.some((value) => value == null || String(value).trim() === '' || typeof value === 'boolean')) return null;
    const total = Number(person?.ml?.paperCount);
    const main = Number(person?.ml?.mainPaperCount);
    if (!Number.isInteger(total) || total <= 0 || !Number.isInteger(main) || main < 0 || main > total) return null;
    return 100 * main / total;
  }

  let portfolioBinsData = null, portfolioBinDefinitions = null;
  function portfolioBins() {
    if (portfolioBinDefinitions && portfolioBinsData === data) return portfolioBinDefinitions;
    const maximum = Math.max(0, ...allPeople().map(mainMlPortfolioShare).filter(Number.isFinite));
    const upperBound = Math.min(100, Math.ceil(maximum) + 1);
    const bins = [{ label: '0%', description: 'no observed main-conference works (0%)', matches: (share) => share === 0, count: 0 }];
    for (let upper = 1; upper <= upperBound; upper += 1) {
      const lower = upper - 1;
      bins.push({ label: `>${lower}–${upper}%`, description: `more than ${lower}% and at most ${upper}%`, matches: (share) => share > lower && share <= upper, count: 0 });
    }
    portfolioBinsData = data;
    portfolioBinDefinitions = bins;
    return portfolioBinDefinitions;
  }

  function filteredPeople() {
    return explorerPeople().filter(matches);
  }

  function explorerScopePeople() {
    return state.explorerCohort === 'all' ? explorerPeople() : scopePeople();
  }

  function updateExplorerCohortSwitch() {
    document.querySelectorAll('[data-explorer-cohort]').forEach((button) => {
      const active = button.dataset.explorerCohort === state.explorerCohort;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function updateExplorerSort() {
    document.querySelectorAll('[data-explorer-sort]').forEach((button) => {
      const active = button.dataset.explorerSort === state.sort;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function updateEditFiltersVisibility() {
    if (!els.explorerControls || !els.editExplorerFilters) return;
    const headerBottom = document.querySelector('.site-header')?.getBoundingClientRect().bottom || 0;
    const filterElements = [...els.explorerControls.querySelectorAll('input, select')];
    if (els.chartSelectionBanner && !els.chartSelectionBanner.hidden) filterElements.push(els.chartSelectionBanner);
    const filtersVisible = filterElements.some((field) => {
      const bounds = field.getBoundingClientRect();
      return bounds.bottom > headerBottom && bounds.top < window.innerHeight;
    });
    els.editExplorerFilters.hidden = filtersVisible;
    els.resetFilters.hidden = !filtersVisible;
  }

  function focusExplorerFilterAccess() {
    updateEditFiltersVisibility();
    (els.editExplorerFilters.hidden ? els.search : els.editExplorerFilters).focus({ preventScroll: true });
  }

  function explorerPopulationLabel() {
    const focus = state.explorerCohort === 'crossovers' || state.chartDrilldown ? 'senior DB ' : '';
    const group = state.dbVenue ? focus ? `senior DB researchers at ${dbVenueLabel(state.dbVenue)}` : `${dbVenueLabel(state.dbVenue)} PC members`
      : focus ? 'senior DB researchers' : 'DB venue PC members';
    if (state.explorerCohort === 'all' && !state.mlVenue && !selectedYear()) return `All ${group}`;
    const venues = state.mlVenue || 'NeurIPS, ICML or ICLR';
    const year = selectedYear() ? ` in ${selectedYear()}` : '';
    return `${group} with at least one ML paper at ${venues}${year}`;
  }

  function updateChartSelectionBanner() {
    if (!els.chartSelectionBanner) return;
    const sources = {
      'venue-service': 'DB PC membership',
      'ml-status': 'Main-conference participation',
      'venue-ml': 'Main-conference participation by DB venue',
      'ml-year': 'Researcher participation',
      'ml-first-year': 'Researcher participation',
      'ml-returning-year': 'Researcher participation',
      portfolio: 'Individual publication portfolios',
      'authorship-role': 'Authorship roles',
    };
    const refinements = [];
    const selectedDbVenue = ['venue-service', 'venue-ml'].includes(state.chartDrilldown?.kind)
      ? state.chartDrilldown.value : '';
    if (state.dbVenue && state.dbVenue !== selectedDbVenue) refinements.push(`${state.dbVenue} service`);
    if (state.mlVenue || selectedYear()) {
      refinements.push(`papers${state.mlVenue ? ` at ${state.mlVenue}` : ''}${selectedYear() ? ` in ${selectedYear()}` : ''}`);
    }
    const refinementText = refinements.length ? ` · Also matching: ${refinements.join('; ')}` : '';
    els.chartSelectionBanner.hidden = !state.chartDrilldown;
    els.chartSelectionLabel.textContent = state.chartDrilldown
      ? `Chart: ${sources[state.chartDrilldown.kind] || 'Selection'} / ${state.chartDrilldown.label}${refinementText}` : '';
    els.explorerPopulation.textContent = explorerPopulationLabel();
    updateEditFiltersVisibility();
  }

  function resetManualExplorerFilters() {
    ['search', 'dbVenue', 'mlVenue', 'year'].forEach((key) => {
      state[key] = '';
      els[key].value = '';
    });
    state.page = 1;
  }

  function selectResearchersFromChart(kind, value, label) {
    if (!els.body) {
      const url = new URL('people.html', window.location.href);
      url.searchParams.set('selection', kind);
      url.searchParams.set('value', value);
      url.searchParams.set('label', label);
      window.location.assign(url.href);
      return;
    }
    closeMetricInfoPanels();
    // Replace previous explorer selections; never modify the chart population.
    resetManualExplorerFilters();
    // Some chart selections include senior DB researchers with no main-conference papers.
    // Switch to the full cohort so they are not hidden by the default crossover view.
    if (kind === 'venue-service' || kind === 'portfolio' || (kind === 'ml-status' && value === 'without')) {
      state.explorerCohort = 'all';
    } else {
      // These chart selections contain only researchers with main-conference papers.
      state.explorerCohort = 'crossovers';
    }
    state.chartDrilldown = { kind, value, label };
    renderFilters();
    renderPeople();
    // Bring the chart selection into view with the list directly below it.
    const target = $('#researcher-table-columns') || $('#explore');
    if (target) {
      const headerHeight = document.querySelector('.site-header')?.getBoundingClientRect().height || 0;
      const statusHeight = els.explorerFilterStatus?.getBoundingClientRect().height || 0;
      const selectionBox = els.chartSelectionBanner;
      const top = selectionBox && !selectionBox.hidden
        ? window.scrollY + selectionBox.getBoundingClientRect().top - headerHeight - 16
        : window.scrollY + target.getBoundingClientRect().top - headerHeight - statusHeight - 24;
      const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      target.focus({ preventScroll: true });
      window.scrollTo({ top: Math.max(0, top), behavior: reducedMotion ? 'auto' : 'smooth' });
    }
  }

  function displayContext(person) {
    const dbEvents = pcEventsFor(person);
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
      return copy.sort((a, b) => pcEventsFor(b).length - pcEventsFor(a).length || nameSort(a, b));
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

  function renderMethodologyDialog() {
    if (!els.methodologyDialogContent) return;
    const sources = provenance?.sources || {};
    const serviceSources = Object.entries(sources)
      .filter(([, source]) => source?.kind === 'official_service_page')
      .sort((a, b) => String(a[1]?.label || '').localeCompare(String(b[1]?.label || '')));
    const observedServiceYears = allPeople().flatMap(person => pcEventsFor(person).map(event => event.year))
      .map(Number).filter(year => Number.isInteger(year) && year > 0);
    const serviceYears = recordedYearRange(observedServiceYears);
    const servicePeriod = observedServiceYears.length ? ` in ${serviceYears}` : '';
    // These definitions follow the current charts, rather than older generated labels.
    const metrics = [
      {
        id: 'db_ml_researchers', page: 'shared', label: 'People and papers',
        paragraphs: [
          'Both pages follow the same senior DB researchers through their recorded publication history, including papers before committee service.',
          'Each person counts once in person totals. Each paper counts once in paper totals, even with multiple included researchers. Matched preprints and published versions count as one work.',
          'The share with a paper is the percentage of these members with at least one main-conference paper at NeurIPS, ICML or ICLR. Home rounds to a whole percent; All Insights shows one decimal place.',
        ],
      },
      {
        id: 'db_service_counts', label: 'Number of DB venues served',
        paragraphs: ['Counts distinct senior DB researchers by the number of venues where they served. Both service charts default to all recorded roles across all service years and have independent controls. In same-year mode, each person is grouped by their highest number of venues in any one year.'],
      },
      {
        id: 'db_service_overlap', label: 'Committee overlap matrix',
        paragraphs: ['Each cell counts people who served at both venues. Percentage divides that count by the row venue’s membership, so reversing the venues can change the percentage. Same-year mode requires a shared service year; the denominator remains the full row group for the selected period.'],
      },
      {
        id: 'crossover_matrix', label: 'Researchers by DB venue',
        paragraphs: [`For each DB venue, divide its senior DB researchers with a main-conference paper at NeurIPS, ICML or ICLR by all its senior DB researchers. PC membership uses ${serviceYears}; papers use their full recorded history. Unlike the service-overlap charts, other organizing roles do not count here.`],
      },
      {
        id: 'publication_activity', label: 'Papers over time',
        paragraphs: [
          'Each main-conference paper counts once, even with multiple included researchers. Annual shows one year. Cumulative includes papers through that year.',
          'Relative shows the percentage of main-conference papers involving these researchers, calculated separately for NeurIPS, ICML and ICLR over the same period.',
          'Missing conference totals mean no Relative percentage for that year. Cumulative Relative skips those years in both running counts and starts with the earliest available venue totals, even before the group’s first paper. Absolute still counts all recorded papers.',
        ],
      },
      {
        id: 'home_community_counts', page: 'home', label: 'The DB side and the ML side',
        paragraphs: ['The DB side counts qualifying PC members at each DB venue. The ML side counts members with a main-conference paper at each ML venue. A person can appear at several venues.'],
      },
      {
        id: 'home_publication_trend', page: 'home', label: 'The momentum',
        source: { label: 'Wikipedia: CAGR ↗', url: 'https://en.wikipedia.org/wiki/Compound_annual_growth_rate' },
        paragraphs: [
          'Home pools papers and conference totals for each year and the previous four years. Earlier history is used at the start; a missing total leaves a gap. The display starts 20 years before the current year and ends at the latest year with totals for all three venues.',
          'Compound annual growth rate (CAGR) compares each venue’s first nonzero annual share within that window with its final annual share. It uses annual shares, not the smoothed line, and cannot start from zero.',
        ],
      },
      {
        id: 'home_first_time_authors', page: 'home', label: 'Your move',
        paragraphs: [
          'Each researcher counts once, in the year of their first recorded main-conference paper across NeurIPS, ICML and ICLR.',
          'An exponential curve is fitted to up to ten years of annual counts by minimizing squared errors in counts. Grey bars show observed counts.',
        ],
      },
      {
        id: 'ml_participants', label: 'Researchers each year',
        paragraphs: ['Each senior DB researcher counts once per year with a main-conference paper at NeurIPS, ICML or ICLR. First time means their earliest recorded paper across these venues, not their first research paper. Published before means they had an earlier paper there. Changing displayed years does not reset first appearances.'],
      },
      {
        id: 'ml_portfolio', label: 'Publication share per researcher',
        paragraphs: ['For each researcher, divide their main-conference papers at NeurIPS, ICML or ICLR by all their recorded publications. For example, 10 of 200 publications = 5%. Other publication types remain in the denominator; matched versions of the same work count once.'],
      },
      {
        id: 'crossover_authorship', label: 'Author positions',
        paragraphs: ['Counts researcher–paper pairs: a paper with two included researchers contributes two authorships. Roles are assigned in order: Sole, First, Last, otherwise Middle. Records without a valid author position or total author count are left out. Author order does not necessarily measure contribution.'],
      },
    ];
    const build = provenance?.build || {};
    const buildInputs = provenance?.buildInputs || {};
    const inputFingerprint = String(buildInputs.inputFingerprint || '');
    const serviceSourcesRefreshedAt = String(buildInputs.serviceSourcesRefreshedAt || '');

    const sourceDescriptions = {
      dblp_snapshot: 'Researcher profiles and publication records.',
      crossref_affiliations: 'Author affiliations from publication metadata.',
      arxiv_metadata_cache: 'Helps verify matches between preprints and published papers.',
      ror_snapshot: 'Checks organization names for researcher resolution.',
    };
    const sourceTitles = {
      dblp_snapshot: 'DBLP publications',
      crossref_affiliations: 'Crossref',
      arxiv_metadata_cache: 'arXiv',
      ror_snapshot: 'ROR',
    };
    const sourceCard = (sourceId) => {
      const source = provenanceSource(sourceId);
      if (!source) return '';
      const title = sourceTitles[sourceId] || source.label;
      return `<article class="methodology-source-card">
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(sourceDescriptions[sourceId])}</p>
        ${source.release ? `<p><strong>Release</strong> ${escapeHtml(source.release)}</p>` : ''}
        ${source.refreshedAt ? `<p><strong>Last refreshed</strong> ${escapeHtml(formatSourceDate(source.refreshedAt))}</p>` : ''}
        ${source.url ? `<a href="${escapeAttr(source.url)}" target="_blank" rel="noreferrer"${sourceAnchorAttrs(source.url)}>Open source ↗</a>` : ''}
      </article>`;
    };

    const serviceSourceList = serviceSources.map(([sourceId]) => `<li>${sourceLinkMarkup(sourceId)}</li>`).join('');
    const metricCards = (page) => metrics.filter(metric => (metric.page || 'insights') === page).map((metric) => `
      <article class="methodology-metric" data-methodology-metric="${metric.id}">
        <h5>${escapeHtml(metric.label)}</h5>
        <div>${metric.paragraphs.map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`).join('')}${metric.source ? `<p class="methodology-metric-source"><a href="${escapeAttr(metric.source.url)}" target="_blank" rel="noreferrer">${escapeHtml(metric.source.label)}</a></p>` : ''}</div>
      </article>
    `).join('');

    els.methodologyDialogContent.innerHTML = `
      <header class="detail-header methodology-header">
        <p class="eyebrow">About the data</p>
        <h2>Methodology</h2>
      </header>

      <section class="methodology-section methodology-scope">
        <div class="methodology-section-heading"><h3>Who is included</h3></div>
        <p>${data?.populationFocus ? `We study senior DB researchers at SIGMOD/PODS, VLDB, ICDE, EDBT or CIDR${servicePeriod}, matched to DBLP profiles. Qualifying service includes research, industry and demo PCs, review boards, senior reviewing roles and program leadership. Shadow PCs and other organizing roles alone do not qualify.` : `We include researchers who organized or reviewed for SIGMOD/PODS, VLDB, ICDE, EDBT or CIDR${servicePeriod} and were matched to a DBLP profile.`}</p>
        ${data?.populationFocus ? '<p>Senior DB researchers are DB-focused: they have at least twice as many DB as ML conference papers. Evidence requires five qualifying papers across three publication years, or two DB papers in two publication years plus PC service in two distinct years. The comparison uses all recorded publication years.</p><p>The DB comparison uses selected main proceedings, including PVLDB and PACMMOD. DB papers need at least six pages; unpaginated CIDR papers also qualify. Workshops, companion proceedings, ordinary journals, known non-paper items and unverified paper lengths are excluded.</p><p>Home and All Insights use the DB-focused group. People Explorer’s All option also includes matched PC members who do not qualify.</p>' : ''}
        <p>ML totals count main-conference papers at NeurIPS, ICML or ICLR. Workshops are excluded for now; other publications remain in the per-researcher publication-share denominator.</p>
      </section>

      <section class="methodology-section">
        <div class="methodology-section-heading"><h3>Data sources</h3></div>
        <div class="methodology-source-grid">
          <article class="methodology-source-card">
            <h3>Official conference pages</h3>
            <p>Recorded committee and organizing roles. Membership does not confirm completed reviews.</p>
            ${serviceSourcesRefreshedAt ? `<p><strong>Last refreshed</strong> ${escapeHtml(formatSourceDate(serviceSourcesRefreshedAt))}</p>` : ''}
            ${serviceSources.length ? `<details><summary>Show source pages</summary><ul class="methodology-source-list">${serviceSourceList}</ul></details>` : ''}
          </article>
          ${sourceCard('dblp_snapshot')}
        </div>
        <p class="methodology-note">Dates use DBLP’s recorded publication years, including forthcoming records. A refresh date does not mean a publication year is complete.</p>
        <p class="methodology-note">Some identity matches, authorship links, affiliations and paper categories use project reviews, including AI-assisted source checks. The affected person or paper’s (i) panel labels the decision and links its evidence. These are project decisions, not claims made by the official sources.</p>
        <details class="methodology-supporting">
          <summary>Supporting sources: ROR, arXiv and Crossref</summary>
          <div class="methodology-source-grid">${sourceCard('ror_snapshot')}${sourceCard('arxiv_metadata_cache')}${sourceCard('crossref_affiliations')}</div>
          <p class="methodology-note">Affiliations use the newest usable publisher evidence, with conference and DBLP records as fallbacks. A paper’s affiliation may differ from current employment.</p>
        </details>
      </section>

      <section class="methodology-section">
        <div class="methodology-section-heading"><h3>How the charts are calculated</h3></div>
        ${[['shared', 'Shared counting rules'], ['home', 'Home'], ['insights', 'All Insights']].map(([page, label]) => `
          <section class="methodology-chart-group" data-methodology-page="${page}" aria-labelledby="methodology-${page}-title">
            <h4 id="methodology-${page}-title">${label}</h4>
            <div class="methodology-metrics">${metricCards(page)}</div>
          </section>
        `).join('')}
        <p class="methodology-note">People Explorer filters change its researcher list, not Home or All Insights. Each chart’s own controls apply to that chart.</p>
      </section>

      <details class="methodology-technical">
        <summary>Technical details</summary>
        <section class="methodology-section">
          <div class="methodology-section-heading"><h3>How records are matched</h3></div>
          <ol class="methodology-process">
            <li><strong>Find researchers</strong><span>Read organizer and reviewer names from official DB venue pages.</span></li>
            <li><strong>Match DBLP profiles</strong><span>Match names using affiliations and identity evidence where needed. Unresolved matches stay outside the analysis. Saved manual corrections are rechecked after refreshes.</span></li>
            <li><strong>Combine paper versions</strong><span>Collect their DBLP publications. Combine a preprint and its published version only when a DBLP link, shared DOI or strict title and author checks confirm the match.</span></li>
          </ol>
          <p class="methodology-note">Other publications are grouped using DBLP record types and a fixed list of manual venue-to-category assignments.</p>
        </section>
        <section class="methodology-section">
          <div class="methodology-section-heading"><h3>Reading the coverage Sankeys</h3></div>
          <p class="methodology-coverage-note">Each stage labels its counting unit. Merges combine repeated entries or paper versions. Percentage mode makes each stage total 100%; it does not show overall retention. Coverage starts at saved parser output, so names missed by the scrapers are not measured.</p>
        </section>
        <section class="methodology-section methodology-build">
          <div class="methodology-section-heading"><h3>Build details</h3></div>
          <dl>
            <div><dt>Data refreshed</dt><dd>${escapeHtml(formatBuildTimestamp(build.generatedAt || data?.meta?.generatedAt) || '—')}</dd></div>
            <div><dt>Data fingerprint</dt><dd>${inputFingerprint ? `<code>${escapeHtml(inputFingerprint)}</code>` : 'Unavailable in this build'}</dd></div>
          </dl>
        </section>
      </details>
    `;
  }

  function openMethodology(metricId = '') {
    if (!els.methodologyDialog || !els.methodologyDialogContent) return;
    renderMethodologyDialog();
    if (typeof els.methodologyDialog.showModal === 'function') els.methodologyDialog.showModal();
    else els.methodologyDialog.setAttribute('open', '');
    els.methodologyDialog.scrollTop = 0;
    if (metricId) {
      const metricAliases = { core_ml_papers: 'publication_activity', venue_core_ml_researchers: 'db_ml_researchers' };
      const targetId = metricAliases[metricId] || metricId;
      const target = metricId === 'derived-results'
        ? els.methodologyDialogContent.querySelector('.methodology-metrics')
        : els.methodologyDialogContent.querySelector(`[data-methodology-metric="${CSS.escape(targetId)}"]`);
      if (target) {
        if (target.classList.contains('methodology-metric')) target.classList.add('is-targeted');
        target.scrollIntoView({ block: 'center' });
      }
    }
  }

  function formatSourceDate(value) {
    const date = new Date(value || '');
    if (!Number.isFinite(date.getTime())) return '';
    return `${new Intl.DateTimeFormat('en-GB', {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZone: 'UTC',
    }).format(date)} UTC`;
  }

  function formatBuildTimestamp(value) {
    const date = new Date(value || '');
    if (!Number.isFinite(date.getTime())) return '';
    return `${new Intl.DateTimeFormat('en-GB', {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZone: 'UTC',
    }).format(date)} UTC`;
  }

  function renderFooterRefresh() {
    if (!els.footerDataRefresh) return;
    const generatedAt = provenance?.build?.generatedAt || data?.meta?.generatedAt || '';
    const date = new Date(generatedAt);
    const formatted = Number.isFinite(date.getTime())
      ? new Intl.DateTimeFormat('en-GB', {
        day: 'numeric',
        month: 'long',
        year: 'numeric',
        timeZone: 'UTC',
      }).format(date)
      : '';
    els.footerDataRefresh.textContent = formatted ? `Data refreshed ${formatted}` : 'Refresh date unavailable';
    els.footerDataRefresh.title = 'When the website data was last refreshed';
  }

  let overviewResizeObserver = null;

  function overviewTextInkBounds(element, context) {
    const textNode = [...element.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    if (!textNode) return element.getBoundingClientRect();
    const style = getComputedStyle(element);
    context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    const text = textNode.textContent;
    // The last line's descent matters when the note wraps. Earlier lines may
    // contain descenders even when the final line ends with only year digits.
    const range = document.createRange();
    range.setStart(textNode, text.length - 1);
    range.setEnd(textNode, text.length);
    const lastLineTop = range.getBoundingClientRect().top;
    let start = text.length - 1;
    while (start > 0) {
      range.setStart(textNode, start - 1);
      range.setEnd(textNode, start);
      if (range.getBoundingClientRect().top < lastLineTop - 1) break;
      start -= 1;
    }
    const line = text.slice(start);
    const font = context.font;
    const textWidth = context.measureText(line).width;
    const baselineMarker = document.createElement('span');
    baselineMarker.setAttribute('aria-hidden', 'true');
    baselineMarker.style.cssText = 'display:inline-block;width:0;height:0;padding:0;margin:0;border:0;vertical-align:baseline;';
    element.append(baselineMarker);
    const baseline = baselineMarker.getBoundingClientRect().top;
    baselineMarker.remove();
    // Measure painted pixels rather than font metrics, which can leave a
    // visible gap at the baseline on some fonts and display scales.
    const scale = window.devicePixelRatio || 1;
    const padding = Math.ceil(parseFloat(style.fontSize) * scale * 2);
    const origin = Math.floor(baseline * scale) - padding;
    context.canvas.width = Math.ceil(textWidth * scale) + 2 * padding;
    context.canvas.height = 2 * padding + Math.ceil(parseFloat(style.fontSize) * scale);
    context.scale(scale, scale);
    context.font = font;
    context.fillText(line, padding / scale, (baseline * scale - origin) / scale);
    const { width, height } = context.canvas;
    const pixels = context.getImageData(0, 0, width, height).data;
    let first = height;
    let last = -1;
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        if (pixels[(y * width + x) * 4 + 3] >= 64) {
          first = Math.min(first, y);
          last = y;
          break;
        }
      }
    }
    if (last < 0) return element.getBoundingClientRect();
    return { top: (origin + first) / scale, bottom: (origin + last + 1) / scale };
  }

  function alignOverviewDonut() {
    if (window.matchMedia('(max-width: 900px)').matches) return;
    const body = els.summary?.querySelector('.viz-cohort-body');
    const total = body?.querySelector('.viz-cohort-total strong');
    const note = body?.querySelector('.viz-overview-note');
    if (!body || !total || !note) return;
    const context = document.createElement('canvas').getContext('2d');
    if (!context) return;
    const bodyBounds = body.getBoundingClientRect();
    const top = Math.max(0, overviewTextInkBounds(total, context).top - bodyBounds.top);
    const bottom = Math.max(0, bodyBounds.bottom - overviewTextInkBounds(note, context).bottom);
    els.summary.style.setProperty('--overview-ink-top', `${top}px`);
    els.summary.style.setProperty('--overview-ink-bottom', `${bottom}px`);
  }

  function insightsInfoButton(metricId, label) {
    return `<button class="info-icon-button insights-info" type="button" data-provenance-metric="${escapeAttr(metricId)}" aria-expanded="false" aria-label="${escapeAttr(label)}"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 10.5v6M12 7.3v.3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button>`;
  }

  function renderInsightsInfo() {
    if (!document.body.classList.contains('insights-page')) return;
    const explanations = [
      ['#summary-title', 'db_focus', 'Who counts as a senior DB researcher?'],
      ['#overlap-matrix-title', 'db_service_overlap', 'How committee overlap is calculated'],
      ['#matrix h2', 'db_venue_participation', 'How the share of members with a paper is calculated'],
      ['#publication-timeline h2', 'publication_activity', 'How publication activity is counted'],
      ['#ml-participants h2', 'ml_participants', 'What First time and Published before mean'],
      ['#ml-authorship h2', 'crossover_authorship', 'How author positions are counted'],
    ];
    explanations.forEach(([selector, id, label]) => {
      document.querySelector(selector)?.insertAdjacentHTML('beforeend', insightsInfoButton(id, label));
    });
    document.body.classList.add('insights-ready');
  }

  function recordedYearRange(values) {
    const years = [...new Set(values.map(Number).filter(year => Number.isInteger(year) && year > 0))];
    if (!years.length) return 'No recorded years';
    const first = Math.min(...years), last = Math.max(...years);
    return first === last ? String(first) : `${first}–${last}`;
  }

  function insightScope(sectionId, text) {
    const label = document.querySelector(`#${sectionId} [data-insights-scope]`);
    if (label) label.textContent = text;
  }

  function insightHistoryLabels() {
    const people = allPeople();
    const service = recordedYearRange(people.flatMap(person => pcEventsFor(person).map(event => event.year)));
    const main = recordedYearRange(people.flatMap(person => mainPapersFor(person).map(paper => paper.year)));
    const publicationYears = people.flatMap(person => list(person.ml?.allPublicationYears));
    const publications = recordedYearRange(publicationYears);
    const snapshotYear = Number(provenance?.sources?.dblp_snapshot?.release?.match(/(\d{4})-\d{2}-\d{2}/)?.[1]);
    const forthcoming = snapshotYear && publicationYears.some(year => Number(year) > snapshotYear);
    return { service, main, publications, forthcoming };
  }

  function renderInsightScopes() {
    if (!document.body.classList.contains('insights-page')) return;
    const history = insightHistoryLabels();
    insightScope('population-focus', `PC service · ${history.service} · All recorded publication years`);
    insightScope('matrix', `PC service · ${history.service} · Main-conference papers · ${history.main}`);
    insightScope('ml-portfolio', `DBLP publication records · ${history.publications}${history.forthcoming ? ' · Includes forthcoming records' : ''}`);
  }

  function renderSummary() {
    if (!els.summary) return;
    const people = allPeople();
    const crossoverCount = people.filter(hasMain).length;
    const crossoverShare = people.length ? 100 * crossoverCount / people.length : 0;
    // Transparent hit areas match the visible donut ring (outer radius 50,
    // inner radius 27) without changing its existing gradient presentation.
    const donutCircumference = 2 * Math.PI * 38.5;
    const donutWithArc = (donutCircumference * crossoverShare / 100).toFixed(4);
    const donutWithoutArc = (donutCircumference * (100 - crossoverShare) / 100).toFixed(4);
    const dbVenues = matrixStats?.dbVenues || uniqueSorted(people.flatMap((person) => list(person.db?.venues)));
    const venueRows = dbVenues.map((venue) => ({
      venue,
      count: people.filter((person) => pcEventsFor(person).some((event) => event.venue === venue)).length,
    })).sort((a, b) => b.count - a.count || a.venue.localeCompare(b.venue));
    const largest = Math.max(1, ...venueRows.map((row) => row.count));
    const history = insightHistoryLabels();
    const mainYears = people.flatMap(person => mainPapersFor(person).map(paper => Number(paper.year))).filter(Number.isInteger);
    const coverage = mainYears.length ? latestCoverageNote(Math.max(...mainYears)) : '';
    const bars = venueRows.map(({ venue, count }) => `
      <button class="viz-cohort-row viz-chart-drilldown" type="button" data-service-drilldown="${escapeAttr(venue)}" aria-label="Show ${fmt.format(count)} senior DB researchers at ${escapeAttr(dbVenueLabel(venue))} in the People Explorer">
        <span class="viz-cohort-venue">${escapeHtml(dbVenueLabel(venue))}</span>
        <span class="viz-cohort-track" aria-hidden="true"><i style="width:${100 * count / largest}%"></i></span>
        <strong>${fmt.format(count)}</strong>
      </button>`).join('');

    els.summary.innerHTML = `
      <div class="viz-overview-panel">
        <div class="viz-overview-heading"><h3>Senior DB researchers by DB venue</h3><p class="insights-data-label">PC service · ${escapeHtml(history.service)}</p></div>
        <div class="viz-cohort-body">
          <div class="viz-cohort-total"><strong>${fmt.format(people.length)}</strong><span>unique PC members</span></div>
          <div class="viz-cohort-bars" role="group" aria-label="Researchers with recorded service at each DB venue">${bars}</div>
          <p class="viz-overview-note">People may appear at more than one venue.</p>
        </div>
      </div>
      <div class="viz-overview-panel viz-overview-crossover">
        <div class="viz-overview-heading"><h3>Ratio of senior DB researchers with a main-conference paper at NeurIPS, ICML or ICLR</h3><p class="insights-data-label">Main-conference papers · ${escapeHtml(history.main)}${coverage ? ` · ${escapeHtml(coverage.replace(' coverage:', ':'))}` : ''}</p></div>
        <div class="viz-donut-layout">
          <div class="viz-donut" style="--viz-donut-share:${crossoverShare}%">
            <svg class="viz-donut-hit-areas" viewBox="0 0 100 100" role="group" aria-label="Select a donut segment to view its DB researchers">
              <circle class="viz-donut-hit-segment" cx="50" cy="50" r="38.5" fill="none" stroke="transparent" stroke-width="23" stroke-dasharray="${donutWithoutArc} ${donutWithArc}" stroke-dashoffset="-${donutWithArc}" transform="rotate(-90 50 50)" role="button" tabindex="${people.length - crossoverCount ? '0' : '-1'}" data-overview-ml="without" aria-label="Show ${fmt.format(people.length - crossoverCount)} senior DB researchers without main-conference papers in the People Explorer" />
              <circle class="viz-donut-hit-segment" cx="50" cy="50" r="38.5" fill="none" stroke="transparent" stroke-width="23" stroke-dasharray="${donutWithArc} ${donutWithoutArc}" transform="rotate(-90 50 50)" role="button" tabindex="${crossoverCount ? '0' : '-1'}" data-overview-ml="with" aria-label="Show ${fmt.format(crossoverCount)} crossover researchers with main-conference papers in the People Explorer" />
            </svg>
            <div class="viz-donut-center"><strong>${crossoverShare.toFixed(1)}%</strong><span>of the DB cohort</span></div>
          </div>
          <div class="viz-donut-legend" role="group" aria-label="Inspect senior DB researchers by main-conference participation">
            <button class="viz-donut-choice viz-chart-drilldown" type="button" data-overview-ml="with" aria-label="Show ${fmt.format(crossoverCount)} crossover researchers with main-conference papers in the People Explorer"><i class="viz-swatch viz-swatch-active" aria-hidden="true"></i><strong>${fmt.format(crossoverCount)}</strong><span>with at least one paper</span></button>
            <button class="viz-donut-choice viz-chart-drilldown" type="button" data-overview-ml="without" aria-label="Show ${fmt.format(people.length - crossoverCount)} senior DB researchers without main-conference papers in the People Explorer"><i class="viz-swatch viz-swatch-inactive" aria-hidden="true"></i><strong>${fmt.format(people.length - crossoverCount)}</strong><span>without any paper</span></button>
          </div>
        </div>
      </div>`;
    els.summary.setAttribute('aria-busy', 'false');
    els.summary.querySelectorAll('.viz-cohort-total strong, .viz-cohort-track, .viz-donut, .viz-donut-legend strong')
      .forEach(plot => plot.classList.add('loading-content-ready'));
    overviewResizeObserver?.disconnect();
    if (typeof ResizeObserver !== 'undefined') {
      overviewResizeObserver = new ResizeObserver(alignOverviewDonut);
      overviewResizeObserver.observe(els.summary.querySelector('.viz-cohort-body'));
    }
    alignOverviewDonut();
    document.fonts?.ready.then(alignOverviewDonut);
    els.dataStatus.textContent = '';
    els.summary.querySelectorAll('[data-service-drilldown]').forEach((button) => {
      button.addEventListener('click', () => {
        const venue = button.dataset.serviceDrilldown;
        selectResearchersFromChart('venue-service', venue, `${dbVenueLabel(venue)} PC roster`);
      });
    });
    els.summary.querySelectorAll('[data-overview-ml]').forEach((button) => {
      const activateSelection = () => {
        const withMain = button.dataset.overviewMl === 'with';
        selectResearchersFromChart('ml-status', withMain ? 'with' : 'without',
          withMain ? 'senior DB researchers with main-conference papers' : 'senior DB researchers without main-conference papers');
      };
      button.addEventListener('click', activateSelection);
      // SVG donut segments need explicit Enter/Space activation; native
      // buttons in the legend already provide that behavior.
      if (button instanceof SVGElement) {
        button.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            activateSelection();
          }
        });
      }
    });
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
    if (!els.body) return;
    const people = explorerScopePeople();
    populateSelect(els.dbVenue, uniqueSorted(people.flatMap((person) => pcEventsFor(person).map(event => event.venue))), dbVenueLabel);
    populateSelect(els.mlVenue, mainVenueOrder.filter((venue) => people.some((person) => mainPapersFor(person).some((paper) => mainVenueLabel(paper) === venue))));

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

  function renderDbVenueRates() {
    if (!els.dbVenueRates) return;
    const people = allPeople();
    const dbVenues = matrixStats?.dbVenues || uniqueSorted(people.flatMap((person) => list(person.db?.venues)));
    const rows = dbVenues.map((venue) => {
      const venuePeople = people.filter((person) => pcEventsFor(person).some((event) => event.venue === venue));
      const participating = venuePeople.filter(hasMain).length;
      return { venue, total: venuePeople.length, participating, share: venuePeople.length ? 100 * participating / venuePeople.length : 0 };
    }).sort((a, b) => b.total - a.total || a.venue.localeCompare(b.venue));
    const isPercentage = state.dbVenueRatesView === 'percentage';
    const maximum = Math.max(1, ...rows.map((row) => row.participating));
    document.querySelectorAll('[data-db-rates-view]').forEach((button) => {
      const selected = button.dataset.dbRatesView === state.dbVenueRatesView;
      button.classList.toggle('is-active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    const note = 'A researcher may have served on PCs at multiple venues.';
    els.dbVenueRates.innerHTML = rows.length ? rows.map((row) => {
      const barWidth = isPercentage ? row.share : 100 * row.participating / maximum;
      const description = `${fmt.format(row.participating)} of ${fmt.format(row.total)} senior DB researchers at ${dbVenueLabel(row.venue)} have a main-conference paper at NeurIPS, ICML or ICLR.`;
      const value = isPercentage ? `${row.share.toFixed(1)}%`
        : `<span class="viz-rate-numerator">${fmt.format(row.participating)}</span><span class="viz-rate-denominator">/${fmt.format(row.total)}</span>`;
      return `
      <button class="viz-rate-row viz-chart-drilldown" type="button" data-venue-ml-drilldown="${escapeAttr(row.venue)}" aria-label="${escapeAttr(description)} Show these researchers in the People Explorer.">
        <span class="viz-rate-head"><strong>${escapeHtml(dbVenueLabel(row.venue))}</strong></span>
        <span class="viz-rate-track" aria-hidden="true"><span style="width:${barWidth}%"></span></span>
        <strong class="viz-rate-value${isPercentage ? '' : ' is-fraction'}" title="${escapeAttr(description)}">${value}</strong>
      </button>`;
    }).join('') + `<p class="viz-chart-note">${note}</p>`
      : '<p class="viz-chart-note">No resolved senior DB researchers found.</p>';
    els.dbVenueRates.querySelectorAll('[data-venue-ml-drilldown]').forEach((button) => {
      button.addEventListener('click', () => {
        const venue = button.dataset.venueMlDrilldown;
        selectResearchersFromChart('venue-ml', venue, `${dbVenueLabel(venue)} PC members with main-conference papers`);
      });
    });
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
        if (!map.has(key)) map.set(key, { paper: { ...paper, provenance: { ...paper.provenance, projectReviews: [] } }, people: new Set() });
        map.get(key).paper.provenance.projectReviews.push(...list(paper?.provenance?.projectReviews));
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
    const venues = mainVenueOrder.map((venue) => {
      const on = state.timelineVisible[venue] !== false;
      const swatchClass = venueClass(venue);
      return `<button class="legend-item legend-toggle${on ? '' : ' is-off'}" type="button" data-timeline-venue="${escapeAttr(venue)}" aria-pressed="${on}"><i class="legend-swatch ${swatchClass}"></i>${escapeHtml(venue)}</button>`;
    }).join('');
    const scaleControls = `<div class="publication-scale-controls"><span class="viz-view-switch" role="group" aria-label="Publication measurement">
      <button type="button" data-publication-scale="absolute" aria-pressed="${state.publicationScale === 'absolute'}" class="${state.publicationScale === 'absolute' ? 'is-active' : ''}">Absolute</button>
      <span class="publication-relative-control"><button type="button" data-publication-scale="relative" aria-pressed="${state.publicationScale === 'relative'}" class="${state.publicationScale === 'relative' ? 'is-active' : ''}">Relative</button><button class="info-icon-button publication-relative-info" type="button" data-provenance-metric="publication_relative" aria-expanded="false" aria-label="What does Relative mean?"><svg viewBox="0 0 12 12" aria-hidden="true" focusable="false"><circle cx="6" cy="6" r="5.5" fill="none" stroke="currentColor" stroke-width="1"/><circle cx="6" cy="3.1" r="0.7" fill="currentColor"/><path d="M6 5.4v3.6" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button></span>
    </span></div>`;
    const viewControls = `<span class="viz-view-switch" role="group" aria-label="Main-conference publication time view">
      <button type="button" data-publication-view="annual" aria-pressed="${state.publicationView === 'annual'}" class="${state.publicationView === 'annual' ? 'is-active' : ''}">Annual</button>
      <button type="button" data-publication-view="cumulative" aria-pressed="${state.publicationView === 'cumulative'}" class="${state.publicationView === 'cumulative' ? 'is-active' : ''}">Cumulative</button>
    </span>`;
    els.timelineLegend.innerHTML = `<div class="timeline-venue-controls">${venues}</div><div class="publication-display-controls">${scaleControls}${viewControls}</div>`;
  }

  // Responsive lines of actual annual/running observations, without smoothing.
  function publicationLineChartMarkup(timeline, { cumulative = false, relative = false } = {}) {
    const venues = mainVenueOrder.filter(venue => state.timelineVisible[venue] !== false);
    const years = timeline.years;
    const valueKey = cumulative ? (relative ? 'cumulativeShare' : 'cumulative') : (relative ? 'annualShare' : 'annual');
    const width = Math.max(240, Math.round(els.globalTimeline.clientWidth || 860));
    const height = width < 600 ? 260 : 300;
    const left = 52, right = 18, top = 20, bottom = 30;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const hitRadius = Math.min(9, plotWidth / Math.max(1, years.length - 1) / 2);
    const series = venues.map(venue => ({ venue, points: years.map(year => {
      const point = timeline.byVenue.get(venue).get(year);
      // Do not turn an uncovered venue-year into an observed zero. Absolute
      // still shows any recorded papers even if its venue total is unavailable.
      const covered = point.denominator > 0 || point.annual > 0;
      return { ...point, year, value: relative || covered ? point[valueKey] : null };
    }) }));
    const upper = publicationAxisMaximum(series.flatMap(line => line.points.map(point => point.value)), relative);
    const x = index => left + (years.length === 1 ? plotWidth / 2 : index * plotWidth / (years.length - 1));
    const y = value => top + (1 - value / upper) * plotHeight;
    const grid = [0, 1, 2, 3, 4].map(tick => {
      const value = upper * tick / 4;
      return `<line class="viz-svg-grid" x1="${left}" y1="${y(value)}" x2="${width - right}" y2="${y(value)}" /><text class="viz-svg-axis" text-anchor="end" x="${left - 10}" y="${y(value) + 4}">${escapeHtml(publicationValueLabel(value, relative))}</text>`;
    }).join('');
    const spacing = 58;
    const labelIndices = [0];
    for (let index = 1; index < years.length - 1; index++) {
      if (x(index) - x(labelIndices.at(-1)) >= spacing && x(years.length - 1) - x(index) >= spacing) labelIndices.push(index);
    }
    if (years.length > 1) labelIndices.push(years.length - 1);
    const xLabels = labelIndices.map(index => `<text class="viz-svg-axis" text-anchor="middle" x="${x(index)}" y="${height - 8}">${years[index]}</text>`).join('');
    const paths = [], points = [];
    series.forEach(line => {
      const color = `var(--ml-${line.venue.toLowerCase()})`;
      let part = [];
      const finishPart = () => {
        if (part.length > 1) paths.push(`<polyline class="viz-svg-series" data-publication-line="${line.venue}" points="${part.join(' ')}" style="stroke:${color}" aria-hidden="true" />`);
        part = [];
      };
      line.points.forEach((point, index) => {
        if (!Number.isFinite(point.value)) { finishPart(); return; }
        part.push(`${x(index).toFixed(2)},${y(point.value).toFixed(2)}`);
        const count = cumulative ? point.cumulative : point.annual;
        const numerator = cumulative && relative ? point.cumulativeCovered : count;
        const denominator = cumulative ? point.cumulativeDenominator : point.denominator;
        const period = cumulative ? ` through ${point.year}` : '';
        const detail = relative
          ? `${publicationValueLabel(point.value, true)} of conference papers${period}\n${fmt.format(numerator)} of ${fmt.format(denominator)} papers`
          : `${fmt.format(count)} main-conference papers${period}`;
        const action = numerator > 0 ? 'Select to inspect papers.' : 'No papers to inspect.';
        points.push(`<g class="publication-line-point"
          data-publication-year="${point.year}" data-publication-venue="${line.venue}"
          data-publication-count="${numerator}" data-publication-total="${denominator}" data-publication-value="${point.value}"
          data-provenance-metric="publication_point" data-point-label="${line.venue} · ${point.year}" data-point-detail="${escapeAttr(detail)}"
          tabindex="0" role="button" aria-disabled="${numerator <= 0}" aria-expanded="false"
          aria-label="${escapeAttr(`${line.venue} ${point.year}: ${detail}. ${action}`)}"
          style="--point-color:${color}" transform="translate(${x(index).toFixed(2)} ${y(point.value).toFixed(2)})">
          <circle class="publication-line-hit" r="${hitRadius}" /><circle class="publication-line-dot" r="3" /></g>`);
      });
      finishPart();
    });
    const axisTitle = relative
      ? (cumulative ? 'Share of venue’s cumulative papers (%)' : 'Share of venue’s papers (%)')
      : (cumulative ? 'Cumulative number of papers' : 'Number of papers');
    return `<div class="publication-chart-panel publication-line-panel" data-publication-chart="${cumulative ? 'cumulative' : 'annual'}"><p class="chart-axis-label chart-axis-y">${axisTitle}</p><svg class="publication-line-chart" viewBox="0 0 ${width} ${height}" role="group" aria-label="${escapeAttr(`${cumulative ? 'Cumulative' : 'Annual'} main-conference ${relative ? 'paper shares' : 'paper counts'} at ${selectionListLabel(venues)}`)}">${grid}${xLabels}${paths.join('')}${points.join('')}</svg><p class="chart-axis-label chart-axis-x">Year</p></div>`;
  }

  function timelineColumnTooltip(year, yearEntries, mode = 'main', options = {}) {
    const total = yearEntries.length;
    const normalized = mode === 'main' && options.normalized === true;
    const visibility = options.visibility || state.timelineVisible;
    const publishedTotal = normalized ? visibleMainVenuePublishedTotal(year, visibility) : 0;
    const share = publishedTotal > 0 ? total / publishedTotal : 0;
    const lines = [normalized && publishedTotal > 0
      ? `${year} · ${total} of ${fmt.format(publishedTotal)} published papers · ${shareFmt.format(share)}`
      : `${year} · ${total} ${total === 1 ? 'paper' : 'papers'}`];
    if (mode === 'main') {
      mainVenueOrder.forEach((venue) => {
        const count = yearEntries.filter((entry) => paperScope(entry.paper) === 'main' && mainVenueLabel(entry.paper) === venue).length;
        if (!count) return;
        if (normalized) {
          const venueTotal = mainVenuePublishedTotal(venue, year);
          lines.push(venueTotal > 0
            ? `${venue}: ${count} / ${fmt.format(venueTotal)} (${shareFmt.format(count / venueTotal)})`
            : `${venue}: ${count}`);
        } else {
          lines.push(`${venue}: ${count}`);
        }
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
    const serviceYears = mode === 'main' && options.serviceYears instanceof Map ? options.serviceYears : null;
    const visibility = options.visibility || (mode === 'main' ? state.timelineVisible : dialogContextVisibility);
    const highlightExplorer = options.highlightExplorer !== false;
    const normalized = mode === 'main' && options.normalized === true;
    const filteredEntries = entries.filter((entry) => {
      if (mode === 'main') {
        if (paperScope(entry.paper) !== 'main') return false;
        return visibility[mainVenueLabel(entry.paper)] !== false;
      }
      return paperScope(entry.paper) === 'side' && visibility[publicationClass(entry.paper)] !== false;
    });
    const groups = timelineGroups(filteredEntries);
    const dataYears = [...groups.keys(), ...(serviceYears ? [...serviceYears.keys()] : [])].sort((a, b) => a - b);
    const minYear = Number.isFinite(Number(options.minYear)) ? Number(options.minYear) : (dataYears.length ? Math.min(...dataYears) : null);
    const maxYear = Number.isFinite(Number(options.maxYear)) ? Number(options.maxYear) : (dataYears.length ? Math.max(...dataYears) : null);
    if (minYear === null || maxYear === null) return `<p class="empty-state">No ${mode === 'main' ? 'main-conference' : 'other'} publication activity.</p>`;
    const years = Array.from({ length: maxYear - minYear + 1 }, (_, index) => minYear + index);
    const metricByYear = new Map(years.map((year) => {
      const total = (groups.get(year) || []).length;
      if (!normalized) return [year, total];
      const publishedTotal = visibleMainVenuePublishedTotal(year, visibility);
      return [year, publishedTotal > 0 ? total / publishedTotal : 0];
    }));
    const maxMetric = Math.max(...metricByYear.values(), Number.EPSILON);

    return `<div class="timeline-scroll"><div class="timeline-chart" style="--timeline-years:${years.length}">${years.map((year) => {
      const yearEntries = groups.get(year) || [];
      const total = yearEntries.length;
      const metric = metricByYear.get(year) || 0;
      const barHeight = linearBarHeight(metric, maxMetric, 162);
      const segments = [];

      if (mode === 'main') {
        mainVenueOrder.forEach((venue) => {
          const matching = yearEntries.filter((entry) => paperScope(entry.paper) === 'main' && mainVenueLabel(entry.paper) === venue);
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
      const serviceCount = serviceYears?.get(year) || 0;
      const tooltip = timelineColumnTooltip(year, yearEntries, mode, { normalized, visibility })
        + (serviceCount ? `\n${serviceCount} observed DB-service ${serviceCount === 1 ? 'event' : 'events'}` : '');
      const label = normalized && total ? shareLabelFmt.format(metric) : (total ? fmt.format(total) : '');
      return `
        <button class="timeline-year" type="button" data-timeline-year="${year}" data-timeline-mode="${mode}" ${total ? '' : 'disabled'} aria-label="${escapeAttr(tooltip.replaceAll('\n', ', '))}" title="${escapeAttr(tooltip)}">
          <span class="timeline-count-label">${label}</span>
          <span class="timeline-bar-area"><span class="timeline-stack" style="height:${barHeight}px">${segments.join('')}</span></span>
          ${serviceYears ? `<span class="timeline-service-year" title="${serviceCount} recorded DB-service ${serviceCount === 1 ? 'event' : 'events'} in ${year}">${serviceCount ? '<i aria-hidden="true"></i>' : ''}</span>` : ''}
          <span class="timeline-year-label${major ? ' is-major' : ''}">${year}</span>
        </button>`;
    }).join('')}</div></div>`;
  }

  // Grouped columns deliberately keep the same layout for both charts and both
  // measurement modes. Each conference has its own denominator in relative mode.
  function publicationAxisMaximum(values, relative) {
    const maximum = Math.max(0, ...values.filter(Number.isFinite));
    if (maximum === 0) return relative ? 1 : 4;
    const magnitude = Math.pow(10, Math.floor(Math.log10(maximum / 4)));
    return 4 * Math.ceil(maximum / (4 * magnitude)) * magnitude;
  }

  function publicationValueLabel(value, relative) {
    if (!relative) return fmt.format(Math.round(value));
    return `${value < 1 ? value.toFixed(2) : value.toFixed(1)}%`;
  }

  function publicationTimelineData(entries) {
    const yearsWithPapers = entries.map((entry) => Number(entry.paper.year)).filter(Number.isFinite);
    if (!yearsWithPapers.length) return null;
    const first = Math.min(...yearsWithPapers), last = Math.max(...yearsWithPapers);
    const years = Array.from({ length: last - first + 1 }, (_, index) => first + index);
    const counts = new Map();
    entries.forEach((entry) => {
      const year = Number(entry.paper.year), venue = mainVenueLabel(entry.paper);
      if (!Number.isFinite(year) || !mainVenueOrder.includes(venue)) return;
      const key = `${venue}:${year}`;
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    const byVenue = new Map();
    mainVenueOrder.forEach((venue) => {
      const coveredYears = Object.keys(data?.mainMlVenueYearTotals?.[venue] || {})
        .map(Number).filter((year) => Number.isFinite(year) && mainVenuePublishedTotal(venue, year) > 0);
      const coverageStart = coveredYears.length ? Math.min(...coveredYears) : first;
      const observations = new Map();
      let cumulative = 0, cumulativeCovered = 0, cumulativeDenominator = 0;
      // Include venue-wide conference totals before the first tracked DB paper.
      // Thus cumulative shares do not spuriously start at 100% in the first year.
      for (let year = Math.min(first, coverageStart); year <= last; year += 1) {
        const annual = counts.get(`${venue}:${year}`) || 0;
        const denominator = mainVenuePublishedTotal(venue, year);
        cumulative += annual;
        if (denominator > 0) {
          cumulativeCovered += annual;
          cumulativeDenominator += denominator;
        }
        if (year < first) continue;
        observations.set(year, {
          annual, cumulative, denominator, cumulativeCovered, cumulativeDenominator,
          annualShare: denominator > 0 ? 100 * annual / denominator : null,
          cumulativeShare: denominator > 0 && cumulativeDenominator > 0
            ? 100 * cumulativeCovered / cumulativeDenominator : null,
        });
      }
      byVenue.set(venue, observations);
    });
    return { years, byVenue };
  }

  function groupedPublicationChartMarkup(timeline, { cumulative = false, relative = false, visibility = state.timelineVisible, person = false } = {}) {
    const visibleVenues = mainVenueOrder.filter((venue) => visibility[venue] !== false);
    const valueKey = cumulative
      ? (relative ? 'cumulativeShare' : 'cumulative')
      : (relative ? 'annualShare' : 'annual');
    const values = visibleVenues.flatMap((venue) => timeline.years.map((year) => timeline.byVenue.get(venue).get(year)[valueKey]));
    const axisMaximum = publicationAxisMaximum(values, relative);
    const title = person
      ? `${cumulative ? 'Cumulative' : 'Annual'} papers at ${selectionListLabel(visibleVenues)}`
      : cumulative
        ? (relative ? 'What share of main-conference papers through each year involved DB researchers?'
          : 'How many main-conference papers had accumulated by each year?')
        : (relative ? 'What share of main-conference papers involved DB researchers each year?'
          : 'How many main-conference papers were published each year?');
    const axisLabels = [0, 1, 2, 3, 4].map((tick) => {
      const value = axisMaximum * tick / 4;
      return `<span class="publication-axis-tick" style="bottom:${25 * tick}%">${escapeHtml(publicationValueLabel(value, relative))}</span>`;
    }).join('');
    const yearGroups = timeline.years.map((year) => {
      const yearPoints = visibleVenues.map((venue) => ({ venue, point: timeline.byVenue.get(venue).get(year) }));
      // Count each paper once across the currently visible main-ML venues.
      // Relative percentages have different venue denominators, so no total is shown there.
      const yearTotal = yearPoints.reduce((sum, { point }) => sum + (cumulative ? point.cumulative : point.annual), 0);
      const totalLabel = relative ? '&nbsp;' : fmt.format(yearTotal);
      const bars = yearPoints.map(({ venue, point }) => {
        const value = point[valueKey];
        const available = value !== null && Number.isFinite(value);
        const height = available && axisMaximum > 0 ? Math.max(0, Math.min(100, 100 * value / axisMaximum)) : 0;
        const scope = cumulative ? `through ${year}` : `in ${year}`;
        const count = cumulative ? point.cumulative : point.annual;
        const comparisonCount = cumulative ? point.cumulativeCovered : point.annual;
        const comparisonTotal = cumulative ? point.cumulativeDenominator : point.denominator;
        const valueText = available ? publicationValueLabel(value, relative) : 'unavailable (no conference-wide total)';
        const tooltip = relative
          ? `${venue} ${scope}: ${valueText}${available ? `; ${fmt.format(comparisonCount)} of ${fmt.format(comparisonTotal)} conference papers` : ''}`
          : `${venue} ${scope}: ${fmt.format(count)} unique papers`;
        const selectableCount = cumulative && relative ? point.cumulativeCovered : count;
        const yearAction = selectableCount > 0
          ? `Select to inspect ${venue} papers published ${cumulative ? 'through' : 'in'} ${year}.`
          : `No ${venue} papers published ${cumulative ? 'through' : 'in'} ${year} to inspect.`;
        const bar = available
          ? `<span class="publication-column-fill ${venueClass(venue)}" style="height:${height.toFixed(3)}%" aria-hidden="true"></span>`
          : '<span class="publication-column-unavailable" aria-hidden="true">–</span>';
        const shared = `class="publication-column${available ? '' : ' is-unavailable'}" title="${escapeAttr(tooltip)}" aria-label="${escapeAttr(`${tooltip}. ${yearAction}`)}"`;
        return selectableCount > 0 && available
          ? `<button type="button" ${shared} ${person ? 'data-person-publication-year' : 'data-publication-year'}="${year}" data-publication-venue="${escapeAttr(venue)}">${bar}</button>`
          : `<span ${shared} role="img">${bar}</span>`;
      }).join('');
      return `<div class="publication-year-group">
        <span class="publication-year-total${relative ? ' is-hidden' : ''}"${relative ? ' aria-hidden="true"' : ''}>${totalLabel}</span>
        <div class="publication-year-columns">${bars}</div>
        <span class="publication-year-label${year % 5 === 0 ? ' is-major' : ''}">${year}</span>
      </div>`;
    }).join('');
    const minWidth = Math.max(520, timeline.years.length * 45 + 65);
    const axisTitle = relative
      ? (cumulative ? 'Share of venue’s cumulative papers (%)' : 'Share of venue’s papers (%)')
      : (cumulative ? 'Cumulative number of papers' : 'Number of papers');
    return `<div class="publication-chart-panel" data-publication-chart="${cumulative ? 'cumulative' : 'annual'}">
      <p class="chart-axis-label chart-axis-y">${axisTitle}</p>
      <div class="publication-chart-scroll" role="region" tabindex="0" aria-label="${escapeAttr(title)}">
        <div class="publication-plot" style="min-width:${minWidth}px">
          <div class="publication-axis" aria-hidden="true">${axisLabels}</div>
          <div class="publication-chart-main">
            <div class="publication-bars-area">
              <div class="publication-grid" aria-hidden="true"></div>
              <div class="publication-year-groups">${yearGroups}</div>
            </div>
          </div>
        </div>
      </div>
      <p class="chart-axis-label chart-axis-x">Year</p>
    </div>`;
  }

  function renderGlobalTimeline() {
    if (!els.globalTimeline) return;
    closeMetricInfoPanels();
    if (els.timelineActionNote) {
      els.timelineActionNote.textContent = state.publicationView === 'cumulative'
        ? "Select a point to inspect that venue’s papers through the year."
        : "Select a point to inspect that venue’s papers for the year.";
    }
    // Above-the-explorer charts always reflect the full resolved cohort.
    const people = scopePeople();
    const entries = aggregatePapers(people).filter((entry) => paperScope(entry.paper) === 'main');
    renderTimelineLegend();
    const timeline = publicationTimelineData(entries);
    insightScope('publication-timeline', `${state.publicationView === 'cumulative' ? 'Cumulative' : 'Annual'} main-conference papers · ${recordedYearRange(timeline?.years || [])}`);
    if (!timeline || !mainVenueOrder.some((venue) => state.timelineVisible[venue] !== false)) {
      els.globalTimeline.innerHTML = '<p class="viz-chart-note">No main-conference papers match the selected researchers and venues.</p>';
    } else {
      const relative = state.publicationScale === 'relative';
      const cumulative = state.publicationView === 'cumulative';
      els.globalTimeline.innerHTML = publicationLineChartMarkup(timeline, { cumulative, relative });

    }
    els.globalTimeline.querySelectorAll('[data-publication-year][data-publication-venue]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        if (!(Number(button.dataset.publicationCount) > 0)) return;
        const year = Number(button.dataset.publicationYear);
        const venue = button.dataset.publicationVenue;
        if (!Number.isFinite(year) || !mainVenueOrder.includes(venue)) return;
        const visibility = Object.fromEntries(mainVenueOrder.map((name) => [name, name === venue]));
        openTimelineYear(year, people, {
          mode: 'main', visibility,
          cumulative: state.publicationView === 'cumulative',
          relative: state.publicationScale === 'relative',
        });
      });
      button.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }
      });
    });
    els.timelineLegend.querySelectorAll('[data-timeline-venue]').forEach((button) => {
      button.addEventListener('click', () => {
        const venue = button.dataset.timelineVenue;
        state.timelineVisible[venue] = state.timelineVisible[venue] === false;
        renderGlobalTimeline();
      });
    });
    els.timelineLegend.querySelectorAll('[data-publication-scale]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.publicationScale === button.dataset.publicationScale) return;
        state.publicationScale = button.dataset.publicationScale;
        renderGlobalTimeline();
        els.timelineLegend.querySelector(`[data-publication-scale="${state.publicationScale}"]`)?.focus({ preventScroll: true });
      });
    });
    els.timelineLegend.querySelectorAll('[data-publication-view]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.publicationView === button.dataset.publicationView) return;
        state.publicationView = button.dataset.publicationView;
        renderGlobalTimeline();
        els.timelineLegend.querySelector(`[data-publication-view="${state.publicationView}"]`)?.focus({ preventScroll: true });
      });
    });
  }

  function mainMlPublicationYears(person) {
    return [...new Set(mainPapersFor(person)
      .filter((paper) => paper.year != null && String(paper.year).trim() !== '')
      .map((paper) => Number(paper.year))
      .filter((year) => Number.isInteger(year) && year > 0))];
  }

  function firstMainMlYear(person) {
    const years = mainMlPublicationYears(person);
    return years.length ? Math.min(...years) : null;
  }

  function mlParticipationCounts(people) {
    const counts = new Map();
    const firstTimeCounts = new Map();
    people.forEach((person) => {
      const years = mainMlPublicationYears(person);
      years.forEach((year) => counts.set(year, (counts.get(year) || 0) + 1));
      const firstYear = firstMainMlYear(person);
      if (firstYear !== null) firstTimeCounts.set(firstYear, (firstTimeCounts.get(firstYear) || 0) + 1);
    });
    return { counts, firstTimeCounts };
  }

  function renderMlParticipants() {
    if (!els.mlParticipantsChart) return;
    const { counts, firstTimeCounts } = mlParticipationCounts(allPeople());
    const firstRecorded = counts.size ? Math.min(...counts.keys()) : null;
    const lastRecorded = counts.size ? Math.max(...counts.keys()) : null;
    const homeEnd = Math.min(lastRecorded, sharedPublicationEndYear() ?? lastRecorded);
    const homeStart = Math.max(firstRecorded, homeEnd - 9);
    const hasHomePeriod = counts.size && homeEnd >= homeStart;
    if (!hasHomePeriod) state.participantPeriod = 'all';
    const periodFirst = state.participantPeriod === 'home' ? homeStart : firstRecorded;
    const periodLast = state.participantPeriod === 'home' ? homeEnd : lastRecorded;
    insightScope('ml-participants', `Annual researchers · ${recordedYearRange([periodFirst, periodLast].filter(year => year != null))}`);
    const groups = [
      { kind: 'returning', label: 'Published before', colorClass: ' participant-returning-fill' },
      { kind: 'first', label: 'First-time authors', colorClass: '' },
    ];
    if (els.mlParticipantsLegend) {
      els.mlParticipantsLegend.innerHTML = `<div class="timeline-venue-controls">${groups.map(({ kind, label, colorClass }) => {
        const visible = state.participantVisible[kind];
        return `<button type="button" class="legend-item legend-toggle${visible ? '' : ' is-off'}" data-participant-group="${kind}" aria-pressed="${visible}"><i class="legend-swatch participant-column-fill${colorClass}" aria-hidden="true"></i>${label}</button>`;
      }).join('')}</div>${hasHomePeriod ? `<div class="viz-view-switch" role="group" aria-label="Participation years"><button type="button" data-participant-period="home" aria-pressed="${state.participantPeriod === 'home'}" class="${state.participantPeriod === 'home' ? 'is-active' : ''}">${homeStart}–${homeEnd}</button><button type="button" data-participant-period="all" aria-pressed="${state.participantPeriod === 'all'}" class="${state.participantPeriod === 'all' ? 'is-active' : ''}">All years</button></div>` : ''}`;
      els.mlParticipantsLegend.querySelectorAll('[data-participant-group]').forEach((button) => {
        button.addEventListener('click', () => {
          const kind = button.dataset.participantGroup;
          state.participantVisible[kind] = !state.participantVisible[kind];
          renderMlParticipants();
          els.mlParticipantsLegend.querySelector(`[data-participant-group="${kind}"]`)?.focus({ preventScroll: true });
        });
      });
      els.mlParticipantsLegend.querySelectorAll('[data-participant-period]').forEach(button => {
        button.addEventListener('click', () => {
          state.participantPeriod = button.dataset.participantPeriod;
          renderMlParticipants();
          els.mlParticipantsLegend.querySelector(`[data-participant-period="${state.participantPeriod}"]`)?.focus({ preventScroll: true });
        });
      });
    }
    if (!counts.size) {
      els.mlParticipantsChart.innerHTML = '<p class="viz-chart-note">No main-conference participation recorded.</p>';
      return;
    }
    if (!Object.values(state.participantVisible).some(Boolean)) {
      els.mlParticipantsChart.innerHTML = '<p class="viz-chart-note">Select an author group to show researchers.</p>';
      return;
    }
    const first = state.participantPeriod === 'home' ? homeStart : firstRecorded;
    const last = state.participantPeriod === 'home' ? homeEnd : lastRecorded;
    const years = Array.from({ length: last - first + 1 }, (_, index) => first + index);
    const yearWidth = Math.max(1, els.mlParticipantsChart.clientWidth - 64) / years.length;
    const labelIndices = new Set([0, years.length - 1]);
    let previousLabel = 0;
    for (let index = 1; index < years.length - 1; index++) {
      if ((index - previousLabel) * yearWidth >= 28 && (years.length - 1 - index) * yearWidth >= 28) {
        labelIndices.add(index);
        previousLabel = index;
      }
    }
    const visibleCount = (year) => (state.participantVisible.first ? firstTimeCounts.get(year) || 0 : 0)
      + (state.participantVisible.returning ? (counts.get(year) || 0) - (firstTimeCounts.get(year) || 0) : 0);
    const axisMaximum = publicationAxisMaximum(years.map(visibleCount), false);
    const axisLabels = [0, 1, 2, 3, 4].map((tick) => `<span class="publication-axis-tick" style="bottom:${25 * tick}%">${fmt.format(Math.round(axisMaximum * tick / 4))}</span>`).join('');
    const yearGroups = years.map((year, index) => {
      const total = visibleCount(year);
      const firstTime = firstTimeCounts.get(year) || 0;
      const returning = (counts.get(year) || 0) - firstTime;
      const column = [
        { kind: 'first', count: firstTime, label: 'senior DB researchers with their first recorded main-conference paper at NeurIPS, ICML or ICLR', colorClass: '' },
        { kind: 'returning', count: returning, label: 'researchers with an earlier NeurIPS, ICML or ICLR paper publishing again', colorClass: ' participant-returning-fill' },
      ].filter(({ kind, count }) => state.participantVisible[kind] && count > 0).map(({ kind, count, label, colorClass }) => {
        const height = 100 * count / axisMaximum;
        const title = `${fmt.format(count)} ${label} in ${year}`;
        return `<button type="button" class="participant-segment participant-column-fill${colorClass}" style="height:${height.toFixed(3)}%" data-participants-year="${year}" data-participants-kind="${kind}" aria-label="Show ${title}" title="${title}"></button>`;
      }).join('');
      return `<div class="publication-year-group">
        <span class="publication-year-total${yearWidth < 22 ? ' is-hidden' : ''}" title="${fmt.format(total)} researchers in the visible groups in ${year}">${fmt.format(total)}</span>
        <div class="publication-year-columns participant-year-columns"><div class="publication-column participant-stack"${total ? '' : ` role="img" aria-label="No researchers in the visible groups in ${year}"`}>${column}</div></div>
        <span class="publication-year-label${year % 5 === 0 ? ' is-major' : ''}${labelIndices.has(index) ? '' : ' is-hidden'}">${year}</span>
      </div>`;
    }).join('');
    els.mlParticipantsChart.innerHTML = `<p class="chart-axis-label chart-axis-y">Number of researchers</p>
    <div class="participant-chart" role="group" aria-label="senior DB researchers publishing at NeurIPS, ICML or ICLR each year, grouped by first-time authors or published before">
      <div class="publication-plot">
        <div class="publication-axis" aria-hidden="true">${axisLabels}</div>
        <div class="publication-chart-main"><div class="publication-bars-area">
          <div class="publication-grid" aria-hidden="true"></div>
          <div class="publication-year-groups">${yearGroups}</div>
        </div></div>
      </div>
    </div>
    <p class="chart-axis-label chart-axis-x">Year</p>`;
    els.mlParticipantsChart.querySelectorAll('[data-participants-year]').forEach((button) => {
      button.addEventListener('click', () => {
        const year = Number(button.dataset.participantsYear);
        if (!Number.isFinite(year)) return;
        if (button.dataset.participantsKind === 'first') {
          selectResearchersFromChart('ml-first-year', year, `first observed main-conference publication in ${year}`);
        } else {
          selectResearchersFromChart('ml-returning-year', year, `researchers publishing in ${year} with an earlier NeurIPS, ICML or ICLR paper`);
        }
      });
    });
  }

  // Shared exclusive role definition for the chart AND explorer drilldown.
  // Do not use authorMeta(): Number(null) would incorrectly imply first authorship.
  function crossoverAuthorshipRole(paper) {
    const positionValue = paper?.authorPosition;
    const countValue = paper?.authorCount;
    if (positionValue == null || positionValue === '' || countValue == null || countValue === '') return null;
    const position = Number(positionValue);
    const authorCount = Number(countValue);
    if (!Number.isInteger(position) || !Number.isInteger(authorCount) || authorCount < 1 || position < 0 || position >= authorCount) return null;
    if (authorCount === 1) return 'sole';
    if (position === 0) return 'first';
    if (position === authorCount - 1) return 'last';
    return 'other';
  }

  // Each researcher–paper relationship is an authorship: a shared paper can
  // contribute more than once. Invalid metadata is excluded from both views.
  function crossoverAuthorshipDistribution() {
    const counts = { sole: 0, first: 0, last: 0, other: 0 };
    let excluded = 0;
    allPeople().filter(hasMain).forEach((person) => {
      mainPapersFor(person).forEach((paper) => {
        const role = crossoverAuthorshipRole(paper);
        if (role === null) excluded += 1;
        else counts[role] += 1;
      });
    });
    return { ...counts, total: Object.values(counts).reduce((sum, count) => sum + count, 0), excluded };
  }

  function renderCrossoverAuthorship() {
    if (!els.mlAuthorshipChart) return;
    const distribution = crossoverAuthorshipDistribution();
    const years = allPeople().flatMap(person => mainPapersFor(person)
      .filter(paper => crossoverAuthorshipRole(paper) !== null).map(paper => paper.year));
    insightScope('ml-authorship', `Main-conference authorships · ${recordedYearRange(years)}`);
    if (!distribution.total) {
      els.mlAuthorshipChart.innerHTML = '<p class="viz-chart-note">No main-conference authorships with complete author-order data are available.</p>';
      return;
    }
    const roles = [
      { key: 'sole', name: 'Sole author' },
      { key: 'first', name: 'First author' },
      { key: 'other', name: 'Middle author' },
      { key: 'last', name: 'Last author' },
    ];
    const percentage = (count) => (100 * count / distribution.total).toFixed(1);
    // A person can hold one role on multiple papers: count distinct researchers
    // for the explorer action, while the displayed chart keeps authorship totals.
    const roleResearcherCounts = Object.fromEntries(roles.map((role) => [role.key,
      allPeople().filter(hasMain).filter((person) =>
        mainPapersFor(person).some((paper) => crossoverAuthorshipRole(paper) === role.key)).length
    ]));
    const accessibleLabel = roles.map((role) => `${role.name}: ${fmt.format(distribution[role.key])} authorships, ${percentage(distribution[role.key])}%`).join('; ');
    const drilldownLabel = (role) => `Show ${fmt.format(roleResearcherCounts[role.key])} senior DB researchers listed as ${role.name.toLowerCase()} on at least one main-conference paper at NeurIPS, ICML or ICLR`;
    const segments = roles.filter((role) => distribution[role.key] > 0).map((role) => `
      <button type="button" class="viz-authorship-segment viz-authorship-${role.key}" data-authorship-role="${role.key}"
        style="width:${(100 * distribution[role.key] / distribution.total).toFixed(5)}%"
        title="${escapeAttr(drilldownLabel(role))}" aria-label="${escapeAttr(drilldownLabel(role))}"></button>`).join('');
    const legend = roles.map((role) => {
      const count = distribution[role.key];
      return `<div class="viz-authorship-legend-item" role="listitem">
        <button type="button" class="viz-authorship-legend-action" data-authorship-role="${role.key}" ${count ? '' : 'disabled'}
          title="${escapeAttr(drilldownLabel(role))}" aria-label="${escapeAttr(drilldownLabel(role))}">
          <span class="viz-authorship-legend-name"><i class="viz-authorship-swatch viz-authorship-${role.key}" aria-hidden="true"></i><span>${role.name}</span></span>
          <strong>${percentage(count)}%</strong>
          <span class="viz-authorship-legend-count">${fmt.format(count)} ${count === 1 ? 'authorship' : 'authorships'}</span>
        </button>
      </div>`;
    }).join('');
    els.mlAuthorshipChart.innerHTML = `
      <div class="viz-authorship-bar" role="group" aria-label="${escapeAttr(`Author positions at NeurIPS, ICML or ICLR; ${accessibleLabel}`)}">${segments}</div>
      <div class="viz-authorship-axis" aria-hidden="true"><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div>
      <div class="viz-authorship-legend" role="list">${legend}</div>`;
    els.mlAuthorshipChart.querySelectorAll('button[data-authorship-role]').forEach((button) => {
      button.addEventListener('click', () => {
        const role = roles.find((candidate) => candidate.key === button.dataset.authorshipRole);
        if (role && distribution[role.key] > 0) {
          selectResearchersFromChart('authorship-role', role.key,
            `${role.name} on a main-conference paper at NeurIPS, ICML or ICLR (at least once)`);
        }
      });
    });
  }

  function renderMlPortfolio() {
    if (!els.mlPortfolioChart) return;
    const bins = portfolioBins().map(bin => ({ ...bin, count: 0 }));
    allPeople().forEach((person) => {
      const share = mainMlPortfolioShare(person);
      if (share === null) return;
      const bin = bins.find((candidate) => candidate.matches(share));
      if (bin) bin.count += 1;
    });
    const largest = Math.max(1, ...bins.map((bin) => bin.count));
    els.mlPortfolioChart.innerHTML = `<p class="chart-axis-label chart-axis-y">Number of researchers</p>
    <div class="viz-histogram-scroll" role="region" aria-label="Main-conference portfolio share by researcher">
      <div class="viz-histogram" style="--portfolio-bin-count:${bins.length}" role="group" aria-label="Distribution of main-conference paper share among DB researchers">
        ${bins.map((bin, index) => `<button type="button" class="viz-histogram-bin" data-portfolio-bin="${index}" ${bin.count ? '' : 'disabled'} aria-label="Show ${fmt.format(bin.count)} researchers with ${escapeAttr(bin.description)}" title="${fmt.format(bin.count)} researchers with ${escapeAttr(bin.description)}">
          <strong>${fmt.format(bin.count)}</strong>
          <span class="viz-histogram-track"><i style="height:${100 * bin.count / largest}%" aria-hidden="true"></i></span>
          <span class="viz-histogram-label">${escapeHtml(bin.label)}</span>
        </button>`).join('')}
      </div>
    </div>
    <p class="chart-axis-label chart-axis-x">Researchers’ publication share at NeurIPS, ICML or ICLR<button class="info-icon-button chart-axis-info" type="button" data-provenance-metric="ml_portfolio" aria-expanded="false" aria-label="How publication share is calculated"><svg viewBox="0 0 12 12" aria-hidden="true" focusable="false"><circle cx="6" cy="6" r="5.5" fill="none" stroke="currentColor" stroke-width="1"/><circle cx="6" cy="3.1" r="0.7" fill="currentColor"/><path d="M6 5.4v3.6" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button></p>`;
    els.mlPortfolioChart.querySelectorAll('[data-portfolio-bin]').forEach((button) => {
      button.addEventListener('click', () => {
        const index = Number(button.dataset.portfolioBin);
        if (Number.isInteger(index) && bins[index]) {
          selectResearchersFromChart('portfolio', index, `main-conference portfolio share ${bins[index].label}`);
        }
      });
    });
  }

  function miniTimelineSvg(person) {
    const allPapers = mainPapersFor(person).filter((paper) => Number.isFinite(Number(paper.year)));
    if (!allPapers.length) return '<span class="mini-timeline-empty">No main-conference papers</span>';
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
      const totalHeight = linearBarHeight(total, maxTotal, 24);
      const buckets = new Map();
      yearPapers.forEach((paper) => {
        let key = mainVenueLabel(paper);
        if (state.mlVenue) {
          if (mainVenueLabel(paper) !== state.mlVenue) key = 'muted';
          else key = 'highlight';
        } else if (selectedYear() && Number(paper.year) !== selectedYear()) key = 'muted';
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
        rects.push(`<rect class="${klass}" x="${index * unit + 0.5}" y="${cursor.toFixed(2)}" width="3" height="${segmentHeight.toFixed(2)}"></rect>`);
      });
      const tooltipEntries = yearPapers.map((paper) => ({ paper }));
      return `<g><title>${escapeHtml(timelineColumnTooltip(year, tooltipEntries, 'main'))}</title>${rects.join('')}</g>`;
    }).join('');

    return `
      <svg class="mini-timeline-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Main-conference papers from ${careerMin} to ${careerMax}">
        <line class="mini-axis" x1="0" y1="28.5" x2="${width}" y2="28.5"></line>
        ${bars}
      </svg>
      <span class="mini-timeline-range">${careerMin}–${careerMax}</span>
    `;
  }

  function renderPeople() {
    if (!els.body) return;
    if (els.discoveryNote) els.discoveryNote.hidden = false;
    const people = sortPeople(filteredPeople());
    const total = people.length;
    const pageSize = resolvedPageSize();
    if (els.pageSize && els.pageSize.value !== String(state.pageSize)) els.pageSize.value = String(state.pageSize);
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    state.page = Math.min(Math.max(1, Number(state.page) || 1), totalPages);

    const startIndex = total ? (state.page - 1) * pageSize : 0;
    const endIndex = Math.min(startIndex + pageSize, total);
    const visiblePeople = people.slice(startIndex, endIndex);

    updateChartSelectionBanner();
    updateExplorerCohortSwitch();
    updateExplorerSort();
    els.results.innerHTML = `
      <span><strong>${fmt.format(total)}</strong> ${total === 1 ? 'researcher' : 'researchers'}</span>
      ${publicationSearchState === 'loading' && normalizeSearch(state.search.trim()).length >= 3
        ? '<span class="results-count-loading">· checking broader paper titles…</span>' : ''}`;
    els.empty.hidden = total > 0;
    const query = state.search.trim();
    if (!total) {
      els.emptyTitle.textContent = query ? `We couldn’t find “${query}”.` : 'We couldn’t find anyone matching these filters.';
    }
    if (els.resultsNoteTitle) {
      els.resultsNoteTitle.hidden = total === 0;
      els.resultsNoteTitle.textContent = query ? `Missing someone in the results for “${query}”?` : 'Missing someone from these results?';
    }
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
        ? `${matchingCount} matching main-conference ${matchingCount === 1 ? 'paper' : 'papers'}`
        : `${allMain.length} main-conference ${allMain.length === 1 ? 'paper' : 'papers'}`;
      const mlChips = mainVenueOrder.filter((venue) => venueCounts[venue] > 0)
        .map((venue) => `<span class="${mlChipClasses(venue)}">${escapeHtml(venue)} ${fmt.format(venueCounts[venue])}</span>`)
        .join('');
      return `
        <tr tabindex="0" data-person-id="${escapeAttr(person.id)}" aria-label="Open ${escapeAttr(displayPersonName(person.name))} details">
          <td>
            <span class="person-name">${escapeHtml(displayPersonName(person.name))}</span>
            ${focusBadgeMarkup(person)}
            <span class="person-sub">${escapeHtml(personPrimaryAffiliation(person) || person.dblpPid || '')}</span>
          </td>
          <td>
            <div class="chips">${context.dbVenues.map((venue) => chip(dbVenueLabel(venue), 'neutral')).join('')}</div>
            <div class="metric-inline">${fmt.format(context.dbEvents.length)} recorded PC ${context.dbEvents.length === 1 ? 'role' : 'roles'}</div>
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
      row.addEventListener('click', (event) => {
        if (!event.target.closest('details, a, button')) open();
      });
      row.addEventListener('keydown', (event) => {
        if (event.target !== row) return;
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
        <div class="event-top"><strong>${escapeHtml(event.venue)} · ${escapeHtml(labelRole(event.role))}${eventSourceArrowMarkup(event)}</strong><span>${escapeHtml(event.year)}</span></div>
        <div class="entry-meta-row">
          <div class="event-sub">${escapeHtml(eventAffiliations(event).join(' · '))}${event.rounds?.length ? ` · ${escapeHtml(event.rounds.join(', '))}` : ''}</div>
          ${eventProvenanceMarkup(event)}
        </div>
      </li>
    `).join('');
  }

  function authorMeta(paper) {
    const zeroBased = Number(paper?.authorPosition);
    const count = Number(paper?.authorCount);
    const hasPosition = Number.isInteger(zeroBased) && zeroBased >= 0;
    const hasCount = Number.isInteger(count) && count >= 1;
    const position = hasPosition ? zeroBased + 1 : null;
    const first = hasPosition && zeroBased === 0;
    const last = hasPosition && hasCount && zeroBased === count - 1;
    const sole = first && hasCount && count === 1;
    return { position, count: hasCount ? count : null, first, last, sole, complete: hasPosition && hasCount };
  }

  // Assign every paper to exactly one authorship bucket. Higher-priority
  // categories win so the summary, distribution, and filters never double-count.
  function authorshipBucket(paper) {
    const meta = authorMeta(paper);
    if (meta.sole) return 'sole';
    if (meta.first) return 'first';
    if (meta.last) return 'last';
    if (meta.position != null) return `position:${meta.position}`;
    return null;
  }

  function recordedOrdinalPosition(paper) {
    // Display-only author-order grouping. Do not treat missing positions as author #1.
    const raw = paper?.authorPosition;
    if (raw == null || raw === '') return null;
    const zeroBased = Number(raw);
    return Number.isInteger(zeroBased) && zeroBased >= 0 ? zeroBased + 1 : null;
  }

  function authorshipMatch(paper, filter) {
    if (!filter || filter === 'all') return true;
    if (filter.startsWith('ordinal:')) {
      if (['sole', 'first', 'last'].includes(authorshipBucket(paper))) return false;
      return recordedOrdinalPosition(paper) === Number(filter.slice('ordinal:'.length));
    }
    return authorshipBucket(paper) === filter;
  }

  function authorshipCounts(papers) {
    const out = { first: 0, last: 0, sole: 0, knownPosition: 0, complete: 0 };
    papers.forEach((paper) => {
      const meta = authorMeta(paper);
      const bucket = authorshipBucket(paper);
      if (meta.position != null) out.knownPosition += 1;
      if (meta.complete) out.complete += 1;
      if (bucket === 'sole') out.sole += 1;
      else if (bucket === 'first') out.first += 1;
      else if (bucket === 'last') out.last += 1;
    });
    return out;
  }

  function ordinal(value) {
    const n = Number(value);
    const mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
    if (n % 10 === 1) return `${n}st`;
    if (n % 10 === 2) return `${n}nd`;
    if (n % 10 === 3) return `${n}rd`;
    return `${n}th`;
  }

  function selectionListLabel(labels) {
    if (labels.length < 2) return labels[0] || '';
    return `${labels.slice(0, -1).join(', ')} or ${labels.at(-1)}`;
  }

  function personPublicationSelection(mode) {
    if (mode === 'main') {
      const venues = mainVenueOrder.filter((venue) => dialogMainVenueVisibility[venue] !== false);
      const label = selectionListLabel(venues);
      return {
        papersHeading: label ? `Papers at ${label}` : 'No ML venues selected',
        authorsHeading: label ? `Author positions at ${label}` : 'Author positions (no ML venues selected)',
      };
    }
    const categories = contextClassOrder.filter((key) => dialogContextVisibility[key] !== false);
    const label = categories.length === contextClassOrder.length
      ? 'other publications'
      : categories.length ? `${selectionListLabel(categories.map((key) => publicationClassLabels[key].toLowerCase()))} publications` : '';
    return {
      publicationsLabel: label,
      authorsHeading: label ? `Author positions in ${label}` : 'Author positions (no categories selected)',
    };
  }

  function authorPositionDistributionMarkup(papers, scope, activeFilter, headingOverride = '') {
    const distribution = new Map();
    papers.forEach((paper) => {
      if (['sole', 'first', 'last'].includes(authorshipBucket(paper))) return;
      const position = recordedOrdinalPosition(paper);
      if (position == null) return;
      distribution.set(position, (distribution.get(position) || 0) + 1);
    });
    const rows = [...distribution.entries()]
      .filter(([position]) => position > 1)
      .sort(([a], [b]) => a - b)
      .map(([position, count]) => ({ filter: `ordinal:${position}`, label: `#${position}`, count }));
    // Assign each paper once: Sole, then First, then Last, otherwise its position.
    const counts = authorshipCounts(papers);
    const priorityRows = [
      { filter: 'sole', label: 'Sole', count: counts.sole },
      { filter: 'first', label: 'First', count: counts.first },
      { filter: 'last', label: 'Last', count: counts.last },
    ];
    const maxCount = Math.max(1, ...rows.map((row) => row.count), ...priorityRows.map((row) => row.count));
    const bars = rows.map((row) => {
      const active = activeFilter === row.filter;
      const width = 100 * row.count / maxCount;
      return `
        <button class="author-position-row${active ? ' is-active' : ''}" type="button"
          data-authorship-scope="${escapeAttr(scope)}" data-authorship-filter="${escapeAttr(row.filter)}"
          aria-pressed="${active}" aria-label="Show papers with author position ${escapeAttr(row.label)}">
          <span class="author-position-label">${escapeHtml(row.label)}</span>
          <span class="author-position-track" aria-hidden="true"><i style="width:${width}%"></i></span>
          <strong>${fmt.format(row.count)}×</strong>
        </button>`;
    }).join('');
    const priorityBars = priorityRows.map((row) => {
      const active = activeFilter === row.filter;
      const width = 100 * row.count / maxCount;
      return `
        <button class="author-position-row is-priority${active ? ' is-active' : ''}" type="button"
          data-authorship-scope="${escapeAttr(scope)}" data-authorship-filter="${escapeAttr(row.filter)}"
          aria-pressed="${active}" aria-label="Show ${escapeAttr(row.label)} author papers"${row.count ? '' : ' disabled'}>
          <span class="author-position-label">${escapeHtml(row.label)}</span>
          <span class="author-position-track" aria-hidden="true"><i style="width:${width}%"></i></span>
          <strong>${fmt.format(row.count)}×</strong>
        </button>`;
    }).join('');
    const reset = activeFilter !== 'all'
      ? `<button class="author-position-reset" type="button" data-authorship-scope="${escapeAttr(scope)}" data-authorship-filter="all">Clear position filter</button>`
      : '';
    const heading = headingOverride || (scope === 'main'
      ? 'Author positions at NeurIPS, ICML or ICLR'
      : 'Author positions in other publications');
    return `
      <div class="authorship-inline authorship-inline-${escapeAttr(scope)}" aria-label="${escapeAttr(heading)}">
        <div class="authorship-popover-head">
          <strong>${escapeHtml(heading)}</strong>
          ${reset}
        </div>
        <p class="person-section-instruction">Select a bar to filter papers. Select it again to clear.</p>
        <div class="author-position-distribution">
          <div class="author-position-group-label author-position-group-first">Authorship roles</div>
          <p class="author-position-explanation">Assigned in order: Sole (only author) → First → Last → numbered position. Each paper appears once.</p>
          ${priorityBars}
          <div class="author-position-group-label">Position in the author list</div>
          <p class="author-position-explanation">Remaining papers by author-list position: #2 means second author, #3 means third and so on.</p>
          ${bars || '<p class="author-position-empty">No additional author positions recorded.</p>'}
        </div>
        <p class="authorship-note">Author order does not necessarily measure contribution.</p>
      </div>`;
  }

  function authorshipSummaryMarkup(papers, scope, activeFilter, heading = '') {
    return authorPositionDistributionMarkup(papers, scope, activeFilter, heading);
  }

  function paperAuthorPositionSuffix(paper) {
    const meta = authorMeta(paper);
    if (meta.position == null) return '';
    let position = meta.count != null ? ` · author ${meta.position} of ${meta.count}` : ` · author #${meta.position}`;
    if (meta.sole) position += ' · sole author';
    else {
      if (meta.first) position += ' · first author';
      if (meta.last) position += ' · last author';
    }
    return position;
  }

  function paperList(papers, emptyMessage = 'No detailed paper records available.') {
    if (!papers?.length) return `<li>${escapeHtml(emptyMessage)}</li>`;
    return papers.map((paper) => {
      const title = escapeHtml(paper.title);
      const linked = paper.url ? `<a class="paper-title" href="${escapeAttr(paper.url)}" target="_blank" rel="noreferrer">${title}</a>` : `<span class="paper-title">${title}</span>`;
      const position = paperAuthorPositionSuffix(paper);
      const paperType = paperScope(paper) === 'main' ? 'Main-conference paper' : publicationClassLabel(paper);
      return `<li>${linked}<div class="entry-meta-row"><div class="event-sub">${escapeHtml(paperVenueLabel(paper))} · ${escapeHtml(paper.year)} · ${escapeHtml(paperType)}${position}</div>${publicationProvenanceMarkup(paper)}</div></li>`;
    }).join('');
  }

  function personTimelineMarkup(person, mode, papersOverride = null) {
    const papers = Array.isArray(papersOverride) ? papersOverride : allPapersFor(person);
    const serviceYears = mode === 'main' ? new Map() : null;
    if (serviceYears) list(person?.db?.events).forEach((event) => {
      const year = Number(event.year);
      if (Number.isFinite(year)) serviceYears.set(year, (serviceYears.get(year) || 0) + 1);
    });
    const years = [...papers.map((paper) => Number(paper.year)).filter(Number.isFinite), ...(serviceYears ? [...serviceYears.keys()] : [])];
    const minYear = years.length ? Math.min(...years) : null;
    const maxYear = years.length ? Math.max(...years) : null;
    if (!years.length) return '<p class="viz-chart-note">No publications match these selections.</p>';
    const entries = papers.map((paper) => ({ paper, people: [displayPersonName(person.name)] }));
    const chart = globalTimelineMarkup(entries, {
      mode,
      visibility: mode === 'main' ? dialogMainVenueVisibility : dialogContextVisibility,
      highlightExplorer: mode === 'main',
      normalized: false,
      minYear,
      maxYear,
      serviceYears,
    }).replaceAll('timeline-scroll', `timeline-scroll person-timeline-scroll${serviceYears ? ' person-service-years' : ''}`);
    return `<div class="person-context-chart"><p class="chart-axis-label chart-axis-y">Number of ${mode === 'main' ? 'papers' : 'publications'}</p>${chart}<p class="chart-axis-label chart-axis-x">Year</p></div>`;
  }

  function personPortfolioMarkup(person) {
    const share = mainMlPortfolioShare(person);
    const total = Number(person?.ml?.paperCount);
    const main = Number(person?.ml?.mainPaperCount);
    if (share === null) {
      return '<p class="viz-chart-note">A complete publication total is not available for this researcher.</p>';
    }
    return `<section class="person-portfolio" aria-label="Publication share at NeurIPS, ICML or ICLR">
      <div class="person-portfolio-head"><h3>Publication share at NeurIPS, ICML or ICLR</h3><strong>${share.toFixed(1)}%</strong></div>
      <div class="person-portfolio-track" role="img" aria-label="${fmt.format(main)} of ${fmt.format(total)} publications are main-conference papers at NeurIPS, ICML or ICLR"><span style="width:${share}%"></span></div>
      <div class="person-portfolio-labels"><span>${fmt.format(main)} ${main === 1 ? 'paper' : 'papers'} at NeurIPS, ICML or ICLR</span><span>${fmt.format(total - main)} other ${total - main === 1 ? 'publication' : 'publications'}</span></div>
      <p class="viz-chart-note">Main-conference papers divided by all this researcher’s DBLP publications. Different versions of the same paper count once.</p>
    </section>`;
  }

  function bindPersonTimeline(person) {
    els.dialogContent.querySelectorAll('[data-timeline-year]').forEach((button) => {
      button.addEventListener('click', () => {
        const year = Number(button.dataset.timelineYear);
        const mode = button.dataset.timelineMode || 'main';
        if (Number.isFinite(year)) openTimelineYear(year, [person], {
          mode,
          visibility: mode === 'main' ? dialogMainVenueVisibility : dialogContextVisibility,
          authorshipFilter: mode === 'main' ? dialogMainAuthorship : dialogContextAuthorship,
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
    els.dialogContent.querySelectorAll('[data-person-publication-scale]').forEach((button) => {
      button.addEventListener('click', () => {
        if (dialogMainScale === button.dataset.personPublicationScale) return;
        dialogMainScale = button.dataset.personPublicationScale;
        renderPersonDialog(person);
        els.dialogContent.querySelector(`[data-person-publication-scale="${dialogMainScale}"]`)?.focus({ preventScroll: true });
      });
    });
    els.dialogContent.querySelectorAll('[data-person-publication-view]').forEach((button) => {
      button.addEventListener('click', () => {
        if (dialogMainView === button.dataset.personPublicationView) return;
        dialogMainView = button.dataset.personPublicationView;
        renderPersonDialog(person);
        els.dialogContent.querySelector(`[data-person-publication-view="${dialogMainView}"]`)?.focus({ preventScroll: true });
      });
    });
    els.dialogContent.querySelectorAll('[data-person-publication-year]').forEach((button) => {
      button.addEventListener('click', () => {
        const year = Number(button.dataset.personPublicationYear);
        const venue = button.dataset.publicationVenue;
        if (!Number.isFinite(year) || !mainVenueOrder.includes(venue)) return;
        const visibility = Object.fromEntries(mainVenueOrder.map((name) => [name, name === venue]));
        openTimelineYear(year, [person], {
          mode: 'main', visibility, authorshipFilter: dialogMainAuthorship,
          cumulative: dialogMainView === 'cumulative', relative: dialogMainScale === 'relative',
        });
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
    els.dialogContent.querySelectorAll('[data-authorship-filter]').forEach((button) => {
      button.addEventListener('click', () => {
        const scope = button.dataset.authorshipScope;
        const filter = button.dataset.authorshipFilter || 'all';
        if (scope === 'main') dialogMainAuthorship = dialogMainAuthorship === filter ? 'all' : filter;
        else dialogContextAuthorship = dialogContextAuthorship === filter ? 'all' : filter;
        renderPersonDialog(person);
      });
    });
    const contextDetails = els.dialogContent.querySelector('[data-context-timeline-details]');
    if (contextDetails) {
      contextDetails.addEventListener('toggle', () => {
        dialogContextExpanded = contextDetails.open;
      });
    }
    const contextJump = els.dialogContent.querySelector('[data-person-context-jump]');
    if (contextJump && contextDetails) {
      contextJump.addEventListener('click', () => {
        dialogContextExpanded = true;
        contextDetails.open = true;
        contextDetails.scrollIntoView({ behavior: 'smooth', block: 'start' });
        if (!personDetailsReady(person)) startPersonDetailLoad(person);
      });
    }
    const retryDetails = els.dialogContent.querySelector('[data-retry-person-details]');
    if (retryDetails) {
      retryDetails.addEventListener('click', () => {
        person._publicationDetailsState = 'loading';
        renderPersonDialog(person);
        startPersonDetailLoad(person, { retry: true });
      });
    }
  }

  function renderPersonDialog(person) {
    if (activeInfoAnchor?.closest('dialog') === els.dialog) closeMetricInfoPanels();
    const dblpUrl = person.dblpPid ? `https://dblp.org/pid/${encodeURI(person.dblpPid)}` : '';
    const orcid = list(person.orcids)[0];
    const dbEvents = [...list(person.db?.events)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.venue).localeCompare(String(b.venue)));
    const mainPapers = [...mainPapersFor(person)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.title).localeCompare(String(b.title)));
    const contextReady = personDetailsReady(person);
    const contextPapers = contextReady
      ? [...sidePapersFor(person)].sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.title).localeCompare(String(b.title)))
      : [];
    const contextPaperCount = contextReady ? contextPapers.length : personContextPaperCount(person);
    const mainSelection = personPublicationSelection('main');
    const contextSelection = personPublicationSelection('context');
    const selectedMainPapers = mainPapers.filter((paper) => dialogMainVenueVisibility[mainVenueLabel(paper)] !== false);
    const selectedContextPapers = contextPapers.filter((paper) => dialogContextVisibility[publicationClass(paper)] !== false);
    const authorshipMainPapers = selectedMainPapers.filter((paper) => authorshipMatch(paper, dialogMainAuthorship));
    const timelineMlPapers = authorshipMainPapers;
    const authorshipContextPapers = contextReady
      ? selectedContextPapers.filter((paper) => authorshipMatch(paper, dialogContextAuthorship))
      : [];

    const mainLegend = mainVenueOrder.map((venue) => {
      const on = dialogMainVenueVisibility[venue] !== false;
      const filterClass = '';
      const swatchClass = venueClass(venue);
      return `<button class="legend-item legend-toggle${on ? '' : ' is-off'}${filterClass}" type="button" data-person-main-venue="${escapeAttr(venue)}" aria-pressed="${on}"><i class="legend-swatch ${swatchClass}"></i>${escapeHtml(venue)}</button>`;
    }).join('');

    let contextLegend = '';
    let groupedContextLists = '';
    if (contextReady) {
      const allContextVisible = contextClassOrder.every((key) => dialogContextVisibility[key] !== false);
      contextLegend = `<button class="legend-item legend-toggle${allContextVisible ? '' : ' is-off'}" type="button" data-person-context-category="all" aria-pressed="${allContextVisible}">All</button>` + contextClassOrder.map((key) => {
        const on = dialogContextVisibility[key] !== false;
        return `<button class="legend-item legend-toggle${on ? '' : ' is-off'}" type="button" data-person-context-category="${key}" aria-pressed="${on}"><i class="legend-swatch ${publicationClassCss(key)}"></i>${escapeHtml(publicationClassLabels[key])}</button>`;
      }).join('');

      const visibleContext = authorshipContextPapers.filter((paper) => dialogContextVisibility[publicationClass(paper)] !== false);
      groupedContextLists = contextClassOrder.map((className) => {
        if (dialogContextVisibility[className] === false) return '';
        const classPapers = visibleContext.filter((paper) => publicationClass(paper) === className);
        if (!classPapers.length) return '';
        return `<details class="publication-group"><summary>${escapeHtml(publicationClassLabels[className])}<span>${fmt.format(classPapers.length)}</span></summary><ul class="paper-list">${paperList(classPapers)}</ul></details>`;
      }).join('');
    }

    const contextTimelineBody = contextReady
      ? `<div class="person-timeline-filters" aria-label="Other publication filters">${contextLegend}</div>
        <div class="timeline-instructions person-chart-instructions"><p>Toggle category visibility.</p><p>Select a year to see its publications.</p></div>
        ${personTimelineMarkup(person, 'context', authorshipContextPapers)}
        ${groupedContextLists ? `<div class="context-publication-groups">${groupedContextLists}</div>` : '<p class="viz-chart-note">No other publications match these filters.</p>'}
        ${authorshipSummaryMarkup(selectedContextPapers, 'context', dialogContextAuthorship, contextSelection.authorsHeading)}`
      : personDetailStateMarkup(person);

    const dbVenues = uniqueSorted(dbEvents.map((event) => event.venue));
    const dbYears = dbEvents.map((event) => Number(event.year)).filter(Number.isFinite);
    const mainVenues = uniqueSorted(mainPapers.map(mainVenueLabel));
    const mainYears = mainPapers.map((paper) => Number(paper.year)).filter(Number.isFinite);
    const yearRange = (years) => years.length ? `${Math.min(...years)}–${Math.max(...years)}` : '—';
    const personMainEntries = timelineMlPapers.map((paper) => ({ paper, people: [displayPersonName(person.name)] }));
    const personMainTimeline = publicationTimelineData(personMainEntries);
    const personMainCharts = personMainTimeline && mainVenueOrder.some((venue) => dialogMainVenueVisibility[venue] !== false)
      ? groupedPublicationChartMarkup(personMainTimeline, { cumulative: dialogMainView === 'cumulative', relative: dialogMainScale === 'relative', visibility: dialogMainVenueVisibility, person: true })
      : '<p class="viz-chart-note">No papers match these selections.</p>';
    const contextJumpMarkup = contextPaperCount > 0
      ? `<button class="person-context-jump" type="button" data-person-context-jump aria-label="Jump to ${fmt.format(contextPaperCount)} other ${contextPaperCount === 1 ? 'publication' : 'publications'}">See ${fmt.format(contextPaperCount)} other ${contextPaperCount === 1 ? 'publication' : 'publications'} <span aria-hidden="true">↓</span></button>`
      : '';

    els.dialogContent.innerHTML = `
      <header class="detail-header">
        <p class="eyebrow">Researcher</p>
        <div class="detail-title-row">
          <h2>${escapeHtml(displayPersonName(person.name))}</h2>
          ${researcherInfoMarkup(person)}
        </div>
        ${focusBadgeMarkup(person)}
        ${personPrimaryAffiliation(person) ? `<p class="detail-primary-affiliation">${escapeHtml(personPrimaryAffiliation(person))}</p>` : ''}
        <div class="detail-links">
          ${dblpUrl ? `<a href="${dblpUrl}" target="_blank" rel="noreferrer">DBLP ↗</a>` : ''}
          ${orcid ? `<a href="https://orcid.org/${escapeAttr(orcid)}" target="_blank" rel="noreferrer">ORCID ↗</a>` : ''}
        </div>
      </header>
      <section class="person-inclusion-summary" aria-label="Why this researcher appears">
        <div class="person-inclusion-block">
          <span>Database community service</span>
          <strong>${escapeHtml(dbVenues.join(' · ') || '—')}</strong>
          <div class="person-inclusion-meta"><small>${fmt.format(dbEvents.length)} recorded ${dbEvents.length === 1 ? 'role' : 'roles'} · ${escapeHtml(yearRange(dbYears))}</small></div>
        </div>
        <div class="person-inclusion-block">
          <span>Main-conference publications</span>
          <strong>${fmt.format(mainPapers.length)} ${mainPapers.length === 1 ? 'paper' : 'papers'}${mainVenues.length ? ` · ${escapeHtml(mainVenues.join(' · '))}` : ''}</strong>
          <div class="person-inclusion-meta"><small>${escapeHtml(yearRange(mainYears))}</small>${contextJumpMarkup}</div>
        </div>
      </section>
      ${personPortfolioMarkup(person)}
      <section class="detail-timeline-section main-ml-timeline-section">
        <div class="detail-section-heading">
          <div>
            <p class="eyebrow">Publication activity</p>
            <h3>${escapeHtml(mainSelection.papersHeading)}</h3>
          </div>
          <div class="publication-display-controls">
            <div class="viz-view-switch" role="group" aria-label="Researcher ML publication measurement">
              <button type="button" data-person-publication-scale="absolute" aria-pressed="${dialogMainScale === 'absolute'}" class="${dialogMainScale === 'absolute' ? 'is-active' : ''}">Absolute</button>
              <span class="publication-relative-control"><button type="button" data-person-publication-scale="relative" aria-pressed="${dialogMainScale === 'relative'}" class="${dialogMainScale === 'relative' ? 'is-active' : ''}">Relative</button><button class="info-icon-button publication-relative-info" type="button" data-provenance-metric="person_publication_relative" aria-expanded="false" aria-label="What does Relative mean?"><svg viewBox="0 0 12 12" aria-hidden="true" focusable="false"><circle cx="6" cy="6" r="5.5" fill="none" stroke="currentColor" stroke-width="1"/><circle cx="6" cy="3.1" r="0.7" fill="currentColor"/><path d="M6 5.4v3.6" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button></span>
            </div>
            <div class="viz-view-switch" role="group" aria-label="Researcher ML publication time view">
              <button type="button" data-person-publication-view="annual" aria-pressed="${dialogMainView === 'annual'}" class="${dialogMainView === 'annual' ? 'is-active' : ''}">Annual</button>
              <button type="button" data-person-publication-view="cumulative" aria-pressed="${dialogMainView === 'cumulative'}" class="${dialogMainView === 'cumulative' ? 'is-active' : ''}">Cumulative</button>
            </div>
          </div>
        </div>
        <div class="person-timeline-filters" aria-label="NeurIPS, ICML and ICLR visibility">${mainLegend}</div>
        <div class="timeline-instructions person-chart-instructions"><p>Toggle conference visibility.</p><p>${dialogMainView === 'cumulative' ? 'Select a column to see papers up to that year.' : 'Select a column to see that year’s papers.'}</p></div>
        <div class="person-grouped-publications">${personMainCharts}</div>
      </section>
      <section class="person-authorship-section" aria-label="${escapeAttr(mainSelection.authorsHeading)}">
        ${authorshipSummaryMarkup(selectedMainPapers, 'main', dialogMainAuthorship, mainSelection.authorsHeading)}
      </section>
      <div class="detail-grid">
        <section class="detail-section">
          <h3>Database community service</h3>
          <ul class="timeline-list">${eventList(dbEvents)}</ul>
        </section>
        <section class="detail-section">
          <h3>${escapeHtml(mainSelection.papersHeading)}</h3>
          <ul class="paper-list">${paperList(authorshipMainPapers, 'No papers match these selections.')}</ul>
        </section>
      </div>
      <details class="detail-timeline-section context-timeline-section context-timeline-details" data-context-timeline-details ${dialogContextExpanded ? 'open' : ''}>
        <summary class="context-timeline-summary">
          <div><p class="eyebrow">Other publications</p><h3>${contextReady
            ? contextSelection.publicationsLabel ? `${fmt.format(authorshipContextPapers.length)} ${escapeHtml(contextSelection.publicationsLabel)}` : 'No categories selected'
            : `${fmt.format(contextPaperCount)} publications`}</h3></div>
          <span class="context-summary-toggle" aria-hidden="true"></span>
        </summary>
        <div class="context-timeline-body">
          ${contextTimelineBody}
        </div>
      </details>
    `;
    bindPersonTimeline(person);
  }

  function openPerson(id) {
    closeMetricInfoPanels();
    const person = explorerPeople().find((candidate) => candidate.id === id);
    if (!person) return;
    activePersonId = person.id;
    dialogMainVenueVisibility = Object.fromEntries(mainVenueOrder.map((venue) => [venue, true]));
    dialogMainScale = 'absolute';
    dialogMainView = 'annual';
    dialogContextVisibility = Object.fromEntries(contextClassOrder.map((key) => [key, true]));
    dialogContextExpanded = false;
    dialogMainAuthorship = 'all';
    dialogContextAuthorship = 'all';
    renderPersonDialog(person);
    if (typeof els.dialog.showModal === 'function') els.dialog.showModal();
    else els.dialog.setAttribute('open', '');
    startPersonEvidenceLoad(person);
    if (!personDetailsReady(person)) {
      window.setTimeout(() => startPersonDetailLoad(person), 0);
    }
  }

  function openTimelineYear(year, people, options = {}) {
    closeMetricInfoPanels();
    const mode = options.mode || 'main';
    const visibility = options.visibility || (mode === 'main' ? state.timelineVisible : dialogContextVisibility);
    const authorshipFilter = options.authorshipFilter || 'all';
    const cumulative = options.cumulative === true;
    const entries = aggregatePapers(people)
      .filter((entry) => {
        const paperYear = Number(entry.paper.year);
        return Number.isFinite(paperYear) && (cumulative ? paperYear <= year : paperYear === year);
      })
      .filter((entry) => !cumulative || !options.relative || mainVenuePublishedTotal(mainVenueLabel(entry.paper), Number(entry.paper.year)) > 0)
      .filter((entry) => {
        if (mode === 'main') {
          if (paperScope(entry.paper) !== 'main') return false;
          return visibility[mainVenueLabel(entry.paper)] !== false;
        }
        return paperScope(entry.paper) === 'side' && visibility[publicationClass(entry.paper)] !== false;
      })
      .filter((entry) => authorshipMatch(entry.paper, authorshipFilter))
      .sort((a, b) => {
        if (cumulative && Number(a.paper.year) !== Number(b.paper.year)) return Number(b.paper.year) - Number(a.paper.year);
        if (mode === 'main') {
          return mainVenueOrder.indexOf(mainVenueLabel(a.paper)) - mainVenueOrder.indexOf(mainVenueLabel(b.paper)) || String(a.paper.title).localeCompare(String(b.paper.title));
        }
        return contextClassOrder.indexOf(publicationClass(a.paper)) - contextClassOrder.indexOf(publicationClass(b.paper)) || paperVenueLabel(a.paper).localeCompare(paperVenueLabel(b.paper)) || String(a.paper.title).localeCompare(String(b.paper.title));
      });

    els.timelineDialogContent.innerHTML = `
      <header class="detail-header compact-detail-header">
        <p class="eyebrow">${mode === 'main' ? 'Main-conference' : 'Other publications'}</p>
        <h2>${cumulative ? 'Through ' : ''}${year}</h2>
        <p class="timeline-dialog-summary">${fmt.format(entries.length)} ${entries.length === 1 ? 'paper' : 'papers'}</p>
      </header>
      <ul class="timeline-paper-results">
        ${entries.map((entry) => {
      const paper = entry.paper;
      const badgeClass = mode === 'main'
        ? `paper-badge ${venueClass(mainVenueLabel(paper))}`
        : `paper-badge ${publicationClassCss(publicationClass(paper))}`;
      const badge = mode === 'main' ? mainVenueLabel(paper) : publicationClassLabel(paper);
      const title = paper.url ? `<a href="${escapeAttr(paper.url)}" target="_blank" rel="noreferrer">${escapeHtml(paper.title)}</a>` : escapeHtml(paper.title);
      const position = entry.people.length === 1 ? paperAuthorPositionSuffix(paper) : '';
      return `<li class="timeline-paper-result"><span class="${badgeClass}">${escapeHtml(badge)}</span><div class="timeline-paper-copy"><strong>${title}</strong><div class="timeline-paper-meta-row"><span>${cumulative ? `${escapeHtml(paper.year)} · ` : ''}${escapeHtml(paperVenueLabel(paper))} · ${escapeHtml(entry.people.join(', '))}${position}</span>${publicationProvenanceMarkup(paper)}</div></div></li>`;
    }).join('') || `<li class="empty-state">No papers ${cumulative ? 'through' : 'in'} this year.</li>`}
      </ul>
    `;
    if (typeof els.timelineDialog.showModal === 'function') els.timelineDialog.showModal();
    else els.timelineDialog.setAttribute('open', '');
  }

  function renderEverything({ filters = false } = {}) {
    if (filters) renderFilters();
    renderPeople();
  }

  function resetFilters() {
    state.search = '';
    state.dbVenue = '';
    state.mlVenue = '';
    state.year = '';
    state.sort = 'mlPapers';
    state.page = 1;
    state.explorerCohort = 'crossovers';
    state.chartDrilldown = null;

    els.search.value = '';
    els.dbVenue.value = '';
    els.mlVenue.value = '';
    els.year.value = '';

    renderEverything({ filters: true });
  }

  function bind() {
    els.dialogContent?.addEventListener('click', event => {
      if (event.target.closest('[data-retry-person-evidence]')) {
        const person = explorerPeople().find(person => person.id === activePersonId);
        if (person) startPersonEvidenceLoad(person, { retry: true });
      }
    });
    document.addEventListener('dbml:open-person', event => {
      if (event.detail?.id) openPerson(event.detail.id);
    });
    const rerenderFromFirstPage = ({ refreshFilters = false, summary = false } = {}) => {
      state.page = 1;
      if (refreshFilters) renderFilters();
      renderPeople();
    };

    els.search?.addEventListener('input', () => {
      state.search = els.search.value;
      if (normalizeSearch(state.search.trim())) {
        state.explorerCohort = 'all';
        state.chartDrilldown = null;
      }
      rerenderFromFirstPage({ refreshFilters: true });
      startPublicationSearchLoad();
    });
    els.dbVenue?.addEventListener('change', () => { state.dbVenue = els.dbVenue.value; rerenderFromFirstPage({ refreshFilters: true }); });
    els.mlVenue?.addEventListener('change', () => { state.mlVenue = els.mlVenue.value; rerenderFromFirstPage({ refreshFilters: true }); });
    els.year?.addEventListener('change', () => { state.year = els.year.value; rerenderFromFirstPage(); });
    document.querySelectorAll('[data-explorer-sort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.sort === button.dataset.explorerSort) return;
        state.sort = button.dataset.explorerSort;
        rerenderFromFirstPage();
      });
    });
    document.querySelectorAll('[data-explorer-cohort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (button.dataset.explorerCohort === state.explorerCohort) return;
        state.explorerCohort = button.dataset.explorerCohort === 'all' ? 'all' : 'crossovers';
        state.chartDrilldown = null;
        rerenderFromFirstPage({ refreshFilters: true });
      });
    });
    els.resetFilters?.addEventListener('click', resetFilters);
    els.clearChartSelection?.addEventListener('click', () => {
      state.chartDrilldown = null;
      state.page = 1;
      renderFilters();
      renderPeople();
      focusExplorerFilterAccess();
    });
    els.editExplorerFilters?.addEventListener('click', () => {
      const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      $('#explorer-controls')?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
      els.search.focus({ preventScroll: true });
    });
    document.querySelectorAll('[data-db-rates-view]').forEach((button) => {
      button.addEventListener('click', () => {
        if (button.dataset.dbRatesView === state.dbVenueRatesView) return;
        state.dbVenueRatesView = button.dataset.dbRatesView === 'absolute' ? 'absolute' : 'percentage';
        renderDbVenueRates();
      });
    });
    if (els.emptyResetFilters) els.emptyResetFilters.addEventListener('click', resetFilters);
    els.pageSize?.addEventListener('change', () => {
      state.pageSize = els.pageSize.value || 'auto';
      state.page = 1;
      renderPeople();
    });
    els.prevPage?.addEventListener('click', (event) => {
      event.preventDefault();
      if (els.prevPage.disabled || state.page <= 1) return;
      state.page = Math.max(1, state.page - 1);
      renderPeople();
    });
    els.nextPage?.addEventListener('click', (event) => {
      event.preventDefault();
      if (els.nextPage.disabled) return;
      state.page += 1;
      renderPeople();
    });
    els.dialogClose.addEventListener('click', () => { closeMetricInfoPanels(); els.dialog.close(); });
    // A queued close event can arrive after another researcher is opened.
    els.dialog.addEventListener('close', () => { if (!els.dialog.open) activePersonId = ''; });
    els.dialog.addEventListener('click', (event) => { if (event.target === els.dialog) { closeMetricInfoPanels(); els.dialog.close(); } });
    els.timelineDialogClose.addEventListener('click', () => { closeMetricInfoPanels(); els.timelineDialog.close(); });
    els.timelineDialog.addEventListener('click', (event) => { if (event.target === els.timelineDialog) { closeMetricInfoPanels(); els.timelineDialog.close(); } });
    els.methodologyDialogClose?.addEventListener('click', () => els.methodologyDialog.close());
    els.methodologyDialog?.addEventListener('click', (event) => { if (event.target === els.methodologyDialog) els.methodologyDialog.close(); });
    [els.dialog, els.timelineDialog, els.methodologyDialog].filter(Boolean).forEach((dialog) => {
      dialog.addEventListener('close', () => {
        if (!dialog.open && activeInfoAnchor?.closest('dialog') === dialog) closeMetricInfoPanels();
      });
    });
    bindInfoInteractions();
    document.addEventListener('click', (event) => {
      const downloadSource = event.target.closest('a[data-confirm-source-download]');
      if (downloadSource) {
        const filename = downloadSource.dataset.confirmSourceDownload || 'this source file';
        if (!window.confirm(`This source opens or downloads the file “${filename}”. Do you want to continue?`)) {
          event.preventDefault();
          return;
        }
      }
      const metric = event.target.closest('[data-provenance-metric]');
      if (metric) {
        event.preventDefault();
        pinInfo(metric);
        return;
      }
      const infoSummary = event.target.closest('.provenance-disclosure > summary, .authorship-info > summary');
      if (infoSummary) {
        event.preventDefault();
        pinInfo(infoSummary);
        return;
      }
      const metricMethodology = event.target.closest('[data-open-methodology-metric]');
      if (metricMethodology) {
        event.preventDefault();
        const metricId = metricMethodology.dataset.openMethodologyMetric || '';
        closeMetricInfoPanels();
        openMethodology(metricId);
        return;
      }
      const methodology = event.target.closest('[data-open-methodology]');
      if (methodology) {
        event.preventDefault();
        closeMetricInfoPanels();
        openMethodology();
        return;
      }
      if (!event.target.closest('.metric-context-panel, .provenance-disclosure-body, .authorship-popover')) closeMetricInfoPanels();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && activeInfoAnchor) {
        event.preventDefault();
        const anchor = activeInfoAnchor;
        const restoreFocus = activeInfoPanel()?.contains(document.activeElement);
        closeMetricInfoPanels();
        if (restoreFocus && anchor.isConnected) {
          restoringInfoFocus = true;
          anchor.focus({ preventScroll: true });
          restoringInfoFocus = false;
        }
      }
    });
    document.addEventListener('scroll', repositionOpenInfo, true);
    document.addEventListener('scroll', updateEditFiltersVisibility, { passive: true });
    // Chart containers can resize after the window event (fonts, grid layout or
    // sidebar changes). Observe their actual widths instead of guessing a delay.
    const chartContainers = [els.globalTimeline, els.mlParticipantsChart].filter(Boolean);
    const chartWidths = new Map(chartContainers.map(el => [el, el.clientWidth]));
    let chartResizeTimer = null;
    const redrawResizedCharts = () => {
      if (chartResizeTimer !== null) return;
      // Leave the observer's layout pass before updating SVGs. An animation
      // frame may be suspended for an offscreen iframe even after observation.
      chartResizeTimer = window.setTimeout(() => {
        chartResizeTimer = null;
        for (const el of chartContainers) {
          const width = el.clientWidth;
          if (!width || width === chartWidths.get(el)) continue;
          chartWidths.set(el, width);
          if (el === els.globalTimeline) renderGlobalTimeline();
          else renderMlParticipants();
        }
      }, 0);
    };
    if (window.ResizeObserver) {
      const chartObserver = new ResizeObserver(redrawResizedCharts);
      chartContainers.forEach(el => chartObserver.observe(el));
    }
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      repositionOpenInfo();
      updateEditFiltersVisibility();
      redrawResizedCharts();
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        if (state.pageSize === 'auto') renderPeople();
      }, 120);
    });
  }

  function init() {
    if (!els.body && els.summary && window.location.hash === '#explore') {
      window.location.replace(new URL(`people.html${window.location.search}#explore`, window.location.href).href);
      return;
    }
    if (!els.body && !els.summary) {
      const button = document.querySelector('[data-open-methodology]');
      let loading = null;
      button?.addEventListener('click', async () => {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        try {
          if (!data) {
            loading ||= new Promise((resolve, reject) => {
              const script = document.createElement('script');
              script.src = button.dataset.methodologyDataSrc;
              script.onload = resolve;
              script.onerror = () => { script.remove(); reject(new Error('Source data unavailable')); };
              document.head.appendChild(script);
            });
            await loading;
            data = expandServiceEvents(window.CROSSOVER_DATA) || null;
            provenance = data?.provenance || {};
          }
          openMethodology();
        } catch {
          loading = null;
          els.methodologyDialogContent.innerHTML = '<p>Methodology could not be loaded. Please try again.</p>';
          els.methodologyDialog.showModal();
        } finally {
          button.disabled = false;
          button.removeAttribute('aria-busy');
        }
      });
      els.methodologyDialogClose?.addEventListener('click', () => els.methodologyDialog.close());
      document.addEventListener('click', (event) => {
        const link = event.target.closest('[data-confirm-source-download]');
        if (link && !window.confirm(`This source opens or downloads the file “${link.dataset.confirmSourceDownload}”. Do you want to continue?`)) event.preventDefault();
      });
      return;
    }
    if (!data || data.meta?.dataReady === false) {
      if (els.dataStatus) els.dataStatus.textContent = 'No generated dataset found. Run the DBLP publication scan and website exporter.';
      window.LoadingPreview?.failPending();
      return;
    }
    renderSummary();
    renderInsightScopes();
    renderFooterRefresh();
    renderFilters();
    renderDbVenueRates();
    renderMlParticipants();
    renderMlPortfolio();
    renderCrossoverAuthorship();
    renderGlobalTimeline();
    renderInsightsInfo();
    bind();
    renderPeople();
    updateEditFiltersVisibility();
    document.querySelectorAll('[data-loading-controls]').forEach(control => { control.inert = false; });
    if (els.body) {
      const params = new URLSearchParams(window.location.search);
      const kind = params.get('selection');
      const kinds = ['venue-service', 'venue-ml', 'ml-status', 'ml-year', 'ml-first-year', 'ml-returning-year', 'portfolio', 'authorship-role'];
      if (kinds.includes(kind) && params.has('value')) {
        selectResearchersFromChart(kind, params.get('value'), params.get('label') || params.get('value'));
      }
    }
  }

  init();
})();
