/* Browser checks against the actual exported flows and lazy audit lists. */
window.runCoverageSankeyTests = async function(frame, baseUrl) {
  let checks=0;
  const assert=(value,message)=>{if(!value)throw Error(message);checks++;};
  const target=new URL('website/deploy/all.html?coverage-check=candidate-lookup#data-coverage',baseUrl);
  await new Promise(resolve=>{
    const loaded=()=>{if(frame.contentDocument.URL===target.href&&frame.contentDocument.readyState==='complete'){frame.removeEventListener('load',loaded);resolve();}};
    frame.addEventListener('load',loaded);frame.src=target.href;
  });
  const doc=frame.contentDocument, win=frame.contentWindow;
  const wait=async condition=>{for(let i=0;i<250;i++){if(condition())return;await new Promise(r=>setTimeout(r,30));}throw Error('Timed out waiting for coverage');};
  doc.querySelector('#data-coverage').scrollIntoView({behavior:'instant'});
  const disclosure=doc.querySelector('#coverage-details');
  assert(disclosure&&!disclosure.open,'Technical coverage details are collapsed by default');
  assert(!doc.querySelector('.coverage-sankey svg'),'Technical charts wait until the details are opened');
  disclosure.querySelector('summary').click();
  await wait(()=>doc.querySelectorAll('.coverage-sankey svg').length===2);
  assert(disclosure.open,'Opening technical details renders both Sankeys');
  disclosure.querySelector('summary').click();
  assert(!disclosure.open&&!doc.querySelector('#coverage-flows').checkVisibility(),'Closing technical details hides the charts');
  disclosure.querySelector('summary').click();
  assert(doc.querySelectorAll('.coverage-sankey svg').length===2,'Reopening preserves the charts without duplicating them');
  const summary=await (await win.fetch('data/coverage_summary.json')).json();
  const sourceYears=summary.flows.people.sources.map(source=>source.year);
  assert(doc.querySelector('[data-flow-host="people"]').closest('.coverage-flow-panel').querySelector('.insights-data-label').textContent.includes(Math.min(...sourceYears)+'\u2013'+Math.max(...sourceYears)), 'People flow labels the source-page service years');
  const snapshotDate=summary.flows.papers.snapshot.id.match(/\d{4}-\d{2}-\d{2}/)[0];
  const snapshotLabel=new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}).format(new Date(snapshotDate+'T00:00:00Z'));
  assert(doc.querySelector('[data-flow-host="papers"]').closest('.coverage-flow-panel').querySelector('.insights-data-label').textContent.includes(snapshotLabel), 'Publication flow labels the pinned snapshot date');
  const people=doc.querySelector('[data-flow-host="people"]'), papers=doc.querySelector('[data-flow-host="papers"]');
  assert(people&&papers,'Both Sankey charts render');
  const formatPct=value=>value>0&&value<0.001?'<0.1%':new Intl.NumberFormat('en-US',{style:'percent',minimumFractionDigits:1,maximumFractionDigits:1}).format(value);
  for(const key of ['people','papers']) {
    const flow=summary.flows[key];
    const last=flow.stages.length-1;
    const model=win.CoverageSankey.layout(flow,1200,0,last);
    assert(model.nodes.every(n=>n.x>=0&&n.x+12+n.labelWidth<=model.width),'Nodes and labels stay within chart width');
    assert(model.nodes.every(n=>n.y>=0&&n.y+n.height<=model.height&&n.labelY+78<=model.height),'Nodes and labels stay within chart height');
    assert(model.links.every(l=>!l.path.includes('NaN')&&!l.path.includes('Infinity')),'Valid SVG ribbon geometry');
    const before=JSON.stringify(flow), normalized=win.CoverageSankey.layout(flow,1200,0,last,'percentage');
    for(let stage=0;stage<flow.stages.length;stage++) {
      const nodes=normalized.nodes.filter(n=>n.stage===stage);
      assert(Math.abs(nodes.reduce((sum,n)=>sum+n.share,0)-1)<1e-10,'Each percentage layer sums to 100%: '+key+' '+stage);
    }
    for(const node of normalized.nodes) {
      const ins=normalized.links.filter(l=>l.target===node.id), outs=normalized.links.filter(l=>l.source===node.id);
      if(ins.length)assert(Math.abs(ins.reduce((sum,l)=>sum+l.targetThickness,0)-node.height)<1e-8,'Incoming normalized ribbons fit node '+node.id);
      if(outs.length)assert(Math.abs(outs.reduce((sum,l)=>sum+l.sourceThickness,0)-node.height)<1e-8,'Outgoing normalized ribbons fit node '+node.id);
    }
    assert(JSON.stringify(flow)===before,'Percentage transform preserves source counts');
    for(const node of flow.nodes) {
      const incoming=flow.links.filter(l=>l.target===node.id).reduce((n,l)=>n+l.value,0);
      const outgoing=flow.links.filter(l=>l.source===node.id).reduce((n,l)=>n+l.value,0);
      assert(!incoming||incoming===node.count,'Incoming flow reconciles: '+node.id);
      assert(!outgoing||outgoing===node.count,'Outgoing flow reconciles: '+node.id);
    }
  }
  assert(summary.flows.people.nodes.find(n=>n.id==='focus-0').count===summary.people[1].groups[0].count,'Final population matches existing coverage');
  assert(summary.flows.papers.nodes.find(n=>n.id==='main').count===summary.papers.find(c=>c.id==='publication-use').groups[0].count,'Final ML work count matches existing coverage');
  const setMode=(host,mode)=>host.closest('.coverage-flow-panel').querySelector(`[data-flow-mode="${mode}"]`).click();
  setMode(people,'percentage');
  if(people.classList.contains('is-compact')){const select=people.querySelector('select');select.value=String(summary.flows.people.nodes.find(n=>n.id==='ambiguous').stage-1);select.dispatchEvent(new win.Event('change',{bubbles:true}));}
  assert(people.querySelector('[data-flow-node="ambiguous"] strong').textContent===formatPct(summary.people[0].groups.find(g=>g.id==='names-ambiguous').count/summary.people[0].total),'Ambiguity uses name-group denominator');
  assert(papers.querySelector('.coverage-flow-scale-note').hidden,'Sankey switches are independent');
  const originalWidth=frame.getAttribute('width');
  for(const width of [1200,800,390,320]) {
    frame.setAttribute('width',String(width));frame.getBoundingClientRect();win.dispatchEvent(new win.Event('resize'));
    await new Promise(resolve=>setTimeout(resolve,250));
    for(const host of [people,papers]) {
      setMode(host,'percentage');
      const bounds=host.getBoundingClientRect(), svg=host.querySelector('svg').getBoundingClientRect();
      assert(svg.right<=bounds.right+1&&svg.left>=bounds.left-1,'Sankey fits at '+width);
      assert(host.scrollWidth<=host.clientWidth+1,'No internal horizontal overflow at '+width);
      assert(!Array.from(host.querySelectorAll('*')).some(el=>['auto','scroll'].includes(win.getComputedStyle(el).overflowX)&&el.scrollWidth>el.clientWidth+1),'No horizontal scrollbar at '+width);
      const select=host.querySelector('select');
      if(host.classList.contains('is-compact')){
        select.value=String(summary.flows[host===people?'people':'papers'].stages.length-2);select.dispatchEvent(new win.Event('change',{bubbles:true}));
        assert(host.querySelectorAll('.coverage-flow-headings > span').length===2,'Two readable steps on narrow screens');
        assert(host.querySelector('[data-flow-node="'+(host===people?'focus-0':'main')+'"]'),'Final counts remain accessible on narrow screens');
        assert(host.querySelector('[data-flow-next]').disabled,'Next control stops at the final steps');
        host.querySelector('[data-flow-previous]').click();
        assert(Number(select.value)===summary.flows[host===people?'people':'papers'].stages.length-3,'Previous control moves back one step');
        host.querySelector('[data-flow-next]').click();
        assert(Number(select.value)===summary.flows[host===people?'people':'papers'].stages.length-2,'Next control restores the final steps');
      } else assert(host.querySelectorAll('.coverage-flow-headings > span').length===summary.flows[host===people?'people':'papers'].stages.length,'Whole flow on desktop');
      const flow=summary.flows[host===people?'people':'papers'];
      host.querySelectorAll('[data-flow-node]').forEach(el=>{
        const n=flow.nodes.find(n=>n.id===el.dataset.flowNode),total=flow.nodes.filter(v=>v.stage===n.stage).reduce((sum,v)=>sum+v.count,0);
        assert(el.querySelector('strong').textContent===formatPct(n.count/total),'Displayed percentage is stage-based: '+n.id);
        assert(el.querySelector('.coverage-flow-label small')?.textContent,'Counting units are visible beside node values');
        const label=el.querySelector('.coverage-flow-label'),slot=el.querySelector('foreignObject');
        assert(label.getBoundingClientRect().height<=slot.getBoundingClientRect().height+1,'Node labels and units fit at '+width+': '+n.id);
      });
    }
    const node=people.querySelector('[data-flow-node="focus-0"]');
    node.focus();
    const tooltip=people.querySelector('.coverage-flow-tooltip');
    assert(!tooltip.hidden&&tooltip.textContent.includes('DB-focused'),'Keyboard focus shows the definition');
    assert(node.getAttribute('aria-describedby')===tooltip.id,'Focused node is linked to its explanation');
    const box=tooltip.getBoundingClientRect();
    assert(box.left>=0&&box.right<=win.innerWidth+1,'Tooltip stays in viewport');
    node.dispatchEvent(new win.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
    assert(tooltip.hidden,'Escape closes the tooltip');
  }
  const node=people.querySelector('[data-flow-node="focus-0"]');
  node.dispatchEvent(new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  await wait(()=>doc.querySelector('#coverage-dialog').open&&doc.querySelector('#coverage-records > article'));
  assert(doc.querySelector('#coverage-selection-title').textContent==='DB-focused','Sankey opens the matching audit list');
  assert(doc.querySelector('#coverage-result-count').textContent.includes(String(summary.flows.people.nodes.find(n=>n.id==='focus-0').count)),'Audit count matches the final Sankey count');
  doc.querySelector('#coverage-dialog').close();
  if(people.classList.contains('is-compact')){const select=people.querySelector('select');select.value=String(summary.flows.people.nodes.find(n=>n.id==='ambiguous').stage-1);select.dispatchEvent(new win.Event('change',{bubbles:true}));}
  people.querySelector('[data-flow-node="ambiguous"]').dispatchEvent(new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  await wait(()=>doc.querySelector('#coverage-selection-title').textContent==='Ambiguous match'&&doc.querySelector('#coverage-records .coverage-candidate'));
  assert(doc.querySelector('#coverage-identity-overview').textContent.includes(formatPct(summary.people[0].groups[1].count/summary.people[0].total)),'Ambiguity overview shows share of all name groups');
  const group=summary.people[0].groups.find(g=>g.id==='names-ambiguous');
  const ambiguous=(await (await win.fetch(group.detailUrl)).json()).records;
  assert(ambiguous.every(r=>r.identityReview?.candidates.length===r.identityReview?.recordedCandidateCount),'All stored candidates are exposed once per profile');
  const sample=ambiguous.find(r=>r.identityReview.candidates.some(c=>c.affiliations.length&&c.orcids.length));
  const search=doc.querySelector('#coverage-search');
  search.value=sample.label;search.dispatchEvent(new win.Event('input',{bubbles:true}));
  const sampleElement=Array.from(doc.querySelectorAll('#coverage-records > .coverage-record')).find(el=>el.querySelector(':scope > strong')?.textContent===sample.label);
  assert(sampleElement,'Service name can be searched');
  sampleElement.querySelector('.coverage-candidate-review').open=true;
  assert(sampleElement.querySelector('.coverage-service-evidence').textContent.includes(sample.identityReview.serviceAffiliations[0]),'Original service affiliation shown');
  assert(sampleElement.querySelector('.coverage-candidate-heading a').href.startsWith('https://dblp.org/pid/'),'Candidate DBLP link');
  assert(sampleElement.querySelector('.coverage-candidate dd').textContent.length>0,'Candidate name-match evidence');
  assert(sampleElement.querySelector('a[href^="https://orcid.org/"]'),'Candidate ORCID is available for review');
  assert(doc.querySelector('#coverage-identity-overview').textContent.includes('not identity confidence'),'Text overlap is not presented as confidence');
  const largest=ambiguous.reduce((a,b)=>a.identityReview.candidates.length>b.identityReview.candidates.length?a:b);
  search.value=largest.label;search.dispatchEvent(new win.Event('input',{bubbles:true}));
  let big=Array.from(doc.querySelectorAll('#coverage-records > .coverage-record')).find(el=>el.querySelector(':scope > strong')?.textContent===largest.label);
  assert(big,'Large candidate lists remain discoverable');
  big.querySelector('.coverage-candidate-review').open=true;
  assert(big.querySelectorAll('.coverage-candidate').length<=6,'Candidate list is paginated without internal scrolling');
  while(big.querySelector('[data-candidate-page]:not([disabled])')?.textContent==='Previous')big.querySelector('[data-candidate-page]:not([disabled])').click();
  const firstPid=big.querySelector('.coverage-candidate-heading small').textContent;
  Array.from(big.querySelectorAll('[data-candidate-page]')).find(b=>b.textContent==='Next').click();
  assert(big.querySelector('.coverage-candidate-heading small').textContent!==firstPid,'Candidate pagination advances to different profiles');
  assert(big.querySelector('.coverage-candidate-pages').textContent.includes('7–12'),'Candidate pagination displays its range');
  const later=largest.identityReview.candidates[largest.identityReview.candidates.length-1];
  search.value=later.pid;search.dispatchEvent(new win.Event('input',{bubbles:true}));
  assert(doc.querySelector('#coverage-records').textContent.includes(later.pid),'Searching a candidate PID reveals its evidence');
  let blob;
  const createObject=win.URL.createObjectURL,clickAnchor=win.HTMLAnchorElement.prototype.click;
  win.URL.createObjectURL=value=>{blob=value;return 'blob:coverage-test';};
  win.HTMLAnchorElement.prototype.click=function(){};
  doc.querySelector('#coverage-download').click();
  win.URL.createObjectURL=createObject;win.HTMLAnchorElement.prototype.click=clickAnchor;
  const csv=await blob.text();
  assert(csv.includes('Organization-text overlap (not confidence)')&&csv.includes(later.pid),'Candidate CSV includes labelled evidence');
  assert(doc.querySelector('#coverage-dialog').scrollWidth<=doc.querySelector('#coverage-dialog').clientWidth+1,'Candidate dialog fits mobile width');
  doc.querySelector('#coverage-dialog').close();
  for(const id of ['coverage-review','coverage-people','coverage-papers','coverage-classification-chart','coverage-evidence-chart'])
    assert(!doc.getElementById(id),'Redundant analysis panels are removed: '+id);
  assert(!doc.querySelector('#data-coverage').textContent.includes('What could improve the analysis?'),'Review-card heading is removed');
  assert(summary.flows.people.stages.includes('Candidate lookup'),'Candidate finding is a separate step');
  const lookups=summary.flows.people.nodes.filter(n=>n.id.startsWith('lookup-'));
  assert(lookups.reduce((sum,n)=>sum+n.count,0)===summary.flows.people.nodes.find(n=>n.id==='name-groups').count,'Candidate counts use service-name groups');
  const stages=people.querySelector('select');
  if(people.classList.contains('is-compact')){stages.value=String(summary.flows.people.stages.length-2);stages.dispatchEvent(new win.Event('change',{bubbles:true}));}
  const limitedNode=people.querySelector('[data-flow-node="focus-1"]');
  limitedNode.focus();
  assert(people.querySelector('.coverage-flow-tooltip').textContent.includes('No qualifying papers'),'Limited-evidence reasons are in the node tooltip');
  limitedNode.dispatchEvent(new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  await wait(()=>doc.querySelector('#coverage-selection-title').textContent==='Limited evidence'&&doc.querySelector('#coverage-records > article'));
  assert(doc.querySelectorAll('#coverage-related-groups [data-coverage-related^="flow-limited-"]').length===4,'Limited evidence has four optional reason drilldowns');
  const potential=summary.quality.find(g=>g.id==='review-potential');
  if(potential?.count){
    const potentialButton=doc.querySelector('[data-coverage-related="review-potential"]');
    assert(potentialButton.textContent.includes(potential.unit),'Potential-addition shortcut states its counting unit');
    assert(potentialButton.textContent.includes('What-if scenario'),'Potential additions are labelled as a scenario before opening');
    potentialButton.click();
    await wait(()=>doc.querySelector('#coverage-selection-title').textContent===potential.label&&doc.querySelector('#coverage-records > article'));
    assert(doc.querySelectorAll('#coverage-records > article').length===potential.count,'Potential additions remain accessible inside the Sankey');
    const context=doc.querySelector('#coverage-selection-context');
    assert(context.textContent.includes('What-if scenario')&&context.textContent.includes(potential.scope),'Potential-addition modal states scenario and population');
    assert(doc.querySelector('#coverage-result-count').textContent.includes(potential.unit),'Scenario results use the exported people unit');
    doc.querySelector('[data-coverage-related="people-1"]').click();
    await wait(()=>doc.querySelector('#coverage-selection-title').textContent==='Limited evidence');
  }
  doc.querySelector('#coverage-dialog').close();
  if(papers.classList.contains('is-compact')){const select=papers.querySelector('select');select.value=String(summary.flows.papers.stages.length-2);select.dispatchEvent(new win.Event('change',{bubbles:true}));}
  const uncertain=summary.flows.papers.nodes.find(n=>n.id==='uncertain');
  if(uncertain?.count){
    papers.querySelector('[data-flow-node="uncertain"]').dispatchEvent(new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
    await wait(()=>doc.querySelector('#coverage-selection-title').textContent===uncertain.label&&doc.querySelector('#coverage-records > article'));
    const length=summary.flows.groups.find(g=>g.id==='flow-unverified-db-cohort');
    const globalLength=summary.quality.find(g=>g.id==='review-length');
    assert(globalLength&&globalLength.unit==='DBLP records','The broader record-level review queue remains separately identified');
    const lengthButton=doc.querySelector('[data-coverage-related="flow-unverified-db-cohort"]');
    assert(length.count===uncertain.count&&length.unit==='distinct works','Study-cohort review counts match the uncertain work branch');
    assert(lengthButton.textContent.includes(length.unit)&&lengthButton.textContent.includes(length.scope),'Study-work review shortcut states its own population and counting unit');
    lengthButton.click();
    await wait(()=>doc.querySelector('#coverage-selection-title').textContent===length.label&&doc.querySelector('#coverage-records > article'));
    const context=doc.querySelector('#coverage-selection-context');
    assert(context.textContent.includes(length.scope)&&context.textContent.includes(length.unit),'Study-work review modal preserves scope and counting unit');
    assert(doc.querySelector('#coverage-result-count').textContent.includes(length.unit),'Study-work review results count works');
    doc.querySelector('#coverage-dialog').close();
  }
  const sparse=summary.quality.find(g=>g.id==='review-sparse-identities');
  if(sparse?.count){
    const parent=summary.flows.people.nodes.find(n=>n.relatedGroupIds?.includes(sparse.id));
    assert(parent,'Accepted-identity review queue is reachable from a people Sankey node');
    if(people.classList.contains('is-compact')){const select=people.querySelector('select');select.value=String(Math.min(parent.stage,summary.flows.people.stages.length-2));select.dispatchEvent(new win.Event('change',{bubbles:true}));}
    people.querySelector(`[data-flow-node="${parent.id}"]`).dispatchEvent(new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
    await wait(()=>doc.querySelector(`[data-coverage-related="${sparse.id}"]`));
    const button=doc.querySelector(`[data-coverage-related="${sparse.id}"]`);
    assert(button.textContent.includes('Needs review')&&button.textContent.includes(sparse.unit),'Sparse accepted matches are a review queue, not confirmed errors');
    button.click();
    await wait(()=>doc.querySelector('#coverage-selection-title').textContent===sparse.label&&doc.querySelector('#coverage-records .coverage-candidate'));
    assert(doc.querySelector('#coverage-selection-context').textContent.includes(sparse.scope),'Accepted-match review scope is explicit');
    assert(doc.querySelector('#coverage-records .coverage-current-match')?.textContent==='Current match','The currently assigned DBLP profile is identifiable');
    assert(doc.querySelector('#coverage-result-count').textContent.includes(sparse.unit),'Accepted-match queue counts service-name groups');
    doc.querySelector('#coverage-dialog').close();
  }
  assert(doc.querySelector('.coverage-flow-snapshot').textContent.includes(String(summary.flows.papers.searchPopulation.withoutPublications)),'Publication search reports profiles without papers');
  assert(!doc.querySelector('#coverage-flows .coverage-flow-table, #coverage-flows details'),'Charts do not contain nested reference expanders');
  const sourceDetails=doc.querySelector('[data-flow-reference-dialog="people"]');
  doc.querySelector('[data-flow-reference="people"]').click();
  assert(sourceDetails.open,'People counts and sources open in a separate popup');
  assert(!sourceDetails.querySelector('[data-reference-panel="counts"]').hidden&&sourceDetails.querySelector('[data-reference-panel="sources"]').hidden,'Reference starts with just the stage counts visible');
  sourceDetails.querySelector('[data-reference-tab="sources"]').click();
  assert(!sourceDetails.querySelector('[data-reference-panel="sources"]').hidden&&sourceDetails.querySelector('[data-reference-panel="counts"]').hidden,'Source pages have their own view inside the popup');
  assert(doc.querySelectorAll('.coverage-source-grid > div').length===20,'Source counts for all 20 conference years');
  assert(Array.from(doc.querySelectorAll('.coverage-source-grid a')).every(a=>a.href.startsWith('https://')||a.href.startsWith('http://')),'Source links are usable');
  assert(!sourceDetails.querySelector('details'),'Source pages need no nested expansion');
  assert(sourceDetails.scrollWidth<=sourceDetails.clientWidth+1,'People reference popup fits the viewport');
  const sourceTab=sourceDetails.querySelector('[data-reference-tab="sources"]');
  sourceTab.focus();sourceTab.dispatchEvent(new win.KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));
  assert(!sourceDetails.querySelector('[data-reference-panel="counts"]').hidden&&sourceDetails.querySelector('[data-reference-tab="counts"]').getAttribute('aria-selected')==='true','Keyboard arrows switch the reference views');
  const firstGroup=summary.flows.people.nodes[0].groupId;
  sourceDetails.querySelector('[data-reference-tab="counts"]').click();
  sourceDetails.querySelector(`[data-flow-group="${firstGroup}"]`).click();
  await wait(()=>doc.querySelector('#coverage-dialog').open&&doc.querySelector('#coverage-records > article'));
  assert(!sourceDetails.open,'Opening a stage list closes its reference popup');
  doc.querySelector('#coverage-dialog').close();
  const paperDetails=doc.querySelector('[data-flow-reference-dialog="papers"]');
  doc.querySelector('[data-flow-reference="papers"]').click();
  paperDetails.querySelector('[data-reference-tab="sources"]').click();
  assert(paperDetails.open&&paperDetails.textContent.includes(summary.flows.papers.snapshot.id),'Publication reference popup includes the pinned DBLP snapshot');
  assert(paperDetails.scrollWidth<=paperDetails.clientWidth+1,'Publication reference popup fits the viewport');
  paperDetails.querySelector('[data-reference-close]').click();
  assert(!paperDetails.open,'Reference popup closes with its close button');
  frame.setAttribute('width',originalWidth||'1440');frame.getBoundingClientRect();win.dispatchEvent(new win.Event('resize'));
  await new Promise(r=>setTimeout(r,250));
  setMode(people,'absolute');setMode(papers,'absolute');
  // Inspect every node and every exact ribbon, including the early stages.
  const descriptors=new Map();
  const collect=value=>{if(!value||typeof value!=='object')return;if(value.id&&value.detailUrl)descriptors.set(value.id,value);Object.values(value).forEach(collect);};
  collect(summary);
  const csvForSelection=async()=>{
    let exported;
    const create=win.URL.createObjectURL,click=win.HTMLAnchorElement.prototype.click;
    win.URL.createObjectURL=blob=>{exported=blob;return 'blob:coverage-stage-check';};
    win.HTMLAnchorElement.prototype.click=function(){};
    try {doc.querySelector('#coverage-download').click();} finally {win.URL.createObjectURL=create;win.HTMLAnchorElement.prototype.click=click;}
    return exported.text();
  };
  const checkedReviewSources=new Set();
  for(const [kind,host] of [['people',people],['papers',papers]]){
    const flow=summary.flows[kind],last=flow.stages.length-1;
    for(const component of [...flow.nodes,...flow.links]){
      assert(component.groupId,'Every component has an inspection group: '+kind+' '+(component.id||component.source));
      const group=descriptors.get(component.groupId),expected=component.count??component.value;
      assert(group&&group.count===expected,'Exact component count has a matching lazy list');
      if(!expected)continue;
      if(host.classList.contains('is-compact')){
        const stage=component.stage??flow.nodes.find(n=>n.id===component.source).stage;
        const select=host.querySelector('select');select.value=String(Math.min(stage,last-1));select.dispatchEvent(new win.Event('change',{bubbles:true}));
      }
      const model=win.CoverageSankey.layout(flow,host.clientWidth,host.classList.contains('is-compact')?Number(host.querySelector('select').value):0,host.classList.contains('is-compact')?Number(host.querySelector('select').value)+1:last);
      const index=component.id?null:model.links.findIndex(l=>l.source===component.source&&l.target===component.target);
      const element=component.id?host.querySelector(`[data-flow-node="${component.id}"]`):host.querySelector(`.coverage-flow-hit[data-flow-link="${index}"]`);
      assert(element?.getAttribute('role')==='button','Nodes and ribbons support keyboard activation');
      if(!component.id){
        element.focus();
        const tooltip=host.querySelector('.coverage-flow-tooltip'),source=flow.nodes.find(n=>n.id===component.source),target=flow.nodes.find(n=>n.id===component.target);
        assert(tooltip.textContent.includes(formatPct(expected/source.count)+' of outgoing flow from '+source.label),'Ribbon explains the originating-node flow share');
        assert(tooltip.textContent.includes(formatPct(expected/target.count)+' of incoming flow to '+target.label),'Ribbon explains the destination-node flow share');
        if(component.kind==='merged')assert(tooltip.textContent.includes('Flow accounting:')&&tooltip.textContent.includes(kind==='people'?'not rejected people':'not extra publications'),'Merge flow explains accounting rather than human or paper losses');
      }
      element.dispatchEvent(new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
      await wait(()=>doc.querySelector('#coverage-selection-title').textContent===group.label&&doc.querySelector('#coverage-records > article'));
      assert(doc.querySelectorAll('#coverage-records > article').length===Math.min(expected,30),'List pagination uses exact component records');
      assert(doc.querySelector('#coverage-records .coverage-stage-evidence dd'),'Relevant stage evidence is visible');
      assert(doc.querySelector('#coverage-selection-context').textContent.includes(group.unit),'Component counting unit is visible');
      if(kind==='people'&&!component.id&&component.target==='ambiguous'){
        if(component.source==='lookup-none')assert(!doc.querySelector('#coverage-records .coverage-candidate')&&doc.querySelector('#coverage-records').textContent.includes('No usable DBLP person candidate'),'A zero-candidate ribbon explains absence without inventing profiles');
        else assert(doc.querySelector('#coverage-records .coverage-candidate'),'Ambiguous ribbons preserve candidate comparisons');
        const csv=await csvForSelection();
        assert(csv.includes('DBLP PID')&&csv.includes('Organization-text overlap (not confidence)'),'Ambiguous ribbon CSV retains candidate evidence');
      }
      if(!checkedReviewSources.has(kind)){
        const payload=await(await win.fetch(group.detailUrl)).json(),reviewed=payload.records.find(record=>record.reviewSources?.length);
        if(reviewed){
          const sourceUrl=reviewed.reviewSources[0],input=doc.querySelector('#coverage-search');
          input.value=sourceUrl;input.dispatchEvent(new win.Event('input',{bubbles:true}));
          const record=Array.from(doc.querySelectorAll('#coverage-records .coverage-record')).find(el=>el.textContent.includes(reviewed.label));
          assert(record,'Review sources can be searched even when the reviewed record was on a later page');
          assert(Array.from(record.querySelectorAll('summary')).some(el=>el.textContent===(reviewed.reviewSourceLabel||'Identity review sources')),'Review evidence uses its own disclosure label');
          assert(Array.from(record.querySelectorAll('a')).some(el=>el.href===new URL(sourceUrl,win.location.href).href),'Review evidence links open the recorded source');
          const csv=await csvForSelection();
          assert(csv.includes('Review sources')&&csv.includes('Review source type')&&csv.includes(sourceUrl),'CSV retains review source URLs and their meaning');
          checkedReviewSources.add(kind);
        }
      }
      if(['source-cidr_reviewers','focus-0','versions'].includes(component.id)){
        const csv=await csvForSelection();
        assert(csv.includes('Selected component')&&csv.includes('Pipeline stage')&&csv.includes(group.label),'CSV preserves component context');
        const evidence=component.id==='source-cidr_reviewers'?'Affiliation':component.id==='focus-0'?'Focus reason':'Version-linking rule';
        assert(csv.includes(evidence),'CSV includes stage decision evidence: '+evidence);
      }
      doc.querySelector('#coverage-dialog').close();
    }
    assert(doc.querySelectorAll(`[data-flow-reference-dialog="${kind}"] .coverage-flow-table [data-flow-group]`).length===flow.nodes.length,'All stage-table entries are inspectable in the reference popup');
  }
  const categoryReview=summary.quality.find(group=>group.id==='review-display-categories');
  if(categoryReview?.count){
    const uncertain=summary.flows.papers.nodes.find(node=>node.id==='uncertain');
    assert(!uncertain.relatedGroupIds?.some(id=>id.startsWith('flow-pending-categories-')),'Display categories remain separate from DB-eligibility uncertainty');
    for(const nodeId of ['context','outside']){
      const node=summary.flows.papers.nodes.find(value=>value.id===nodeId);
      const groupId='flow-pending-categories-'+nodeId;
      const group=descriptors.get(groupId);
      if(!group?.count)continue;
      assert(node.relatedGroupIds.includes(groupId),'Pending display categories are reachable from their exact work branch');
      if(papers.classList.contains('is-compact')){
        const select=papers.querySelector('select');select.value=String(Math.min(node.stage,summary.flows.papers.stages.length-2));select.dispatchEvent(new win.Event('change',{bubbles:true}));
      }
      papers.querySelector(`[data-flow-node="${nodeId}"]`).dispatchEvent(new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
      await wait(()=>doc.querySelector('#coverage-selection-title').textContent===node.label&&doc.querySelector(`[data-coverage-related="${groupId}"]`));
      doc.querySelector(`[data-coverage-related="${groupId}"]`).click();
      await wait(()=>doc.querySelector('#coverage-selection-title').textContent===group.label&&doc.querySelector('#coverage-records > article'));
      assert(group.unit==='distinct works'&&doc.querySelector('#coverage-selection-context').textContent.includes(group.scope),'Pending category inspection states the correct unit and population');
      const payload=await(await win.fetch(group.detailUrl)).json();
      assert(payload.records.length===group.count&&payload.records.every(record=>record.reviewSourceLabel==='Publication category sources'),'Publication review sources are distinct from service and identity sources');
      const source=payload.records[0].reviewSources[0];
      assert(Array.from(doc.querySelectorAll('#coverage-records a')).some(link=>link.href===new URL(source,win.location.href).href),'Publication review evidence is linked');
      const csv=await csvForSelection();
      assert(csv.includes('Review source type')&&csv.includes('Publication category sources')&&csv.includes(source),'Category-review CSV preserves source meaning and URLs');
      doc.querySelector('#coverage-dialog').close();
    }
  }
  win.scrollTo({top:doc.querySelector('#data-coverage').getBoundingClientRect().top+win.scrollY-90,behavior:'instant'});
  return checks;
};
