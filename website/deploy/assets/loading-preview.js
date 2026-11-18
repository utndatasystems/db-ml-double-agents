(() => {
  'use strict';
  const pending = new Set();
  const stripe = (width = '70%') => `<span class="loading-stripe" style="width:${width}"></span>`;
  const bars = (count = 5) => `<div class="loading-bars">${Array.from({length: count}, (_, i) => `<div>${stripe('24%')}${stripe(`${[78, 63, 87, 48, 68][i % 5]}%`)}</div>`).join('')}</div>`;
  const graph = kind => kind === 'line'
    ? '<svg class="loading-preview-svg" viewBox="0 0 500 280" preserveAspectRatio="none"><path class="loading-grid" d="M25 60H480M25 130H480M25 200H480M25 250H480"/><path class="loading-curve" d="M25 218C100 218 105 170 170 178S260 125 320 136S400 72 480 58"/><path class="loading-curve loading-curve-secondary" d="M25 242C110 220 125 235 190 211S260 195 320 167S400 165 480 123"/></svg>'
    : `<div class="loading-columns">${[28, 42, 34, 56, 49, 64, 58, 78, 72, 89, 80, 96].map(height => `<i style="height:${height}%"></i>`).join('')}</div>`;
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const value = () => '<span class="loading-preview loading-value" aria-hidden="true" inert></span>';
  const fill = (index = 0) => `<i style="width:${[78,63,87,48,68][index % 5]}%"></i>`;
  function chartMarkup(target, labels) {
    // Use the actual chart classes: their row heights, tracks, typography and
    // responsive rules must be identical before and after the data arrives.
    if (target.classList.contains('viz-cohort-bars')) return labels.map((label, i) => `<div class="viz-cohort-row viz-chart-drilldown"><span class="viz-cohort-venue">${escape(label)}</span><span class="viz-cohort-track loading-preview loading-preview-art loading-flat" aria-hidden="true" inert>${fill(i)}</span><strong>${value()}</strong></div>`).join('');
    if (target.id === 'overlap-distribution' || target.id === 'focus-groups') {
      const focus = target.id === 'focus-groups';
      return labels.map((label, i) => {
        const name = `<span class="overlap-bar-label">${escape(label)}${focus ? `<small>${i ? 'Outside study' : 'Included'}</small>` : ''}</span>`;
        const number = `<span class="overlap-bar-value"><strong>${value()}</strong>${focus ? '' : `<small>${value()} people</small>`}</span>`;
        const action = content => focus && i === 3 ? `<button class="focus-row-action" type="button" inert>${content}</button>` : content;
        const tag = focus && i < 3 ? 'button' : 'div';
        return `<${tag}${tag === 'button' ? ' type="button" inert' : ''} class="overlap-bar-row${focus ? ` focus-group focus-group-${i}` : ''}">${action(name)}<span class="overlap-bar-track loading-preview loading-preview-art loading-flat" aria-hidden="true" inert>${fill(i)}</span>${action(number)}</${tag}>`;
      }).join('');
    }
    if (target.id === 'db-venue-rates') return labels.map((label, i) => `<div class="viz-rate-row viz-chart-drilldown"><span class="viz-rate-head"><strong>${escape(label)}</strong></span><span class="viz-rate-track loading-preview loading-preview-art loading-flat" aria-hidden="true" inert><span style="width:${[28,21,34,18,25][i % 5]}%"></span></span><strong class="viz-rate-value">${value()}</strong></div>`).join('');
    if (target.id === 'global-timeline') return `<div class="loading-preview loading-preview-art loading-flat loading-timeline" aria-hidden="true" inert>${graph('line')}</div>`;
    if (target.id === 'ml-participants-chart') return `<div class="participant-chart"><div class="publication-plot loading-preview loading-preview-art loading-flat" aria-hidden="true" inert><div class="publication-axis"></div><div class="publication-chart-main"><div class="publication-bars-area"><div class="publication-grid"></div>${graph('columns')}</div></div></div></div>`;
    if (target.id === 'ml-portfolio-chart') return `<div class="viz-histogram-scroll"><div class="viz-histogram loading-preview loading-preview-art loading-flat" style="--portfolio-bin-count:12" aria-hidden="true" inert>${Array.from({length:12}, (_, i) => `<div class="viz-histogram-bin"><strong>&nbsp;</strong><span class="viz-histogram-track"><i style="height:${[80,55,40,22,15,12][i % 6]}%"></i></span><span class="viz-histogram-label">&nbsp;</span></div>`).join('')}</div></div>`;
    if (target.id === 'ml-authorship-chart') return '<div class="viz-authorship-bar loading-preview loading-preview-art loading-flat" aria-hidden="true" inert><i class="viz-authorship-sole" style="width:15%"></i><i class="viz-authorship-first" style="width:25%"></i><i class="viz-authorship-other" style="width:40%"></i><i class="viz-authorship-last" style="width:20%"></i></div>';
    if (target.dataset.homeVenues !== undefined || target.dataset.homeMlVenues !== undefined) {
      const venues = target.dataset.homeVenues !== undefined ? ['SIGMOD/PODS','VLDB','ICDE','CIDR','EDBT'] : ['NeurIPS','ICML','ICLR'];
      return venues.map((venue, i) => `<div style="--venue-share:${[78,63,87,48,68][i]}%"><dt>${venue}</dt><dd>${value()}</dd></div>`).join('');
    }
    if (target.dataset.homeGrowthMetrics !== undefined) return ['NeurIPS','ICML','ICLR'].map(venue => `<div><dt><strong>${venue}</strong></dt><dd><span class="home-growth-value">${value()}</span><span class="home-growth-unit">/year</span></dd></div>`).join('');
    return null;
  }
  function markup(kind, rows = 5, labels = []) {
    if (kind === 'value') return '<span class="loading-preview loading-value" aria-hidden="true" inert></span>';
    if (kind === 'people') return Array.from({length: 7}, () => `<tr class="loading-preview loading-person-row" aria-hidden="true" inert>${[0, 1, 2, 3].map((_, index) => `<td><div class="loading-person-cell">${index === 3 ? graph('line') : stripe(index === 0 ? '72%' : '54%') + stripe('90%')}</div></td>`).join('')}</tr>`).join('');
    if (labels.length) return `<div class="loading-labeled-bars">${labels.map((label, index) => `<div><span>${escape(label)}</span><div class="loading-preview" aria-hidden="true" inert><div class="loading-preview-art">${stripe(`${[78,63,87,48,68][index % 5]}%`)}</div></div><span class="loading-preview loading-value" aria-hidden="true" inert></span></div>`).join('')}</div>`;
    if (kind === 'matrix') {
      const venues = ['SIGMOD/PODS', 'VLDB', 'ICDE', 'EDBT', 'CIDR'];
      const label = venue => venue === 'SIGMOD/PODS' ? '<span class="overlap-venue-token">SIGMOD</span><wbr>/PODS' : venue;
      return `<table class="overlap-matrix-table"><thead><tr><th class="overlap-matrix-corner"><span>From ↓</span><span>To →</span></th>${venues.map(v => `<th scope="col">${label(v)}</th>`).join('')}</tr></thead><tbody>${venues.map(v => `<tr><th scope="row"><span>${label(v)}</span><small>${value()} people</small></th>${venues.map(() => '<td><div class="overlap-matrix-cell loading-preview loading-preview-art loading-flat" aria-hidden="true" inert>&nbsp;</div></td>').join('')}</tr>`).join('')}</tbody></table>`;
    }
    if (kind === 'donut') return `<div class="viz-donut-center"><strong>${value()}</strong><span>of the DB cohort</span></div>`;
    const art = kind === 'donut' ? '<span class="loading-donut"></span>'
      : kind === 'bars' ? bars(rows)
        : kind === 'stack' ? '<div class="loading-stack"><i></i><i></i><i></i><i></i></div>' : graph(kind);
    return `<div class="loading-preview" aria-hidden="true" inert><div class="loading-preview-art">${art}</div><span class="loading-preview-label">Loading…</span></div>`;
  }
  function complete(target, reveal = true) {
    pending.delete(target);
    target.setAttribute('aria-busy', 'false');
    if (reveal) {
      const plots = target.hasAttribute('data-loading-shell')
        ? target.querySelectorAll('.publication-line-chart, .publication-plot, .viz-authorship-bar, .viz-histogram-track, .viz-rate-track, .overlap-bar-track') : [target];
      plots.forEach(plot => plot.classList.add('loading-content-ready'));
    }
  }
  function mount(target) {
    if (target.dataset.loadingMounted) return;
    target.dataset.loadingMounted = 'true';
    if (document.body?.classList.contains('home-page') && document.querySelector('[data-home-status]')?.hidden === false) {
      fail(target); return;
    }
    // This helper downloads in parallel. A cached data file may finish first;
    // in that case keep the real content rather than inserting a preview.
    const slot = target.querySelector('[data-loading-slot]') || target;
    const hasRealChild = slot.firstElementChild && !slot.firstElementChild.matches('.home-preview-note');
    const hasRealValue = target.dataset.loadingKind === 'value' && /\d/.test(target.textContent);
    const hasEmptyResults = target.dataset.loadingKind === 'people' && document.querySelector('#empty-state')?.hidden === false;
    if (hasRealChild || hasRealValue || hasEmptyResults) { complete(target, false); return; }
    target.setAttribute('aria-busy', 'true');
    const labels = target.dataset.loadingLabels ? JSON.parse(target.dataset.loadingLabels) : [];
    if (target.id === 'global-timeline' && new URLSearchParams(location.search).get('view') === 'publication-share') target.querySelector('.chart-axis-y').textContent = 'Share of venue’s papers (%)';
    slot.innerHTML = chartMarkup(target, labels) ?? markup(target.dataset.loadingKind, Number(target.dataset.loadingRows) || 5, labels);
    pending.add(target);
  }
  function fail(target) {
    const isTable = target.tagName === 'TBODY';
    const isValue = target.dataset.loadingKind === 'value';
    const error = '<span class="loading-error" role="status">Couldn’t load this data. <button type="button" data-reload-page>Try again</button></span>';
    const slot = target.querySelector('[data-loading-slot]') || target;
    slot.innerHTML = isTable ? `<tr><td colspan="4">${error}</td></tr>` : isValue ? '—' : error;
    complete(target);
  }
  function scan(node) {
    if (!(node instanceof Element)) return;
    const setInitialControl = control => {
      const view = new URLSearchParams(location.search).get('view');
      if (control.dataset.loadingScale) {
        const selected = control.dataset.loadingScale === (view === 'publication-share' ? 'relative' : 'absolute');
        control.classList.toggle('is-active', selected);
        control.setAttribute('aria-pressed', String(selected));
      } else if (control.dataset.loadingGroup === 'returning' && view === 'first-time-authors') {
        control.classList.add('is-off');
        control.setAttribute('aria-pressed', 'false');
      }
    };
    if (node.matches('[data-loading-scale], [data-loading-group]')) setInitialControl(node);
    node.querySelectorAll('[data-loading-scale], [data-loading-group]').forEach(setInitialControl);
    if (node.matches('[data-loading-kind]')) mount(node);
    node.querySelectorAll('[data-loading-kind]').forEach(mount);
  }
  let parsed = document.readyState !== 'loading';
  const observer = new MutationObserver(records => {
    records.forEach(record => record.addedNodes.forEach(scan));
    for (const target of pending) {
      if (!target.isConnected) pending.delete(target);
      else if (!target.querySelector('.loading-preview')) complete(target);
    }
    if (parsed && !pending.size) observer.disconnect();
  });
  observer.observe(document.documentElement, {childList: true, subtree: true});
  scan(document.documentElement);
  if (parsed && !pending.size) observer.disconnect();
  document.addEventListener('DOMContentLoaded', () => {
    parsed = true;
    if (!pending.size) observer.disconnect();
  });
  document.addEventListener('click', event => {
    if (event.target.closest('[data-reload-page]')) location.reload();
  });
  window.addEventListener('error', event => {
    if (!document.body?.classList.contains('home-page') && event.target instanceof HTMLScriptElement && /(?:site_data|app)\.js(?:\?|$)/.test(event.target.src)) {
      pending.forEach(fail);
    }
  }, true);
  function checkFailedStartup() {
    if (!document.body?.classList.contains('home-page') &&
        (!window.CROSSOVER_DATA || window.CROSSOVER_DATA.meta?.dataReady === false || window.CROSSOVER_DATA.browserServiceEvents)) pending.forEach(fail);
  }
  if (document.readyState === 'complete') checkFailedStartup();
  else window.addEventListener('load', checkFailedStartup, {once: true});
  window.LoadingPreview = {markup, fail, failPending: () => pending.forEach(fail)};
})();
