/* Check real lazy-loading interactions, including delayed and failed evidence. */
window.runStaticLoadingTests = async function(frame, baseUrl) {
  let checks = 0;
  const assert = (value, message) => { if (!value) throw Error(message); checks++; };
  const wait = async predicate => {
    for (let i = 0; i < 400; i++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 30));
    }
    throw Error('Timed out waiting for static loading');
  };
  const load = async page => {
    const previous = frame.contentDocument;
    frame.src = new URL(page, baseUrl);
    await wait(() => frame.contentDocument !== previous && frame.contentDocument?.readyState === 'complete' &&
      (page.startsWith('index') ? /\d/.test(frame.contentDocument.querySelector('[data-home-total]')?.textContent || '') : frame.contentDocument.querySelector('#people-body tr')));
    return frame.contentDocument;
  };
  let doc = await load('index.html');
  let win = frame.contentWindow;
  const initialRequests = win.performance.getEntriesByType('resource').map(r => r.name);
  assert(!initialRequests.some(url => /\/app\.js|\/site_data\.js/.test(url)), 'Home avoids analysis code and full startup data');
  const append = doc.head.append.bind(doc.head);
  doc.head.append = (...nodes) => {
    nodes.forEach(node => { if (node.src?.includes('/assets/app.js')) node.src = new URL('missing-methodology-test.js', baseUrl).href; });
    return append(...nodes);
  };
  doc.querySelector('[data-open-methodology]').click();
  await wait(() => doc.querySelector('#methodology-dialog').open);
  assert(doc.querySelector('#methodology-dialog-content').textContent.includes('Please try again'), 'Home explains an unavailable methodology script');
  assert(!doc.querySelector('[data-open-methodology]').disabled, 'Methodology can be retried after a failed download');
  doc.head.append = append;
  doc.querySelector('#methodology-dialog-close').click();
  doc.querySelector('[data-open-methodology]').click();
  await wait(() => doc.querySelector('#methodology-dialog').open && doc.querySelector('#methodology-dialog-content').textContent.includes('DBLP'));
  assert(!win.CROSSOVER_DATA.browserServiceEvents, 'Home methodology expands its lazy service records');
  assert(win.CROSSOVER_DATA.people.every(person => Array.isArray(person.db.events)), 'Methodology receives the ordinary data model');
  doc.querySelector('#methodology-dialog-close').click();
  doc.querySelector('[data-open-methodology]').click();
  await wait(() => doc.querySelector('#methodology-dialog').open);
  assert(doc.querySelectorAll('script[src*="assets/app.js"]').length === 1, 'Repeated methodology opens reuse one script');

  doc = await load('people.html'); win = frame.contentWindow;
  assert(!win.performance.getEntriesByType('resource').some(r => /\/people\/evidence-/.test(r.name)), 'No profile evidence is downloaded initially');
  const people = [...win.CROSSOVER_DATA.people, ...win.CROSSOVER_DATA.additionalExplorerPeople];
  const row = doc.querySelector('#people-body tr');
  const person = people.find(p => p.id === row.dataset.personId);
  const nativeFetch = win.fetch.bind(win);
  const expectedEvidence = await (await nativeFetch(person.evidenceUrl)).json();
  let release, evidenceCalls = 0;
  const gate = new Promise(resolve => { release = resolve; });
  win.fetch = async (url, options) => {
    if (String(url) === person.evidenceUrl) { evidenceCalls++; await gate; }
    return nativeFetch(url, options);
  };
  row.click();
  await wait(() => doc.querySelector('#person-dialog').open && evidenceCalls === 1 && person._publicationDetailsLoaded);
  assert(doc.querySelector('.title-provenance').textContent.includes('Loading affiliation'), 'Delayed sources show an honest loading state');
  const toggle = doc.querySelector('[data-person-main-venue]');
  const venue = toggle.dataset.personMainVenue;
  toggle.click();
  const chart = doc.querySelector('.person-grouped-publications');
  const summary = doc.querySelector('.title-provenance summary');
  summary.focus();
  const scroll = doc.querySelector('#person-dialog .dialog-shell');
  const beforeScroll = scroll.scrollTop;
  release();
  await wait(() => person._evidenceLoaded);
  assert(JSON.stringify(person.provenance) === JSON.stringify(expectedEvidence.provenance), 'Researcher evidence is restored without alteration');
  assert(JSON.stringify(person.db.events) === JSON.stringify(expectedEvidence.events), 'Full service provenance is restored without alteration');
  assert(doc.querySelector('.person-grouped-publications') === chart, 'Evidence does not rebuild the charts');
  assert(doc.querySelector(`[data-person-main-venue="${venue}"]`).getAttribute('aria-pressed') === 'false', 'Conference selection survives evidence loading');
  assert(doc.activeElement === summary, 'Keyboard focus survives evidence loading');
  assert(scroll.scrollTop === beforeScroll, 'Evidence does not move the popup scroll position');
  assert(!doc.querySelector('.title-provenance').textContent.includes('Loading affiliation'), 'Loaded sources replace the loading message');
  doc.querySelector('#dialog-close').click();
  row.click();
  assert(evidenceCalls === 1, 'Reopening the same researcher reuses loaded evidence');
  doc.querySelector('#dialog-close').click();

  const secondRow = doc.querySelectorAll('#people-body tr')[1];
  const second = people.find(p => p.id === secondRow.dataset.personId);
  let fail = true;
  win.fetch = async (url, options) => {
    if (String(url) === second.evidenceUrl && fail) return new win.Response('{}', {status: 503});
    return nativeFetch(url, options);
  };
  secondRow.click();
  await wait(() => second._evidenceState === 'error');
  assert(doc.querySelector('[data-retry-person-evidence]'), 'Failed evidence offers a retry');
  assert(doc.querySelector('.person-grouped-publications'), 'Evidence failure leaves the main publication chart usable');
  fail = false;
  doc.querySelector('[data-retry-person-evidence]').click();
  await wait(() => second._evidenceLoaded);
  assert(!doc.querySelector('[data-retry-person-evidence]'), 'Retry restores the source information');
  win.fetch = nativeFetch;
  return checks;
};
