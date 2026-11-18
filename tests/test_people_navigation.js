/* End-to-end page separation, chart handoff and explorer regressions. */
window.runPeopleNavigationTests = async function(frame, baseUrl) {
  let checks=0;
  const assert=(value,message)=>{if(!value)throw Error(message);checks++;};
  const wait=async predicate=>{for(let i=0;i<300;i++){if(predicate())return;await new Promise(r=>setTimeout(r,30));}throw Error('Page did not become ready');};
  const ready=doc=>doc?.readyState==='complete'&&(doc.querySelector('#people-body tr')||doc.querySelector('#global-timeline .publication-line-chart')||doc.querySelector('[data-home-new-growth] .home-growth-info'));
  const load=async(relative, destination=relative.split(/[?#]/)[0])=>{
    const previous=frame.contentDocument;
    frame.src=new URL(relative,baseUrl).href;
    await wait(()=>frame.contentDocument!==previous&&frame.contentWindow.location.pathname.endsWith('/'+destination)&&ready(frame.contentDocument));
    return frame.contentDocument;
  };
  const follow=async action=>{
    action();await wait(()=>frame.contentWindow.location.pathname.endsWith('/people.html')&&ready(frame.contentDocument));
    return frame.contentDocument;
  };
  const count=doc=>Number(doc.querySelector('#results-count strong').textContent.replace(/\D/g,''));
  let doc=await load('all.html'),data=frame.contentWindow.CROSSOVER_DATA;
  assert(!doc.querySelector('#explore'),'Explorer is removed from insights');
  assert(doc.title.startsWith('All Insights'),'All Insights browser title is renamed');
  for(const filename of ['index.html','people.html','all.html']){
    doc=await load(filename);
    const nav=doc.querySelector('.top-nav');
    assert([...nav.querySelectorAll('a')].map(a=>a.textContent.trim()).join('|')==='Home|People Explorer|All Insights','Navigation order is shared');
    assert(nav.querySelector('[aria-current=page]').getAttribute('href')===filename,'Correct page is active');
    for(const width of [320,390,768,1440]){
      frame.width=width;await new Promise(r=>setTimeout(r,180));
      assert(doc.documentElement.scrollWidth<=doc.documentElement.clientWidth+1,'Page fits '+filename+' '+width);
      const buttons=[...nav.children].map(a=>a.getBoundingClientRect());
      assert(buttons.every(r=>r.left>=0&&r.right<=doc.documentElement.clientWidth+1),'Navigation fits '+filename+' '+width);
      assert(buttons.every((r,i)=>!i||buttons[i-1].right<=r.left+1),'Tabs do not overlap '+filename+' '+width);
    }
    doc.querySelector('[data-open-methodology]').click();
    await wait(()=>doc.querySelector('#methodology-dialog').open);
    assert(doc.querySelector('#methodology-dialog-content').textContent.includes('DBLP'),'Methodology works on '+filename);
    doc.querySelector('#methodology-dialog-close').click();
  }
  for(const selection of [
    {selector:'[data-service-drilldown="SIGMOD"]',kind:'venue-service',expected:()=>Number(doc.querySelector('[data-service-drilldown="SIGMOD"] strong').textContent.replace(/\D/g,''))},
    {selector:'button[data-overview-ml="without"]',kind:'ml-status',expected:()=>data.people.length-data.people.filter(p=>p.ml.mainPaperCount>0).length},
    {selector:'[data-venue-ml-drilldown="VLDB"]',kind:'venue-ml',expected:()=>data.people.filter(p=>data.populationFocus.candidates.find(c=>c.dblpPid===p.dblpPid)?.db.events.some(e=>e.venue==='VLDB')&&p.ml.mainPaperCount>0).length},
    {selector:'[data-authorship-role="first"]',kind:'authorship-role'},
  ]){
    doc=await load('all.html');const expected=selection.expected?.();
    doc=await follow(()=>doc.querySelector(selection.selector).click());
    assert(new URL(frame.contentWindow.location.href).searchParams.get('selection')===selection.kind,'Chart selection reaches People Explorer');
    assert(!doc.querySelector('#chart-selection-banner').hidden,'Selection remains visible above the list');
    assert(expected==null?count(doc)>0:count(doc)===expected,'Chart handoff preserves its researcher group: '+selection.kind+' '+count(doc)+' / '+expected);
  }
  doc=await load('people.html');
  assert(doc.querySelector('h1').textContent==='People Explorer','People page has its own heading');
  assert(count(doc)===data.people.filter(p=>p.focus.included&&p.ml.mainPaperCount>0).length,'Default qualifying ML population is unchanged');
  assert(!doc.querySelector('#global-timeline')&&!doc.querySelector('#data-coverage'),'All Insights stay on their own page');
  doc.querySelector('[data-explorer-cohort="all"]').click();
  assert(count(doc)===data.people.length+data.additionalExplorerPeople.length,'All still exposes excluded PC profiles');
  const input=doc.querySelector('#search-input'),win=frame.contentWindow;
  const excluded=data.additionalExplorerPeople.find(p=>p.focus.label==='ML-focused');
  input.value=excluded.name;input.dispatchEvent(new win.Event('input',{bubbles:true}));
  assert(doc.querySelector('.focus-badge'),'Excluded researcher remains searchable with the warning badge');
  input.value='NoSuchGem731';input.dispatchEvent(new win.Event('input',{bubbles:true}));
  assert(!doc.querySelector('#empty-state').hidden&&doc.querySelector('#empty-title').textContent.includes(input.value),'Friendly no-results message is preserved');
  doc.querySelector('#reset-filters').click();doc.querySelector('#people-body tr').click();
  await wait(()=>doc.querySelector('#person-dialog').open&&doc.querySelector('#dialog-content h2'));
  assert(doc.querySelector('#dialog-content').textContent.includes('NeurIPS'),'Researcher detail still opens');
  doc.querySelector('#dialog-close').click();
  doc=await load('all.html#explore','people.html');
  assert(doc.querySelector('#people-title'),'Old explorer bookmarks redirect to People Explorer');
  return checks;
};
