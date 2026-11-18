(() => {
  'use strict';

  const number = new Intl.NumberFormat('en-US');
  const setText = (selector, value) => {
    document.querySelector(selector).textContent = value;
  };
  const validVenues = rows => Array.isArray(rows) && rows.length > 0 &&
    rows.every(row => typeof row.label === 'string' && Number.isInteger(row.count) && row.count >= 0);
  const svgElement = (tag, attributes = {}, content) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
    if (content !== undefined) element.textContent = content;
    return element;
  };
  const trends = window.DBML_HOME_TRENDS;
  const decimal = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
  const trendYears = 5;
  const percent = value => `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value)}%`;

  const tooltipLabel = content => [content.heading,
    ...(content.rows || []).map(row => `${row.label}: ${row.value}${row.count ? `, ${row.count}` : ''}${row.detail ? `, ${row.detail}` : ''}`),
    content.text].filter(Boolean).join('. ');

  function renderTooltip(tooltip, content) {
    const heading = document.createElement('strong');
    heading.className = 'home-tooltip-heading';
    heading.textContent = content.heading;
    tooltip.replaceChildren(heading);
    if (content.source) {
      heading.classList.add('home-tooltip-heading-with-source');
      const link = document.createElement('a');
      link.className = 'home-tooltip-source';
      link.href = content.source.url;
      link.textContent = content.source.label;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      tooltip.append(link);
    }
    (content.rows || []).forEach(row => {
      const line = document.createElement('div');
      line.className = 'home-tooltip-row';
      const label = document.createElement('span');
      label.textContent = row.label;
      const value = document.createElement('strong');
      value.textContent = row.value;
      if (row.count) {
        const count = document.createElement('span');
        count.className = 'home-tooltip-count';
        count.textContent = ` (${row.count})`;
        value.append(count);
      }
      line.append(label, value);
      if (row.detail) {
        const detail = document.createElement('span');
        detail.className = 'home-tooltip-detail';
        detail.textContent = row.detail;
        line.append(detail);
      }
      tooltip.append(line);
    });
    if (content.text) {
      const note = document.createElement('p');
      note.className = 'home-tooltip-note';
      note.textContent = content.text;
      tooltip.append(note);
    }
  }

  function bindGrowthInfo(button, content, index) {
    const tooltip = document.createElement('div');
    tooltip.id = `home-growth-info-${index}`;
    tooltip.className = 'home-focus-tooltip home-growth-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    renderTooltip(tooltip, content);
    tooltip.hidden = true;
    document.body.append(tooltip);
    button.setAttribute('aria-describedby', tooltip.id);
    let closeTimer;
    const close = () => { clearTimeout(closeTimer); tooltip.hidden = true; button.setAttribute('aria-expanded', 'false'); };
    const show = () => {
      clearTimeout(closeTimer);
      tooltip.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      const rect = button.getBoundingClientRect();
      tooltip.style.left = `${Math.max(12, Math.min(rect.left, document.documentElement.clientWidth - tooltip.offsetWidth - 12))}px`;
      tooltip.style.top = `${Math.max(12, Math.min(rect.bottom + 8, innerHeight - tooltip.offsetHeight - 12))}px`;
    };
    button.addEventListener('pointerenter', show);
    button.addEventListener('focus', show);
    button.addEventListener('click', show);
    const scheduleClose = () => {
      clearTimeout(closeTimer);
      closeTimer = setTimeout(() => {
        if (!tooltip.matches(':hover') && !tooltip.contains(document.activeElement) && document.activeElement !== button) close();
      }, 180);
    };
    button.addEventListener('pointerleave', scheduleClose);
    button.addEventListener('blur', scheduleClose);
    tooltip.addEventListener('pointerenter', () => clearTimeout(closeTimer));
    tooltip.addEventListener('pointerleave', scheduleClose);
    tooltip.addEventListener('focusin', () => clearTimeout(closeTimer));
    tooltip.addEventListener('focusout', scheduleClose);
    document.addEventListener('click', event => { if (!button.contains(event.target) && !tooltip.contains(event.target)) close(); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
    window.addEventListener('scroll', close, { passive: true });
    window.addEventListener('resize', close);
  }

  function growthInfoButton(label) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'home-growth-info';
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-expanded', 'false');
    const icon = svgElement('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    icon.append(svgElement('circle', { cx: 12, cy: 12, r: 9, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6 }),
      svgElement('path', { d: 'M12 10.5v6M12 7.3v.3', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round' }));
    button.append(icon);
    return button;
  }

  function renderGrowthMetrics(trend) {
    const container = document.querySelector('[data-home-growth-metrics]');
    container.replaceChildren();
    const comparison = trend.growthComparison;
    if (!comparison || !Number.isInteger(comparison.endYear)) return;
    const rates = [];
    trend.series.forEach((series, index) => {
      const startYear = comparison.ratios.find(row => row.venue === series.venue)?.startYear;
      const start = series.points.find(p => p.year === startYear);
      const end = series.points.find(p => p.year === comparison.endYear);
      const rate = trends.annualizedGrowth(start?.share, end?.share, comparison.endYear - startYear);
      rates.push(rate);
      const item = document.createElement('div');
      item.dataset.venue = series.venue;
      const name = document.createElement('dt');
      const venue = document.createElement('strong');
      venue.textContent = series.venue;
      name.append(venue);
      const value = document.createElement('dd');
      const metric = document.createElement('span');
      metric.className = 'home-growth-value';
      metric.textContent = rate === null ? '—' : `${rate > 0 ? '+ ' : ''}${decimal.format(rate * 100)}%`;
      if (rate !== null) {
        metric.dataset.cagr = rate;
        const button = growthInfoButton(`${series.venue}: how annualized growth is calculated`);
        metric.append(button);
        bindGrowthInfo(button, {
          heading: 'Compound annual growth rate of paper share',
          source: { label: 'Wikipedia: CAGR ↗', url: 'https://en.wikipedia.org/wiki/Compound_annual_growth_rate' },
          rows: [{ label: String(start.year), value: percent(start.share),
            count: `${number.format(start.papers)} of ${number.format(start.total)} papers` },
          { label: String(end.year), value: percent(end.share),
            count: `${number.format(end.papers)} of ${number.format(end.total)} papers` }],
          text: 'Annualized growth within the last 20 years, starting at the first available nonzero paper share. Venue totals account for conference growth.',
        }, index);
      }
      const unit = document.createElement('span');
      unit.className = 'home-growth-unit';
      unit.textContent = '/year';
      value.append(metric, unit);
      item.append(name, value);
      container.append(item);
    });
    if (!rates.length || rates.some(rate => rate === null || rate <= 0)) {
      setText('#home-trend-title', 'Senior DB researchers’ share of main-conference papers over time.');
    }
  }

  function linePath(points, x, y, value) {
    let path = '', connected = false;
    points.forEach(point => {
      const v = value(point);
      if (!Number.isFinite(v)) { connected = false; return; }
      path += `${connected ? 'L' : 'M'}${x(point.year)},${y(v)} `;
      connected = true;
    });
    return path;
  }

  function attachTooltip(mark, content, x, y, tooltip, width) {
    mark.setAttribute('aria-label', tooltipLabel(content));
    const show = () => {
      renderTooltip(tooltip, content);
      tooltip.hidden = false;
      tooltip.style.left = `${Math.max(0, Math.min(width - tooltip.offsetWidth, x - tooltip.offsetWidth / 2))}px`;
      tooltip.style.top = `${Math.max(0, y - tooltip.offsetHeight - 12)}px`;
    };
    mark.addEventListener('pointerenter', show);
    mark.addEventListener('click', show);
    mark.addEventListener('focus', show);
    mark.addEventListener('pointerleave', () => { tooltip.hidden = true; });
    mark.addEventListener('blur', () => { tooltip.hidden = true; });
  }

  function renderVenueCounts(selector, venues) {
    const maximum = Math.max(1, ...venues.map(row => row.count));
    const rows = venues.map(venue => {
      const row = document.createElement('div');
      const label = document.createElement('dt');
      const count = document.createElement('dd');
      label.textContent = venue.label;
      count.textContent = number.format(venue.count);
      row.style.setProperty('--venue-share', `${venue.count / maximum * 100}%`);
      row.append(label, count);
      return row;
    });
    document.querySelector(selector).replaceChildren(...rows);
  }

  function renderTrend(trend) {
    const container = document.querySelector('[data-home-trend-chart]');
    if (trend?.series?.length) trend = trends.publicationWindow(trend);
    if (!trend?.series?.length) {
      container.textContent = 'Publication trend is currently unavailable.';
      return;
    }
    renderGrowthMetrics(trend);
    const colors = { NeurIPS: 'var(--ml-neurips)', ICML: 'var(--ml-icml)', ICLR: 'var(--ml-iclr)' };
    const smoothed = trend.series.map(series => ({ ...series,
      points: trends.paperShares(series.points, trendYears)
        .map((point, index) => ({ ...point, annual: series.points[index] }))
        .filter(point => point.year >= trend.startYear),
    }));
    function draw() {
      const width = Math.max(260, container.clientWidth);
      const height = width < 600 ? 280 : 320;
      const margin = { left: 44, right: 18, top: 18, bottom: 44 };
      const ceiling = 2, step = 0.5;
      const x = year => margin.left + (year - trend.startYear) / Math.max(1, trend.endYear - trend.startYear) * (width - margin.left - margin.right);
      const y = value => height - margin.bottom - value / ceiling * (height - margin.top - margin.bottom);
      const svg = svgElement('svg', {
        viewBox: `0 0 ${width} ${height}`, role: 'group',
        'aria-label': `Five-year trends in main-conference paper shares involving senior DB researchers, ${trend.startYear} to ${trend.endYear}. Each point pools up to five years, using the available years at the beginning. Missing totals remain gaps.`,
      });
      for (let tick = 0; tick <= ceiling + step / 10; tick += step) {
        svg.append(svgElement('line', { x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick), class: 'home-trend-grid' }));
        svg.append(svgElement('text', { x: margin.left - 10, y: y(tick) + 4, 'text-anchor': 'end', class: 'home-trend-tick' }, percent(tick)));
      }
      const tickYears = Array.from({ length: trend.endYear - trend.startYear + 1 }, (_, i) => trend.startYear + i);
      const minimumSpacing = (trend.endYear - trend.startYear) /
        Math.max(1, Math.floor((width - margin.left - margin.right) / 45));
      const spacing = [1, 2, 5, 10, 20, 50].find(value => value >= minimumSpacing) || 50;
      tickYears.filter((year, index) => index === 0 || index === tickYears.length - 1 ||
        (index % spacing === 0 && x(trend.endYear) - x(year) >= 40)).forEach(year => {
        svg.append(svgElement('text', { x: x(year), y: height - 19, 'text-anchor': 'middle', class: 'home-trend-tick' }, year));
      });
      svg.append(svgElement('text', { x: width / 2, y: height - 1, 'text-anchor': 'middle', class: 'home-trend-tick' }, 'Publication year'));
      const tooltip = document.createElement('div');
      tooltip.className = 'home-trend-tooltip';
      tooltip.hidden = true;
      tooltip.setAttribute('aria-hidden', 'true');
      const trendLayer = svgElement('g', { class: 'home-smoothed-data' });
      smoothed.forEach(series => {
        trendLayer.append(svgElement('path', { d: linePath(series.points, x, y, p => p.value),
          stroke: colors[series.venue], class: 'home-trend-line', 'aria-hidden': 'true' }));
        series.points.filter(p => p.value !== null).forEach(point => {
          const content = {
            heading: series.venue,
            rows: [
              { label: `${point.year} annual share`, value: percent(point.annual.share),
                detail: `${number.format(point.annual.papers)} of ${number.format(point.annual.total)} papers` },
              { label: `${point.year - point.startYear + 1}-year pooled trend`, value: percent(point.value),
                detail: `${point.startYear}–${point.year}: ${number.format(point.numerator / 100)} of ${number.format(point.denominator)} papers` },
            ],
          };
          const dot = svgElement('circle', { cx: x(point.year), cy: y(point.value), r: 3,
            fill: colors[series.venue], class: 'home-trend-dot', tabindex: 0, role: 'img',
            'data-year': point.year, 'data-trend-value': point.value });
          attachTooltip(dot, content, x(point.year), y(point.value), tooltip, width);
          trendLayer.append(dot);
        });
      });
      svg.append(trendLayer);
      container.replaceChildren(svg, tooltip);
    }
    draw();
    if ('ResizeObserver' in window) new ResizeObserver(draw).observe(container);
    else window.addEventListener('resize', draw);
  }

  function renderFirstTimers(data) {
    const container = document.querySelector('[data-home-new-chart]');
    if (!data?.points?.length) {
      container.textContent = 'First-time author data is currently unavailable.';
      return;
    }
    const metric = document.querySelector('[data-home-new-growth]');
    const first = data.points[0], last = data.points[data.points.length - 1];
    const curve = trends.exponentialTrend(data.points);
    const rate = curve?.rate ?? null;
    if (rate !== null) {
      const magnitude = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 0 }).format(Math.abs(rate));
      metric.textContent = rate > 0 ? `+ ${magnitude}` : magnitude;
      setText('[data-home-new-growth-direction]', rate > 0 ? 'grew' : rate < 0 ? 'fell' : 'changed');
      const button = growthInfoButton('How is first-time author growth calculated?');
      metric.append(button);
      bindGrowthInfo(button, {
        heading: 'First-time author growth',
        rows: [{ label: 'Period', value: `${first.year}–${last.year}` },
          { label: 'Fitted annual growth', value: metric.textContent }],
        text: 'Each senior DB researcher counts once, in the year of their first recorded main-conference paper across these three venues. The exponential curve fits all yearly counts by minimizing squared gaps to the bars.',
      }, 'newcomers');
    } else {
      setText('#home-newcomers-title', `Senior DB researchers publishing their first recorded main-conference paper at NeurIPS, ICML or ICLR, ${data.startYear}–${data.endYear}.`);
    }
    function draw() {
      const width = Math.max(260, container.clientWidth), height = 280;
      const margin = { left: 34, right: 14, top: 24, bottom: 44 };
      const maximum = Math.max(1, ...data.points.map(point => point.firstTime), ...(curve?.points.map(p => p.value) || []));
      const step = Math.max(1, Math.ceil(maximum / 3 / 5) * 5);
      const ceiling = Math.ceil(maximum / step) * step;
      const y = value => height - margin.bottom - value / ceiling * (height - margin.top - margin.bottom);
      const slot = (width - margin.left - margin.right) / data.points.length;
      const xCenter = year => margin.left + (year - data.startYear + 0.5) * slot;
      const svg = svgElement('svg', { viewBox: `0 0 ${width} ${height}`, role: 'group',
        'aria-label': `First-time authors among senior DB researchers at NeurIPS, ICML or ICLR, ${data.startYear} to ${data.endYear}. ${curve ? 'The filled curve is an exponential trend fitted to all annual counts. ' : ''}Grey bars show actual yearly counts.` });
      for (let tick = 0; tick <= ceiling; tick += step) {
        svg.append(svgElement('line', { x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick), class: 'home-trend-grid' }));
        svg.append(svgElement('text', { x: margin.left - 8, y: y(tick) + 4, 'text-anchor': 'end', class: 'home-trend-tick' }, tick));
      }
      const tooltip = document.createElement('div');
      tooltip.className = 'home-trend-tooltip';
      tooltip.hidden = true;
      tooltip.setAttribute('aria-hidden', 'true');
      const rawLayer = svgElement('g', { class: 'home-observed-bars', 'data-observed-layer': '' });
      data.points.forEach((point, index) => {
        const x = margin.left + index * slot + slot * 0.2;
        const barWidth = slot * 0.6;
        if (point.firstTime > 0) {
          const content = { heading: String(point.year), rows: [{ label: 'Observed', value: `${number.format(point.firstTime)} people` }] };
          const rect = svgElement('rect', { x, y: y(point.firstTime), width: barWidth,
            height: y(0) - y(point.firstTime), rx: 1.5, class: 'home-newcomers-bar',
            tabindex: 0, role: 'img' });
          attachTooltip(rect, content, xCenter(point.year), y(point.firstTime), tooltip, width);
          rawLayer.append(rect);
        }
        const last = index === data.points.length - 1;
        const labelStep = Math.max(1, Math.ceil(48 / slot));
        if (last || (index % labelStep === 0 && data.points.length - 1 - index >= labelStep)) {
          svg.append(svgElement('text', { x: x + barWidth / 2, y: height - 20, 'text-anchor': 'middle', class: 'home-trend-tick' }, point.year));
        }
      });
      svg.append(rawLayer);
      if (curve) {
        const trendLayer = svgElement('g', { class: 'home-fitted-data' });
        const path = linePath(curve.samples, xCenter, y, p => p.value);
        trendLayer.append(svgElement('path', { d: `${path}L${xCenter(last.year)},${y(0)} L${xCenter(first.year)},${y(0)} Z`,
          class: 'home-newcomers-area', 'aria-hidden': 'true' }));
        trendLayer.append(svgElement('path', { d: path,
          class: 'home-trend-line home-newcomers-trend', 'aria-hidden': 'true' }));
        curve.points.forEach((point, index) => {
          const content = { heading: String(point.year), rows: [
            { label: 'Fitted trend', value: `${decimal.format(point.value)} people` },
            { label: 'Observed', value: `${number.format(data.points[index].firstTime)} people` },
          ] };
          const dot = svgElement('circle', { cx: xCenter(point.year), cy: y(point.value), r: 3.5,
            class: 'home-newcomers-trend-dot', tabindex: 0, role: 'img',
            'data-year': point.year, 'data-trend-value': point.value });
          attachTooltip(dot, content, xCenter(point.year), y(point.value), tooltip, width);
          trendLayer.append(dot);
        });
        svg.append(trendLayer);
      }
      // Keep the last observed count independent of the fitted curve.
      svg.append(svgElement('text', { x: xCenter(last.year), y: y(Math.max(last.firstTime, curve?.points.at(-1).value || 0)) - 12,
        'text-anchor': 'middle', class: 'home-newcomers-bar-label' }, number.format(last.firstTime)));
      svg.append(svgElement('text', { x: width / 2, y: height - 1, 'text-anchor': 'middle', class: 'home-trend-tick' }, 'First publication year'));
      container.replaceChildren(svg, tooltip);
    }
    draw();
    if ('ResizeObserver' in window) new ResizeObserver(draw).observe(container);
    else window.addEventListener('resize', draw);
  }

  async function loadPopulation() {
    try {
      const response = await fetch('data/home_summary.json', { cache: 'no-cache' });
      if (!response.ok) throw new Error('Population data unavailable');
      const summary = await response.json();
      if (!Number.isInteger(summary.total) || summary.total < 0 ||
          !Number.isInteger(summary.withMainMlPaper) || summary.withMainMlPaper < 0 ||
          summary.withMainMlPaper > summary.total ||
          !Number.isInteger(summary.startYear) || !Number.isInteger(summary.endYear) ||
          !validVenues(summary.venues) || !validVenues(summary.mlVenues)) {
        throw new Error('Invalid population data');
      }
      renderVenueCounts('[data-home-venues]', summary.venues);
      renderVenueCounts('[data-home-ml-venues]', summary.mlVenues);
      setText('[data-home-total]', number.format(summary.total));
      setText('[data-home-years]', `${summary.startYear}–${summary.endYear}`);
      const mlYears = summary.mlPublicationYears;
      setText('[data-home-ml-years]', Number.isInteger(mlYears?.startYear) && Number.isInteger(mlYears?.endYear)
        ? (mlYears.startYear === mlYears.endYear ? mlYears.startYear : `${mlYears.startYear}–${mlYears.endYear}`) : '—');
      setText('[data-home-year-start]', summary.startYear);
      setText('[data-home-year-end]', summary.endYear);
      const share = summary.total ? summary.withMainMlPaper / summary.total : null;
      const percentage = share === null ? '—' : new Intl.NumberFormat('en-US', {
        style: 'percent', maximumFractionDigits: 0,
      }).format(share);
      setText('[data-home-ml-percentage]', percentage);
      renderTrend(summary.publicationTrend);
      renderFirstTimers(summary.firstTimePublishers);
    } catch {
      window.LoadingPreview?.failPending();
      const status = document.querySelector('[data-home-status]');
      status.textContent = 'Counts are temporarily unavailable.';
      status.hidden = false;
    }
  }

  if ('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    document.body.classList.add('home-motion');
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.08 });
    document.querySelectorAll('[data-home-reveal]').forEach(section => observer.observe(section));
  }

  const focusButton = document.querySelector('.home-focus-info');
  const focusTooltip = document.querySelector('#home-focus-tooltip');
  let focusCloseTimer;
  const closeFocus = () => { focusTooltip.hidden = true; focusButton.setAttribute('aria-expanded', 'false'); };
  const showFocus = () => {
    clearTimeout(focusCloseTimer);
    focusTooltip.hidden = false; focusButton.setAttribute('aria-expanded', 'true');
    const rect = focusButton.getBoundingClientRect();
    focusTooltip.style.left = `${Math.max(12, Math.min(rect.left, document.documentElement.clientWidth - focusTooltip.offsetWidth - 12))}px`;
    focusTooltip.style.top = `${Math.max(12, Math.min(rect.bottom + 8, innerHeight - focusTooltip.offsetHeight - 12))}px`;
  };
  const scheduleFocusClose = () => { clearTimeout(focusCloseTimer); focusCloseTimer = setTimeout(() => { if (!focusTooltip.matches(':hover') && !focusTooltip.contains(document.activeElement) && document.activeElement !== focusButton) closeFocus(); }, 180); };
  focusButton.addEventListener('pointerenter', showFocus); focusButton.addEventListener('focus', showFocus);
  focusButton.addEventListener('pointerleave', scheduleFocusClose); focusButton.addEventListener('blur', scheduleFocusClose);
  focusButton.addEventListener('click', showFocus);
  focusTooltip.addEventListener('pointerenter', () => clearTimeout(focusCloseTimer)); focusTooltip.addEventListener('pointerleave', scheduleFocusClose); focusTooltip.addEventListener('focusout', scheduleFocusClose);
  document.addEventListener('click', event => { if (!focusButton.contains(event.target) && !focusTooltip.contains(event.target)) closeFocus(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeFocus(); });
  window.addEventListener('scroll', closeFocus, {passive: true});
  window.addEventListener('resize', closeFocus);
  loadPopulation();
})();
