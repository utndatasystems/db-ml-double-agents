/* Browser checks against the exported data. Supply an iframe and website URL. */
async function runAllPresentationTests(frame, baseUrl) {
  let checks = 0;
  const assert = (condition, message) => { if (!condition) throw new Error(message); checks++; };
  const waitUntil = async (condition, message) => {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (condition()) return;
      // Poll on the tested document's task queue, alongside its resize work.
      // Headless virtual time can advance the parent ahead of its iframe.
      await new Promise(resolve => frame.contentWindow.setTimeout(resolve, 30));
    }
    throw new Error(message);
  };
  const load = relative => new Promise(resolve => {
    const url = new URL(relative, baseUrl);
    const ready = () => {
      if (frame.contentDocument.URL === url.href && frame.contentDocument.readyState === 'complete') {
        frame.removeEventListener('load', ready); resolve(frame.contentDocument);
      }
    };
    frame.addEventListener('load', ready); frame.src = url.href;
  });
  let doc = await load('all.html');
  const win = frame.contentWindow, data = win.CROSSOVER_DATA;
  assert(doc.documentElement.scrollWidth <= doc.documentElement.clientWidth + 1, 'All page fits the viewport');
  assert(doc.querySelector('#summary-title [data-provenance-metric="db_focus"]')&&!doc.querySelector('#summary-cards [data-provenance-metric="db_focus"]'), 'Population definition sits beside the main overview question');
  const yearRange=values=>{const years=[...new Set(values.map(Number).filter(y=>Number.isInteger(y)&&y>0))];return Math.min(...years)+'\u2013'+Math.max(...years);};
  const cohort=new Set(data.people.map(p=>p.dblpPid));
  const serviceRange=yearRange(data.populationFocus.candidates.filter(p=>cohort.has(p.dblpPid)).flatMap(p=>p.db.events.map(e=>e.year)));
  const mainRange=yearRange(data.people.flatMap(p=>p.ml.papers.filter(paper=>paper.scope==='main').map(paper=>paper.year)));
  const allPublicationRange=yearRange(data.people.flatMap(p=>p.ml.allPublicationYears));
  const overviewLabels=[...doc.querySelectorAll('#summary-cards .insights-data-label')].map(el=>el.textContent);
  assert(overviewLabels[0].includes(serviceRange)&&overviewLabels[0].includes('PC service'), 'PC count labels its actual service period');
  assert(overviewLabels[1].includes(mainRange)&&overviewLabels[1].includes('Main-conference papers'), 'Donut labels its actual publication history');
  assert(doc.querySelector('#matrix [data-insights-scope]').textContent.includes(serviceRange)&&doc.querySelector('#matrix [data-insights-scope]').textContent.includes(mainRange), 'Venue shares distinguish service years from publication years');
  assert(doc.querySelector('#ml-portfolio [data-insights-scope]').textContent.includes(allPublicationRange), 'Portfolio labels all recorded publication years');
  assert(doc.querySelector('#ml-authorship [data-insights-scope]').textContent.includes(mainRange), 'Author positions label their paper years');
  assert(doc.querySelector('#population-focus [data-insights-scope]').textContent.includes(serviceRange)&&doc.querySelector('#population-focus [data-insights-scope]').textContent.includes('All recorded publication years'), 'Classification distinguishes service period from publication history');
  for (const [metric, expected] of [
    ['db_focus', 'twice as many DB as ML'],
    ['db_service_overlap', 'row venue'],
    ['db_venue_participation', 'recorded publication history'],
    ['publication_activity', 'Annual shows one year'],
    ['ml_participants', 'not their first research paper'],
    ['crossover_authorship', 'researcher\u2013paper pair'],
  ]) {
    const button=doc.querySelector(`[data-provenance-metric="${metric}"]`);
    assert(button, 'Explanation is available for '+metric);
    assert(!button.closest('.eyebrow'), 'Explanation stays beside the main question for '+metric);
    button.dispatchEvent(new win.PointerEvent('pointerover',{bubbles:true,pointerType:'mouse'}));
    const panel=doc.querySelector(`.metric-context-panel[data-metric-id="${metric}"]`);
    assert(panel?.textContent.includes(expected), 'Hover explains the relevant definition for '+metric);
    assert(!panel.textContent.includes('Home'), 'Explanation describes this chart without comparing to Home');
    assert(button.getAttribute('aria-describedby')===panel.id, 'Explanation is accessible for '+metric);
    doc.dispatchEvent(new win.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
    assert(!doc.querySelector('.metric-context-panel')&&button.getAttribute('aria-expanded')==='false', 'Escape closes the explanation for '+metric);
  }
  const format = new Intl.NumberFormat('en-US');
  const count = () => Number(doc.querySelector('#results-count strong').textContent.replace(/\D/g, ''));
  const annual = new Map();
  for (const person of data.people) for (const paper of person.ml.papers) {
    if (paper.scope !== 'main') continue;
    const key = `${paper.venue}:${paper.year}`;
    if (!annual.has(key)) annual.set(key, new Set());
    annual.get(key).add(paper.workId || paper.canonicalDblpKey || paper.dblpKey);
  }
  for (const cumulative of [false, true]) {
    doc.querySelector(`[data-publication-view="${cumulative ? 'cumulative' : 'annual'}"]`).click();
    for (const relative of [false, true]) {
      doc.querySelector(`[data-publication-scale="${relative ? 'relative' : 'absolute'}"]`).click();
      const chart = doc.querySelector('#global-timeline');
      assert(doc.querySelector('#publication-timeline [data-insights-scope]').textContent.includes(mainRange)&&doc.querySelector('#publication-timeline [data-insights-scope]').textContent.startsWith(cumulative?'Cumulative':'Annual'), 'Publication scope follows the selected time view');
      assert(chart.querySelector('.publication-line-chart') && !chart.querySelector('.publication-column'), 'Publication activity uses lines in each mode');
      for (const element of [chart, chart.querySelector('.publication-chart-panel')]) {
        assert(element.scrollWidth <= element.clientWidth + 1 && element.scrollHeight <= element.clientHeight + 1, 'No internal chart overflow');
        const style = win.getComputedStyle(element);
        assert(!['auto', 'scroll'].includes(style.overflowX) && !['auto', 'scroll'].includes(style.overflowY), 'Chart has no scroll container');
      }
      for (const venue of ['NeurIPS', 'ICML', 'ICLR']) {
        const year = 2025, totals = data.mainMlVenueYearTotals[venue];
        const keys = cumulative ? [...annual.keys()].filter(k => k.startsWith(venue + ':') && Number(k.split(':')[1]) <= year) : [`${venue}:${year}`];
        const expected = keys.filter(k => !relative || totals[k.split(':')[1]] > 0).reduce((n, key) => n + (annual.get(key)?.size || 0), 0);
        const denominator = cumulative ? Object.entries(totals).filter(([y, n]) => Number(y) <= year && n > 0).reduce((n, [, value]) => n + value, 0) : totals[year];
        const point = chart.querySelector(`[data-publication-year="${year}"][data-publication-venue="${venue}"]`);
        assert(Number(point.dataset.publicationCount) === expected, 'Point uses observed paper counts: ' + venue);
        assert(Math.abs(Number(point.dataset.publicationValue) - (relative ? 100 * expected / denominator : expected)) < 1e-10, 'Point uses the correct annual/cumulative formula: ' + venue);
        assert(!point.hasAttribute('title') && !chart.querySelector('title'), 'No second native tooltip');
        point.dispatchEvent(new win.PointerEvent('pointerover', { bubbles: true, pointerType: 'mouse' }));
        const info = doc.querySelector('#publication-point-explanation');
        assert(info && info.textContent.includes(venue) && info.textContent.includes(format.format(expected)), 'Hover panel shows the actual value');
        assert(info.getBoundingClientRect().left >= 0 && info.getBoundingClientRect().right <= doc.documentElement.clientWidth + 1, 'Point tooltip fits the viewport');
        point.dispatchEvent(new win.KeyboardEvent('keydown', { bubbles: true, key: 'Enter' }));
        assert(doc.querySelector('#timeline-dialog').open && doc.querySelector('.timeline-dialog-summary').textContent === `${format.format(expected)} ${expected === 1 ? 'paper' : 'papers'}`, 'Keyboard activation opens exactly the plotted papers');
        doc.querySelector('#timeline-dialog-close').click();
      }
      assert(!chart.querySelector('[data-publication-year="2026"][data-publication-venue="NeurIPS"]'), 'Missing latest-year coverage stays a gap rather than zero');
    }
  }
  const visibleVenue = doc.querySelector('[data-timeline-venue="ICML"]');
  visibleVenue.click();
  assert(!doc.querySelector('#global-timeline [data-publication-venue="ICML"]'), 'Legend hides its line and points');
  visibleVenue.click();
  const valid = data.people.filter(p => p.ml.paperCount > 0);
  const maximum = Math.max(...valid.map(p => 100 * p.ml.mainPaperCount / p.ml.paperCount));
  const end = Math.min(100, Math.ceil(maximum) + 1);
  const bins = [...doc.querySelectorAll('[data-portfolio-bin]')];
  assert(bins.length === end + 1, 'Histogram ends just beyond the observed maximum');
  let assigned = 0;
  for (let upper = 0; upper <= end; upper++) {
    const expected = valid.filter(p => { const share = 100 * p.ml.mainPaperCount / p.ml.paperCount; return upper ? share > upper - 1 && share <= upper : share === 0; }).length;
    const bin = bins[upper];
    assigned += expected;
    assert(Number(bin.querySelector('strong').textContent.replace(/\D/g, '')) === expected, 'One-percentage-point bin count: ' + upper);
    assert(bin.querySelector('.viz-histogram-label').textContent === (upper ? `>${upper - 1}\u2013${upper}%` : '0%'), 'Every percentage interval is labeled');
  }
  assert(assigned === valid.length, 'Histogram assigns each valid study member exactly once');
  const sample=bins.find(bin=>Number(bin.querySelector('strong').textContent.replace(/\D/g,''))>0);
  const sampleCount=Number(sample.querySelector('strong').textContent.replace(/\D/g,''));
  await new Promise(resolve=>{frame.addEventListener('load',resolve,{once:true});sample.click();});doc=frame.contentDocument;
  assert(count()===sampleCount&&doc.querySelector('#people-title'),'Histogram opens its exact group in People Explorer');
  doc.querySelector('#reset-filters').click();
  const expectedIds = new Set(data.people.filter(p => p.focus.included && p.ml.papers.some(paper => paper.scope === 'main')).map(p => p.id));
  const pageSize = doc.querySelector('#page-size-filter');
  pageSize.value = '250'; pageSize.dispatchEvent(new win.Event('change', { bubbles: true }));
  const shownIds = [];
  const expectedPages = Math.ceil(expectedIds.size / Number(pageSize.value));
  for (let page = 0; page < expectedPages; page++) {
    const rows = [...doc.querySelectorAll('#people-body tr')];
    shownIds.push(...rows.map(row => row.dataset.personId));
    const remaining = expectedIds.size - page * Number(pageSize.value);
    assert(rows.length === Math.min(Number(pageSize.value), remaining), 'Explorer page contains the correct number of qualifying researchers');
    const next = doc.querySelector('#next-page');
    assert(next.disabled === (page === expectedPages - 1), 'Explorer pagination ends exactly at the last matching researcher');
    if (!next.disabled) next.click();
  }
  assert(count() === expectedIds.size && shownIds.length === expectedIds.size && new Set(shownIds).size === expectedIds.size && shownIds.every(id => expectedIds.has(id)), 'Explorer includes exactly the qualifying DB-focused ML publishers across all pages');
  doc.querySelector('[data-explorer-cohort="all"]').click();
  assert(count() === data.people.length + data.additionalExplorerPeople.length, 'All still exposes the broader PC population');
  doc=await load('all.html');
  const originalWidth = frame.style.width;
  frame.style.width = frame.clientWidth > 700 ? '390px' : '900px';
  frame.getBoundingClientRect();
  // A container resize must work without a manually dispatched window event.
  await waitUntil(() => doc.querySelector('.publication-line-chart').viewBox.baseVal.width === Math.max(240, doc.querySelector('#global-timeline').clientWidth), 'Chart did not follow its resized container');
  assert(doc.querySelector('.publication-line-chart').viewBox.baseVal.width === Math.max(240, doc.querySelector('#global-timeline').clientWidth), 'Line layout redraws after resize, including when page size is fixed');
  frame.style.width = originalWidth;
  frame.getBoundingClientRect();
  await waitUntil(() => doc.querySelector('.publication-line-chart').viewBox.baseVal.width === Math.max(240, doc.querySelector('#global-timeline').clientWidth), 'Chart did not follow its restored container');
  const timelineContainer = doc.querySelector('#global-timeline');
  const originalChartWidth = timelineContainer.style.width;
  let viewportResizeEvents = 0;
  const countViewportResize = () => { viewportResizeEvents++; };
  win.addEventListener('resize', countViewportResize);
  timelineContainer.style.width = `${timelineContainer.clientWidth - 48}px`;
  await waitUntil(() => doc.querySelector('.publication-line-chart').viewBox.baseVal.width === Math.max(240, timelineContainer.clientWidth), 'Chart did not follow a container-only layout change');
  assert(viewportResizeEvents === 0, 'Chart follows container changes without a window resize event');
  timelineContainer.style.width = originalChartWidth;
  await waitUntil(() => doc.querySelector('.publication-line-chart').viewBox.baseVal.width === Math.max(240, timelineContainer.clientWidth), 'Chart did not recover from a container-only layout change');
  win.removeEventListener('resize', countViewportResize);
  const headerSignature = d => ['.site-header', '.header-inner', '.brand', '.brand-copy strong', '.home-brand-mark', '.top-nav', '.top-nav a[aria-current]', '.top-nav-button'].map(selector => {
    const element = d.querySelector(selector), style = d.defaultView.getComputedStyle(element);
    return ['display', 'fontSize', 'letterSpacing', 'padding', 'gap', 'backgroundColor', 'borderRadius'].map(property => style[property]);
  });
  const allHeader = headerSignature(doc);
  doc = await load('index.html');
  assert(JSON.stringify(headerSignature(doc)) === JSON.stringify(allHeader), 'Home and All share navigation styling');
  return checks;
}
