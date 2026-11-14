/* The browser-check server delays/fails data for these explicit test URLs. */
window.runLoadingPreviewTests = async function(frame, baseUrl) {
  let checks = 0;
  const assert = (value, message) => { if (!value) throw Error(message); checks++; };
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const wait = async predicate => {
    for (let i = 0; i < 400; i++) { if (predicate()) return; await pause(20); }
    throw Error('Loading preview did not change state');
  };
  const load = async (page, mode) => {
    const previous = frame.contentDocument;
    frame.src = new URL(`${page}?preview-check=${mode}${mode === 'slow' ? '&first-frames=1' : ''}`, baseUrl);
    await wait(() => frame.contentDocument !== previous && frame.contentDocument?.querySelector('[data-loading-mounted]'));
    await wait(() => [...frame.contentDocument.querySelectorAll('link[rel="stylesheet"]')].every(link => link.sheet));
    await pause(60); // Let the new iframe width and its responsive styles settle.
    return frame.contentDocument;
  };
  const layoutSelectors = ['#overview', '#summary-cards', '.viz-donut-legend', '#db-membership', '#overlap-distribution', '#db-overlap', '#overlap-matrix', '#population-focus', '#focus-groups', '#matrix', '#db-venue-rates', '#publication-timeline', '#timeline-legend', '#global-timeline', '#ml-participants', '#ml-participants-legend', '#ml-participants-chart', '#ml-portfolio', '#ml-portfolio-chart', '#ml-authorship', '#ml-authorship-chart', '#data-coverage'];
  const snapshot = doc => layoutSelectors.map(selector => {
    const rect = doc.querySelector(selector).getBoundingClientRect();
    return {selector, top: rect.top + doc.defaultView.scrollY, height: rect.height};
  });
  const compare = (before, doc, width) => snapshot(doc).forEach((after, index) => {
    assert(Math.abs(before[index].height - after.height) <= 1, `Loading keeps chart height: ${after.selector} at ${width}px (${before[index].height} vs ${after.height})`);
    assert(Math.abs(before[index].top - after.top) <= 2, `Loading keeps section position: ${after.selector} at ${width}px (${before[index].top} vs ${after.top})`);
  });
  const checkFirstFrames = async doc => {
    const audit = doc.defaultView.loadingFrameAudit;
    assert(audit, 'Frame recorder starts before the preview helper');
    await wait(() => audit.done);
    const selectors = new Set(audit.samples.flatMap(sample => Object.keys(sample)));
    for (const selector of selectors) {
      const sizes = audit.samples.map(sample => sample[selector]).filter(Boolean);
      assert(sizes.length > 1, 'Observe multiple frames of '+selector);
      for (const dimension of ['width', 'height']) {
        const values = sizes.map(size => size[dimension]);
        assert(Math.max(...values) - Math.min(...values) <= 2,
          `${selector} keeps its ${dimension} from the first frame (${Math.min(...values)} to ${Math.max(...values)})`);
      }
    }
  };
  for (const page of ['index.html', 'people.html', 'all.html']) {
    const doc = await load(page, 'slow'), win = frame.contentWindow;
    await wait(() => doc.querySelector('.loading-preview'));
    const preview = doc.querySelector('.loading-preview');
    assert(preview.inert && preview.getAttribute('aria-hidden') === 'true', 'Preview is inert and decorative: '+page);
    assert(!preview.querySelector('a,button,input,[tabindex]'), 'Preview has no selectable entries: '+page);
    assert(doc.querySelector('[data-loading-kind][aria-busy="true"]'), 'Region reports its loading state: '+page);
    if (page === 'people.html') {
      assert(doc.querySelector('#explorer-controls').inert, 'Search waits until it can handle input');
      assert(!doc.querySelector('#people-body [data-person-id]'), 'Placeholder rows are never real researchers');
    }
    let headings, legends, before;
    if (page === 'all.html') {
      await wait(() => doc.querySelector('#ml-authorship-chart .loading-preview'));
      before = snapshot(doc);
      headings = [...doc.querySelectorAll('#summary-cards h3')].map(node => node.textContent);
      legends = [...doc.querySelectorAll('#summary-cards .viz-donut-choice > span:last-child')].map(node => node.textContent);
      assert(headings.length === 2, 'Overview keeps both normal panel headings while loading');
      assert(legends.join('|') === 'with at least one paper|without any paper', 'Donut legend stays readable while loading');
      for (const node of doc.querySelectorAll('#summary-cards h3, #summary-cards .viz-donut-choice > span:last-child, #timeline-legend, #ml-participants-legend, .chart-axis-label')) {
        assert(!node.closest('.loading-preview, .loading-preview-art'), 'Static text is outside the blurred preview');
        assert(win.getComputedStyle(node).filter === 'none', 'Static text has no blur filter');
      }
      assert(doc.querySelector('#timeline-legend').textContent.includes('NeurIPS') && doc.querySelector('#timeline-legend').textContent.includes('Cumulative'), 'Conference legend and controls are visible immediately');
      assert(doc.querySelector('#ml-participants-legend').textContent.includes('First-time authors'), 'Participation legend is visible immediately');
      assert(doc.querySelector('#ml-authorship-chart').textContent.includes('Middle author'), 'Author-role labels are visible immediately');
      assert([...doc.querySelectorAll('#overlap-matrix th[scope="col"]')].some(node => node.textContent.includes('SIGMOD')), 'Matrix venue labels are visible immediately');
    }
    await wait(() => doc.readyState === 'complete' && !doc.querySelector('[data-loading-kind][aria-busy="true"]'));
    assert(!doc.querySelector('.loading-preview'), 'All previews are replaced: '+page);
    if (page !== 'people.html') await checkFirstFrames(doc);
    assert(doc.querySelector('.loading-content-ready'), 'Real content gets the reveal transition: '+page);
    if (page === 'all.html') {
      compare(before, doc, frame.width);
      assert(JSON.stringify([...doc.querySelectorAll('#summary-cards h3')].map(node => node.textContent)) === JSON.stringify(headings), 'Loaded overview preserves its headings');
      assert(JSON.stringify([...doc.querySelectorAll('#summary-cards .viz-donut-choice > span:last-child')].map(node => node.textContent)) === JSON.stringify(legends), 'Loaded donut preserves its legend wording');
      assert(doc.querySelector('#summary-cards').getAttribute('aria-busy') === 'false', 'Overview clears its loading state');
    }
    if (page === 'people.html') {
      assert(!doc.querySelector('#explorer-controls').inert, 'Search becomes usable');
      assert(doc.querySelector('#people-body tr[data-person-id]'), 'Real rows replace outlines');
      assert(!doc.querySelector('#pagination').hidden, 'Real pagination appears');
    }
    for (const width of [390, 1440]) {
      frame.width = width; await pause(100);
      assert(doc.documentElement.scrollWidth <= doc.documentElement.clientWidth + 1, 'Loaded page fits '+page+' '+width);
    }
    assert(win.LoadingPreview, 'Shared loading helper is available');
  }
  // Real cold navigations at each width catch responsive changes hidden by a
  // test that only resizes a chart after its data has already rendered.
  for (const width of [390, 1024]) {
    frame.width = width;
    const doc = await load('all.html', 'slow');
    await wait(() => doc.querySelector('#ml-authorship-chart .loading-preview'));
    const before = snapshot(doc);
    assert(doc.documentElement.scrollWidth <= doc.documentElement.clientWidth + 1, 'Loading page fits '+width);
    await wait(() => doc.readyState === 'complete' && !doc.querySelector('[data-loading-kind][aria-busy="true"]'));
    compare(before, doc, width);
    await checkFirstFrames(doc);
  }
  const doc = await load('people.html', 'fail');
  await wait(() => doc.readyState === 'complete' && doc.querySelector('[data-reload-page]'));
  assert(!doc.querySelector('.loading-preview'), 'A failed download removes the preview');
  assert(!doc.querySelector('#people-body tr[data-person-id]'), 'Failure never displays fake results');
  assert(doc.querySelector('#pagination').hidden, 'Failure never displays fake pagination');
  assert(doc.querySelector('#explorer-controls').inert, 'Unavailable filters stay inactive');
  const previous = frame.contentDocument;
  doc.querySelector('[data-reload-page]').click();
  await wait(() => frame.contentDocument !== previous && frame.contentDocument?.querySelector('#people-body tr[data-person-id]'));
  assert(!frame.contentDocument.querySelector('#explorer-controls').inert, 'Reload recovers the real explorer');
  return checks;
};
