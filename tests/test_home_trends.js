(function (root) {
  'use strict';
  function runHomeTrendTests(api) {
    let checks = 0;
    const assert = (condition, message) => { if (!condition) throw new Error(message); checks++; };
    const near = (actual, expected, message) => assert(Math.abs(actual - expected) < 1e-10, message);
    const points = [10, 100, 1000].map((total, i) => ({year: 2020+i, total, papers: i+1, share: 100*(i+1)/total}));
    const before = JSON.stringify(points), smoothed = api.paperShares(points);
    near(smoothed[0].value,10,'Start with the observed first-year share');
    near(smoothed[1].value,100*3/110,'Pool the two available initial years with their real denominators');
    assert(smoothed[0].startYear===2020 && smoothed[1].startYear===2020,'Initial windows report their actual starting year');
    near(smoothed[2].value, 100*6/1110, 'Pool paper totals rather than average venue percentages');
    assert(smoothed[2].startYear === 2020, 'Report the full window');
    assert(JSON.stringify(points) === before, 'Keep observed data unchanged');
    const missing = [...points, {year:2023,total:null,papers:2,share:null},
      {year:2024,total:100,papers:1,share:1},{year:2025,total:100,papers:2,share:2},
      {year:2026,total:100,papers:3,share:3}];
    const gaps = api.paperShares(missing);
    assert(gaps.slice(3,6).every(p=>p.value === null), 'Do not smooth across missing totals');
    near(gaps[6].value, 2, 'Resume after three consecutive valid years');
    assert(api.paperShares([points[0],points[1],{...points[2],year:2024}])[2].value === null, 'A missing calendar year creates a gap');
    assert(api.paperShares([{...points[0],total:0},points[1],points[2]])[2].value === null, 'Zero denominator is unavailable');
    const five = Array.from({length:7},(_,i)=>({year:2019+i,total:100*(i+1),papers:i+1,share:1}));
    const pooled = api.paperShares(five,5);
    assert(pooled.slice(0,4).every(p=>p.value===1 && p.startYear===2019),'Initial windows use one to four available years');
    near(pooled[4].value,1,'Five-year share pools all five denominators');
    assert(pooled[4].startYear===2019,'Five-year window starts four years before its endpoint');
    const future = five.map(p=>({...p})); future[6].papers=100;
    near(api.paperShares(future,5)[5].value,pooled[5].value,'Future observations do not alter earlier trailing shares');
    const interrupted = five.map(p=>({...p})); interrupted[3].total=null;
    const interruptedTrend=api.paperShares(interrupted,5);
    assert(interruptedTrend.slice(0,3).every(p=>p.value===1),'Later missing totals leave earlier initial windows unchanged');
    assert(interruptedTrend.slice(3).every(p=>p.value===null),'Missing totals invalidate every affected five-year window');
    assert(pooled[5].startYear===2020,'After five years, the oldest year drops out of the trailing window');
    near(pooled[5].denominator,200+300+400+500+600,'The moving window includes only its five actual totals');
    const history = Array.from({length:28}, (_, i) => {
      const year=2001+i, papers=year<2006?10:1;
      return {year,papers,total:100,share:papers};
    });
    const historicalTrend={startYear:2001,endYear:2025,series:[
      {venue:'ICML',points:history},
      {venue:'NeurIPS',points:history.map(p=>({...p,papers:p.year<2010?0:2,share:p.year<2010?0:2}))},
      {venue:'ICLR',points:history.map(p=>({...p,total:p.year<2013?null:100,share:p.year<2013?null:1}))},
    ]};
    const historyBefore=JSON.stringify(historicalTrend);
    const view=api.publicationWindow(historicalTrend,2026);
    assert(view.startYear===2006 && view.endYear===2025,'The 2026 view starts in 2006 and ends at shared data coverage');
    assert(view.series.every(s=>s.points.every(p=>p.year<=2025)),'No observations beyond the shared endpoint enter the view');
    assert(view.series[0].points[0].year===2001,'Retain earlier history to smooth the first visible year');
    const firstVisible=api.paperShares(view.series[0].points,5).find(p=>p.year===2006);
    near(firstVisible.value,8.2,'The first visible point pools 2002 to 2006, including history outside the display');
    assert(firstVisible.startYear===2002,'Initial visible tooltip reports its full historical window');
    assert(view.growthComparison.ratios[0].startYear===2006 && view.growthComparison.ratios[0].ratio===1,'Growth starts at the cutoff rather than the historical first paper');
    assert(view.growthComparison.ratios[1].startYear===2010,'A later first nonzero share starts growth later');
    assert(view.growthComparison.ratios[2].startYear===2013,'Missing venue totals cannot supply a growth baseline');
    assert(api.publicationWindow(historicalTrend,2027).startYear===2007,'The window advances automatically next year');
    assert(api.publicationWindow({...historicalTrend,endYear:2028},2026).endYear===2026,'Future years are not displayed');
    assert(api.publicationWindow(historicalTrend,2050).series.length===0,'An expired snapshot does not invent recent observations');
    assert(api.publicationWindow({endYear:2025,series:[{venue:'ICML',points:[{year:2025,share:1}]}]},2026).growthComparison.ratios[0].ratio===null,'A single usable year has no growth rate');
    assert(JSON.stringify(historicalTrend)===historyBefore,'Window selection preserves the full source history');
    near(api.annualizedGrowth(1,49,9),Math.pow(49,1/9)-1,'CAGR uses elapsed years, not the number of observations');
    near(api.annualizedGrowth(100,25,2),-0.5,'CAGR handles declining values');
    near(api.annualizedGrowth(2,2,9),0,'Unchanged endpoints have zero CAGR');
    near(api.annualizedGrowth(2,0,9),-1,'A zero ending count implies a complete decline');
    assert(api.annualizedGrowth(0,49,9)===null,'CAGR is unavailable for a zero baseline');
    assert(api.annualizedGrowth(1,49,0)===null,'A single year has no annualized growth rate');
    assert(api.annualizedGrowth(null,49,9)===null,'Missing counts are not zero');
    const counts = [1,2,6,16,11,17,23,25,39,49].map((firstTime,i)=>({year:2016+i,firstTime}));
    const countBefore=JSON.stringify(counts), curve=api.exponentialTrend(counts);
    assert(Math.abs(curve.rate-0.31989894563311026)<1e-6,'Count-scale least squares agrees with an independent numerical fit');
    assert(Math.abs(curve.squaredError-84.66253428172585)<1e-7,'All-year fit has the independently calculated squared error');
    assert(curve.points[0].value>4 && curve.points[0].value<4.1,'Fitted curve is not forced through the small starting count');
    assert(Math.abs(curve.points[3].value-counts[3].firstTime)>1,'Curve does not pretend fitted values are observations');
    curve.points.slice(1).forEach((p,i)=>near(p.value/curve.points[i].value,1+curve.rate,'Curve applies one constant annual multiplier'));
    assert(curve.samples.length===109 && curve.samples[0].year===2016 && curve.samples[108].year===2025,'Curve samples stay within the observed endpoints');
    assert(JSON.stringify(counts)===countBefore,'Curve calculation preserves observed annual counts');
    const endpointError=counts.reduce((sum,p,i)=>sum+(p.firstTime-Math.pow(49,i/9))**2,0);
    assert(curve.squaredError<endpointError/4,'All-year exponential fit substantially improves on endpoint CAGR for these counts');
    const changed=counts.map(p=>({...p}));changed[3].firstTime=100;
    assert(Math.abs(api.exponentialTrend(changed).rate-curve.rate)>0.01,'Interior annual counts influence the fit');
    const exact=[3,6,12,24].map((firstTime,i)=>({year:2020+i,firstTime}));
    const recovered=api.exponentialTrend(exact);
    near(recovered.rate,1,'Recover the rate of an exact exponential series');
    recovered.points.forEach((p,i)=>near(p.value,exact[i].firstTime,'Recover exact exponential values'));
    const decline=[24,12,6,3].map((firstTime,i)=>({year:2020+i,firstTime}));
    near(api.exponentialTrend(decline).rate,-0.5,'Fit declines as well as growth');
    near(api.exponentialTrend(exact.map(p=>({...p,firstTime:4}))).rate,0,'Flat counts have zero trend growth');
    assert(api.exponentialTrend([0,2,4,8].map((firstTime,i)=>({year:2020+i,firstTime})))!==null,'Zero counts remain observations in count-scale fitting');
    assert(api.exponentialTrend(exact.map(p=>({...p,firstTime:0})))===null,'All-zero counts have no exponential growth estimate');
    assert(api.exponentialTrend([{year:2020,firstTime:1}])===null,'Require at least three years for a trend');
    assert(api.exponentialTrend([...exact].reverse())===null,'Reject unordered observations');
    assert(api.exponentialTrend(exact.map((p,i)=>({...p,firstTime:i===2?null:p.firstTime})))===null,'Missing counts are not silently omitted');
    assert(api.paperShares([]).length===0 && api.exponentialTrend([])===null,'Empty inputs have no trends');
    return checks;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports=runHomeTrendTests;
    if (require.main===module) process.stdout.write(`PASS ${runHomeTrendTests(require('../website/deploy/assets/home-trends.js'))} checks\n`);
  } else root.runHomeTrendTests=runHomeTrendTests;
})(typeof globalThis !== 'undefined' ? globalThis : this);
