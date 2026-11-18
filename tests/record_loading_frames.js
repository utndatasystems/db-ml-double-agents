// Test-server instrumentation, installed before the preview helper. Production
// pages do not load this file. Observe every frame, not only the ready preview.
(() => {
  const selectors = document.location.pathname.endsWith('/all.html')
    ? ['.viz-donut', '.viz-cohort-bars', '#overlap-distribution', '#overlap-matrix', '#focus-groups', '#db-venue-rates', '#global-timeline', '#ml-participants-chart', '#ml-portfolio-chart', '#ml-authorship-chart']
    : ['[data-home-venues]', '[data-home-ml-venues]', '[data-home-growth-metrics]', '[data-home-trend-chart]', '[data-home-new-chart]'];
  const audit = window.loadingFrameAudit = {samples: [], done: false};
  let settledFrames = 0;
  function record() {
    if ([...document.querySelectorAll('link[rel="stylesheet"]')].every(link => link.sheet)) {
      const charts = {};
      for (const selector of selectors) {
        const node = document.querySelector(selector);
        if (!node) continue;
        const bounds = node.getBoundingClientRect();
        charts[selector] = {width: bounds.width, height: bounds.height};
      }
      if (Object.keys(charts).length) audit.samples.push(charts);
    }
    const ready = document.readyState === 'complete' && !document.querySelector('[data-loading-kind][aria-busy="true"]');
    settledFrames = ready ? settledFrames + 1 : 0;
    if (settledFrames >= 4 || performance.now() > 15000) audit.done = true;
    else requestAnimationFrame(record);
  }
  requestAnimationFrame(record);
})();
