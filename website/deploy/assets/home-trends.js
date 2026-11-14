/* Read-only chart transforms; observed data stays unchanged. */
(function (root) {
  'use strict';
  function trailingAverage(points, numerator, denominator, period = 3) {
    if (!Number.isInteger(period) || period < 1) throw new Error('Invalid averaging period');
    return points.map((point, index) => {
      const window = points.slice(Math.max(0, index - period + 1), index + 1);
      // Use the available history at the start; never fill missing years or
      // denominators inside a window, and never borrow future observations.
      const complete = window.every((p, i) =>
        Number.isInteger(p.year) && p.year === point.year - window.length + i + 1 &&
        Number.isFinite(numerator(p)) && numerator(p) >= 0 &&
        Number.isFinite(denominator(p)) && denominator(p) > 0);
      if (!complete) return { year: point.year, value: null };
      const sum = window.reduce((total, p) => total + numerator(p), 0);
      const weight = window.reduce((total, p) => total + denominator(p), 0);
      return { year: point.year, startYear: window[0].year, value: sum / weight,
        numerator: sum, denominator: weight };
    });
  }
  function annualizedGrowth(start, end, years) {
    if (!Number.isFinite(start) || start <= 0 || !Number.isFinite(end) || end < 0 ||
        !Number.isFinite(years) || years <= 0) return null;
    return Math.pow(end / start, 1 / years) - 1;
  }
  function publicationWindow(trend, currentYear = new Date().getFullYear()) {
    const startYear = currentYear - 20;
    const endYear = Math.min(currentYear, trend.endYear);
    if (!Number.isInteger(endYear) || endYear < startYear) {
      return { ...trend, startYear, endYear: null, series: [], growthComparison: null };
    }
    // Keep earlier observations for smoothing the first visible point.
    const series = trend.series.map(item => ({ ...item,
      points: item.points.filter(point => point.year <= endYear),
    }));
    const ratios = series.map(item => {
      const first = item.points.find(point => point.year >= startYear &&
        Number.isFinite(point.share) && point.share > 0);
      const last = item.points.find(point => point.year === endYear);
      const ratio = first && first.year < endYear && Number.isFinite(last?.share)
        ? last.share / first.share : null;
      return { venue: item.venue, startYear: first?.year ?? null, ratio };
    });
    return { ...trend, startYear, endYear, series, growthComparison: { endYear, ratios } };
  }
  function exponentialTrend(points, samplesPerYear = 12) {
    if (!Number.isInteger(samplesPerYear) || samplesPerYear < 1) throw new Error('Invalid sampling rate');
    if (points.length < 3 || points.some((p, i) => !Number.isInteger(p.year) ||
        !Number.isFinite(p.firstTime) || p.firstTime < 0 || (i > 0 && p.year <= points[i - 1].year))) return null;
    const first = points[0], last = points[points.length - 1], years = last.year - first.year;
    if (!points.some(p => p.firstTime > 0)) return null;
    const xs = points.map(p => (p.year - first.year) / years);
    // For each exponent, solve the amplitude exactly, minimizing squared errors in counts.
    const evaluate = exponent => {
      const weights = xs.map(x => Math.exp(exponent * x));
      const amplitude = weights.reduce((sum, w, i) => sum + w * points[i].firstTime, 0) /
        weights.reduce((sum, w) => sum + w * w, 0);
      const squaredError = weights.reduce((sum, w, i) => sum + (points[i].firstTime - amplitude * w) ** 2, 0);
      return { amplitude, squaredError };
    };
    // Bracket the best region before refinement; reject a fit at the search boundary.
    const grid = Array.from({ length: 81 }, (_, i) => -20 + i * 0.5);
    let best = 0;
    grid.forEach((k, i) => { if (evaluate(k).squaredError < evaluate(grid[best]).squaredError) best = i; });
    if (best === 0 || best === grid.length - 1) return null;
    let low = grid[best - 1], high = grid[best + 1];
    for (let i = 0; i < 90; i++) {
      const left = low + (high - low) / 3, right = high - (high - low) / 3;
      if (evaluate(left).squaredError < evaluate(right).squaredError) high = right;
      else low = left;
    }
    const exponent = (low + high) / 2, fit = evaluate(exponent);
    const valueAt = year => fit.amplitude * Math.exp(exponent * (year - first.year) / years);
    const samples = Array.from({ length: years * samplesPerYear + 1 }, (_, i) => {
      const year = first.year + i / samplesPerYear;
      return { year, value: valueAt(year) };
    });
    return { rate: Math.expm1(exponent / years), squaredError: fit.squaredError,
      samples, points: points.map(p => ({ year: p.year, value: valueAt(p.year) })) };
  }
  const api = {
    paperShares: (points, period = 3) => trailingAverage(points,
      p => Number.isFinite(p.share) && Number.isFinite(p.papers) ? 100 * p.papers : NaN, p => p.total, period),
    annualizedGrowth,
    publicationWindow,
    exponentialTrend,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.DBML_HOME_TRENDS = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
