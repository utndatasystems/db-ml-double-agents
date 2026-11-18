/* Browser integration checks. Supply an iframe and the served website directory. */
(function (root) {
  'use strict';
  root.runAllHomeLinkTests = async function (frame, baseUrl) {
    let checks = 0;
    const assert = (condition, message) => { if (!condition) throw new Error(message); checks++; };
    const participationFits = doc => {
      const chart = doc.querySelector('#ml-participants-chart');
      for (const element of [chart, chart.querySelector('.participant-chart'), chart.querySelector('.publication-plot')]) {
        assert(element.scrollWidth <= element.clientWidth + 1 && element.scrollHeight <= element.clientHeight + 1, 'Participation fits without horizontal or vertical overflow');
        const style = doc.defaultView.getComputedStyle(element);
        assert(!['auto', 'scroll'].includes(style.overflowX) && !['auto', 'scroll'].includes(style.overflowY), 'Participation has no internal scroll container');
      }
      const labels = [...chart.querySelectorAll('.publication-year-label')].filter(el => doc.defaultView.getComputedStyle(el).visibility !== 'hidden');
      assert(labels.every((label, i) => !i || labels[i - 1].getBoundingClientRect().right <= label.getBoundingClientRect().left), 'Visible year labels do not overlap');
      const bounds = chart.getBoundingClientRect();
      assert(labels.every(label => label.getBoundingClientRect().left >= bounds.left && label.getBoundingClientRect().right <= bounds.right), 'End-year labels stay inside the chart');
    };
    const number = text => Number(text.replace(/[^\d.-]/g, ''));
    const pause = () => new Promise(resolve => setTimeout(resolve, 50));
    const load = async relative => {
      const url = new URL(relative, baseUrl);
      await new Promise(resolve => {
        const ready = () => {
          if (frame.contentDocument.URL === url.href && frame.contentDocument.readyState === 'complete') {
            frame.removeEventListener('load', ready); resolve();
          }
        };
        frame.addEventListener('load', ready); frame.src = url.href;
      });
      for (let i = 0; i < 600; i++) {
        const doc = frame.contentDocument;
        const ready = relative.startsWith('index.html')
          ? doc?.querySelector('[data-home-new-growth] .home-growth-info')
          : relative.startsWith('people.html') ? doc?.querySelector('#people-body tr') : doc?.querySelector('#global-timeline .publication-line-chart');
        if (frame.contentWindow.location.href === url.href && ready) return doc;
        await pause();
      }
      throw new Error('Page did not finish loading: ' + relative);
    };
    const home = await (await fetch(new URL('data/home_summary.json', baseUrl))).json();
    const homeDoc = new DOMParser().parseFromString(await (await fetch(new URL('index.html', baseUrl))).text(), 'text/html');
    const homeLink = hash => [...homeDoc.querySelectorAll('a[href]')].find(a => a.getAttribute('href').endsWith(hash)).getAttribute('href');
    let doc = await load('all.html');
    assert(doc.querySelector('[data-publication-scale="absolute"]').getAttribute('aria-pressed') === 'true', 'Direct All keeps Absolute by default');
    assert(doc.querySelector('[data-participant-group="returning"]').getAttribute('aria-pressed') === 'true', 'Direct All keeps both author groups');
    participationFits(doc);
    const defaultData = frame.contentWindow.CROSSOVER_DATA;
    assert(defaultData.people.length === home.total, 'Home and All use the same cohort');
    for (const venue of home.venues) {
      const row = doc.querySelector(`[data-service-drilldown="${venue.venue}"]`);
      assert(number(row.querySelector('strong').textContent) === venue.count, 'PC roster matches Home: ' + venue.venue);
      await new Promise(resolve=>{frame.addEventListener('load',resolve,{once:true});row.click();});doc=frame.contentDocument;
      assert(number(doc.querySelector('#results-count strong').textContent) === venue.count, 'PC roster drilldown matches displayed count: ' + venue.venue);
      doc=await load('all.html');
    }
    doc.querySelector('[data-db-rates-view="absolute"]').click();
    for (const venue of home.venues) {
      doc.querySelector('[data-db-rates-view="absolute"]').click();
      const row = doc.querySelector(`[data-venue-ml-drilldown="${venue.venue}"]`);
      assert(number(row.querySelector('.viz-rate-denominator').textContent) === venue.count, 'Venue denominator matches Home: ' + venue.venue);
      const expected = number(row.querySelector('.viz-rate-numerator').textContent);
      await new Promise(resolve=>{frame.addEventListener('load',resolve,{once:true});row.click();});doc=frame.contentDocument;
      assert(number(doc.querySelector('#results-count strong').textContent) === expected, 'Venue ML drilldown matches numerator: ' + venue.venue);
      doc=await load('all.html');
    }
    doc = await load(homeLink('#publication-timeline'));
    assert(doc.querySelector('[data-publication-scale="relative"]').getAttribute('aria-pressed') === 'true', 'Momentum link opens Relative');
    assert(doc.querySelector('[data-publication-view="annual"]').getAttribute('aria-pressed') === 'true', 'Momentum link opens Annual');
    const endYear = home.publicationTrend.endYear;
    const format = new Intl.NumberFormat('en-US');
    for (const series of home.publicationTrend.series) {
      const point = series.points.find(p => p.year === endYear);
      const marker = doc.querySelector(`[data-publication-year="${endYear}"][data-publication-venue="${series.venue}"]`);
      assert(Number(marker.dataset.publicationCount) === point.papers && Number(marker.dataset.publicationTotal) === point.total, 'Annual numerator and denominator match Home: ' + series.venue);
    }
    doc = await load(homeLink('#ml-participants'));
    assert(doc.querySelector('[data-participant-group="first"]').getAttribute('aria-pressed') === 'true', 'Your move selects first-time authors');
    assert(doc.querySelector('[data-participant-group="returning"]').getAttribute('aria-pressed') === 'false', 'Your move hides previously published authors');
    assert(doc.querySelector('[data-participant-period="home"]').getAttribute('aria-pressed') === 'true', 'Your move selects Home period');
    const groups = [...doc.querySelectorAll('#ml-participants-chart .publication-year-group')];
    assert(doc.querySelector('#ml-participants [data-insights-scope]').textContent.includes(home.firstTimePublishers.startYear+'\u2013'+home.firstTimePublishers.endYear), 'Participation scope follows the selected Home period');
    assert(groups.length === home.firstTimePublishers.points.length, 'Home period has the same number of years');
    for (const point of home.firstTimePublishers.points) {
      const group = groups.find(g => number(g.querySelector('.publication-year-label').textContent) === point.year);
      assert(group && number(group.querySelector('.publication-year-total').textContent) === point.firstTime, 'First-time count matches Home without resetting history: ' + point.year);
    }
    participationFits(doc);
    doc.querySelector('[data-participant-period="all"]').click();
    participationFits(doc);
    const allYears=[...doc.querySelectorAll('#ml-participants-chart .publication-year-label')].map(el=>Number(el.textContent));
    assert(doc.querySelector('#ml-participants [data-insights-scope]').textContent.includes(Math.min(...allYears)+'\u2013'+Math.max(...allYears)), 'Participation scope updates to the full displayed period');
    assert(doc.querySelectorAll('#ml-participants-chart .publication-year-group').length >= groups.length, 'Full history remains available');
    const last = home.firstTimePublishers.points.at(-1);
    await new Promise(resolve=>{frame.addEventListener('load',resolve,{once:true});doc.querySelector(`[data-participants-year="${last.year}"][data-participants-kind="first"]`).click();});doc=frame.contentDocument;
    assert(number(doc.querySelector('#results-count strong').textContent) === last.firstTime, 'First-time drilldown matches Home');
    doc = await load('people.html');
    doc.querySelector('[data-explorer-cohort="all"]').click();
    assert(number(doc.querySelector('#results-count strong').textContent) === defaultData.people.length + defaultData.additionalExplorerPeople.length, 'All still exposes excluded PC profiles');
    const excluded = defaultData.additionalExplorerPeople.find(p => p.focus.label === 'ML-focused');
    const search = doc.querySelector('#search-input');
    search.value = excluded.name; search.dispatchEvent(new Event('input', { bubbles: true }));
    assert([...doc.querySelectorAll('#people-body tr')].some(row => row.dataset.personId === excluded.id && row.querySelector('.focus-badge')), 'Excluded profile stays discoverable with a badge');
    doc = await load('index.html');
    doc.querySelector('[data-open-methodology]').click();
    for (let i = 0; i < 300 && !doc.querySelector('#methodology-dialog').open; i++) await pause();
    const methods = doc.querySelector('#methodology-dialog-content').textContent;
    assert(methods.includes('Home pools papers') && methods.includes('CAGR'), 'Shared methodology explains Home paper-share trends');
    assert(methods.includes('exponential curve')&&methods.includes('annual counts'), 'Shared methodology explains fitted author growth');
    const methodology=doc.querySelector('#methodology-dialog-content');
    const card=id=>methodology.querySelector(`[data-methodology-metric="${id}"]`)?.textContent||'';
    const methodologyGroups=[...methodology.querySelectorAll('[data-methodology-page]')];
    assert(methodologyGroups.map(group=>group.dataset.methodologyPage).join('|')==='shared|home|insights', 'Shared rules precede separate Home and All Insights explanations');
    assert(methodology.querySelector('[data-methodology-metric="db_ml_researchers"]').closest('[data-methodology-page]').dataset.methodologyPage==='shared', 'Both pages share the cohort and counting rules');
    for (const id of ['home_community_counts', 'home_publication_trend', 'home_first_time_authors']) assert(methodology.querySelector(`[data-methodology-metric="${id}"]`).closest('[data-methodology-page]').dataset.methodologyPage==='home', 'Home explanation is in its own group: '+id);
    assert(methodology.querySelector('[data-methodology-metric="ml_participants"]').closest('[data-methodology-page]').dataset.methodologyPage==='insights', 'Annual participation belongs to All Insights');
    assert(methodology.querySelector('.methodology-scope').textContent.includes(home.startYear+'\u2013'+home.endYear), 'Methodology uses the actual qualifying service years');
    assert(methods.includes('research, industry and demo PCs')&&methods.includes('review boards')&&methods.includes('Shadow PCs'), 'Population explains included and excluded service roles');
    assert(methods.includes('Membership does not confirm completed reviews'), 'Committee membership is distinguished from completed reviewing');
    assert(methods.includes('People Explorer\u2019s All option also includes matched PC members who do not qualify'), 'Methodology explains the wider searchable population');
    assert(methods.includes('unverified paper lengths are excluded'), 'Focus definition explains missing DB-paper length evidence');
    assert(card('db_service_counts').includes('all recorded roles')&&card('db_service_counts').includes('highest number'), 'Venue-count definition reflects defaults and same-year grouping');
    assert(card('db_service_overlap').includes('row venue')&&card('db_service_overlap').includes('denominator remains the full row group'), 'Matrix definition explains its directional denominator');
    const homeTrend=card('home_publication_trend');
    assert(homeTrend.includes('20 years before the current year')&&homeTrend.includes('first nonzero annual share within that window'), 'CAGR definition follows the rolling window and positive baseline');
    assert(homeTrend.includes('not the smoothed line')&&!methods.includes('from each venue\u2019s first qualifying paper year'), 'Outdated all-history CAGR explanation is removed');
    assert(card('home_first_time_authors').includes('minimizing squared errors in counts')&&card('home_first_time_authors').includes('Grey bars show observed counts'), 'Home explains its fitted curve and observed counts');
    assert(!card('ml_participants').includes('exponential'), 'All Insights participation does not mix in Home fitting rules');
    assert(card('crossover_authorship').includes('two authorships')&&card('crossover_authorship').includes('valid author position or total author count'), 'Author positions define their counting unit and exclusions');
    assert(card('ml_portfolio').includes('Other publication types remain in the denominator'), 'Publication share retains contextual works in its denominator');
    assert(methods.includes('forthcoming records')&&methods.includes('publication year is complete'), 'Snapshot and partial-year limitations are clear');
    assert(methods.includes('each stage total 100%')&&methods.includes('names missed by the scrapers are not measured'), 'Coverage explanation defines its denominator and extraction limit');
    return checks;
  };
})(window);
