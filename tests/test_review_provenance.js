/* Review labels, safe evidence links and hover behavior in the real UI. */
window.runReviewProvenanceTests = async function(frame, baseUrl) {
  let checks = 0;
  const assert = (value, message) => { if (!value) throw new Error(message); checks++; };
  const wait = async condition => {
    for (let i = 0; i < 200; i++) {
      if (condition()) return;
      await new Promise(resolve => setTimeout(resolve, 30));
    }
    throw new Error('Review provenance UI did not become ready');
  };
  const load = async name => {
    const previous = frame.contentDocument;
    frame.src = new URL(name, baseUrl).href;
    await wait(() => frame.contentDocument !== previous && frame.contentDocument?.readyState === 'complete'
      && frame.contentWindow.CROSSOVER_DATA && frame.contentDocument.querySelector(name === 'people.html' ? '#people-body tr' : '[data-publication-year]'));
    return frame.contentDocument;
  };
  const review = kind => ({kind, method: 'ai_assisted', reviewedOn: '2026-11-09',
    reason: 'Verified author <img src=x onerror=alert(1)> and institutional evidence.',
    evidenceUrls: ['https://example.org/evidence', 'javascript:alert(1)'],
    reviewFile: 'test-review.csv', recordKey: kind, personName: 'Reviewed researcher'});
  for (const width of [1440, 390]) {
    frame.style.width = width + 'px';
    const doc = await load('people.html'), win = frame.contentWindow;
    const row = doc.querySelector('#people-body tr');
    const person = win.CROSSOVER_DATA.people.find(p => p.id === row.dataset.personId);
    person._evidenceLoaded = person._publicationDetailsLoaded = true;
    person.provenance = {identity: {projectReviews: [review('identity')]},
      primaryAffiliation: {projectReviews: [{...review('affiliation'), usedForDisplay: false}]}};
    person.ml.papers[0].provenance = {projectReviews: [review('authorship')]};
    row.click();
    await wait(() => doc.querySelector('#person-dialog').open);
    const identity = doc.querySelector('.title-provenance');
    assert(identity.textContent.includes('AI-assisted project review'), 'Identity match labels AI assistance');
    assert(identity.textContent.includes('2026-11-09'), 'Review date is visible');
    assert(identity.textContent.includes('does not set the displayed affiliation'), 'Older reviewed evidence is distinguished from the selected affiliation');
    assert(identity.querySelector('a[href="https://dblp.org/pid/'+person.dblpPid+'"]'), 'Official DBLP source remains separate');
    assert(identity.querySelector('a[href="https://example.org/evidence"]'), 'Review evidence is linked');
    assert(!identity.querySelector('img') && identity.textContent.includes('<img'), 'Review reason is escaped');
    assert(!identity.querySelector('a[href^="javascript:"]'), 'Unsafe review links are omitted');
    const reason = identity.querySelector('.project-review-reason');
    assert(reason && !reason.open, 'Detailed review reason is available without initial clutter');
    const paperInfo = [...doc.querySelectorAll('.entry-provenance')].find(el => el.textContent.includes('Our assignment of this paper'));
    assert(paperInfo && paperInfo.textContent.includes('AI-assisted project review'), 'Reviewed paper authorship is labelled');
    const anchor = identity.querySelector('summary');
    anchor.dispatchEvent(new win.PointerEvent('pointerover', {bubbles:true, pointerType:'mouse'}));
    await wait(() => identity.open);
    const bounds = identity.querySelector('.provenance-disclosure-body').getBoundingClientRect();
    assert(bounds.left >= 0 && bounds.right <= doc.documentElement.clientWidth + 1, 'Review tooltip fits '+width+'px viewport');
    doc.querySelector('#dialog-close').click();
    doc.querySelector('[data-open-methodology]').click();
    assert(doc.querySelector('#methodology-dialog-content').textContent.includes('These are project decisions'), 'Methodology explains the distinction from official sources');
    doc.querySelector('#methodology-dialog-close').click();
  }
  frame.style.width = '';
  const doc = await load('all.html'), win = frame.contentWindow;
  const records = new Map();
  let shared;
  for (const person of win.CROSSOVER_DATA.people) {
    for (const paper of person.ml.papers.filter(p => p.scope === 'main')) {
      const key = paper.workId || paper.dblpKey;
      if (records.has(key)) { shared = [records.get(key), paper]; break; }
      records.set(key, paper);
    }
    if (shared) break;
  }
  assert(shared, 'Fixture has a shared main-conference paper');
  shared[0].provenance = {};
  shared[1].provenance = {projectReviews:[review('authorship')]};
  const paper = shared[1];
  doc.querySelector(`[data-publication-year="${paper.year}"][data-publication-venue="${paper.venue}"]`)
    .dispatchEvent(new win.KeyboardEvent('keydown', {bubbles:true, key:'Enter'}));
  assert(doc.querySelector('#timeline-dialog-content').textContent.includes('AI-assisted project review'), 'Aggregated paper list preserves reviews from either researcher');
  return checks;
};
