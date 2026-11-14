/* Local SVG renderer: no external chart library or runtime data service. */
(() => {
  'use strict';
  const fmt = new Intl.NumberFormat('en-US');
  const percent = value => value>0&&value<0.001?'<0.1%':new Intl.NumberFormat('en-US',{style:'percent',minimumFractionDigits:1,maximumFractionDigits:1}).format(value);
  const shortUnit = unit => ({'parsed service entries':'entries','service entries':'entries','additional service entries':'extra entries',
    'normalized name groups':'name groups','name groups':'name groups','DBLP identities':'DBLP identities',
    'researcher–record links':'author links','deduplicated works':'works','distinct works':'works','additional DBLP records':'extra records'}[unit]||unit);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const colors = {accent:'var(--accent)', sage:'var(--author-middle)', green:'var(--author-first)',
    'dark-green':'var(--author-last)', muted:'var(--line-strong)', attention:'var(--pub-workshop)', database:'var(--pub-database)'};
  let serial = 0;

  function layout(flow, width, first, last, mode='absolute') {
    const height = width < 600 ? 510 : 600, gap = 18, labelSpace = 78;
    const totals=flow.stages.map((_,i)=>flow.nodes.filter(n=>n.stage===i).reduce((sum,n)=>sum+n.count,0));
    const nodes = flow.nodes.filter(n => n.stage >= first && n.stage <= last && n.count > 0).map(n => ({...n,
      total:totals[n.stage], share:n.count/totals[n.stage], value:mode==='percentage'?100*n.count/totals[n.stage]:n.count}));
    const columns = Array.from({length:last-first+1}, (_, i) => nodes.filter(n => n.stage === i+first));
    let lo = 0, hi = height / Math.max(1, ...nodes.map(n => n.value));
    const occupied = (column, scale) => column.reduce((s,n) => s+Math.max(labelSpace,n.value*scale), 0)+Math.max(0,column.length-1)*gap;
    for (let i=0; i<45; i++) {
      const mid = (lo+hi)/2;
      if (columns.every(c => occupied(c,mid) <= height-24)) lo=mid; else hi=mid;
    }
    const step = width/columns.length;
    columns.forEach((column,i) => {
      let y=12;
      column.forEach(node => {
        const slot=Math.max(labelSpace,node.value*lo);
        Object.assign(node,{x:i*step+4, y:y+(slot-node.value*lo)/2, height:node.value*lo,
          scale:mode==='percentage'?100*lo/node.total:lo, labelY:y+(slot-labelSpace)/2, labelWidth:step-22});
        y+=slot+gap;
      });
    });
    const byId = new Map(nodes.map(n => [n.id,n]));
    const sourceOffsets=new Map(), targetOffsets=new Map();
    const links=flow.links.filter(l => byId.has(l.source)&&byId.has(l.target)).map(link => {
      const source=byId.get(link.source), target=byId.get(link.target);
      const sy=source.y+(sourceOffsets.get(source.id)||0), ty=target.y+(targetOffsets.get(target.id)||0);
      const sourceThickness=link.value*source.scale, targetThickness=link.value*target.scale;
      sourceOffsets.set(source.id,(sourceOffsets.get(source.id)||0)+sourceThickness);
      targetOffsets.set(target.id,(targetOffsets.get(target.id)||0)+targetThickness);
      const x=source.x+8, end=target.x, mid=(x+end)/2;
      const path=`M${x},${sy} C${mid},${sy} ${mid},${ty} ${end},${ty} L${end},${ty+targetThickness} C${mid},${ty+targetThickness} ${mid},${sy+sourceThickness} ${x},${sy+sourceThickness} Z`;
      const center=`M${x},${sy+sourceThickness/2} C${mid},${sy+sourceThickness/2} ${mid},${ty+targetThickness/2} ${end},${ty+targetThickness/2}`;
      return {...link,path,center,sourceThickness,targetThickness,sourceNode:source,targetNode:target};
    });
    return {nodes,links,width,height,totals};
  }

  function mount(host, flow, onGroup) {
    if (flow.unavailable) { host.innerHTML=`<p class="coverage-note">${esc(flow.unavailable)}</p>`; return; }
    const id='coverage-flow-'+(++serial);
    const reference=document.createElement('dialog');
    reference.className='coverage-reference-dialog';
    reference.dataset.flowReferenceDialog=flow.id;
    reference.setAttribute('aria-labelledby',`${id}-reference-title`);
    const sourcePages=(flow.sources||[]).map(s=>`<div><strong>${esc(s.venue==='SIGMOD'?'SIGMOD/PODS':s.venue)} ${esc(s.year)}</strong><span>${fmt.format(s.entries)} entries · ${fmt.format(s.pages)} documents</span>${s.urls.map((url,i)=>`<a href="${esc(url)}" target="_blank" rel="noreferrer">Source ${i+1} ↗</a>`).join('')}</div>`).join('');
    const searched=flow.searchPopulation;
    reference.innerHTML=`<div class="dialog-shell"><button class="dialog-close" type="button" data-reference-close aria-label="Close counts and sources">×</button><h2 id="${id}-reference-title">${flow.id==='people'?'People':'Publication'} pipeline details</h2>${sourcePages?`<section class="coverage-reference-sources"><h3>Source pages</h3><p class="coverage-note">Saved entries by conference year. Names missed by the scrapers are not measured.</p><div class="coverage-source-grid">${sourcePages}</div></section>`:''}${flow.snapshot?`<p class="coverage-note">DBLP snapshot: <strong>${esc(flow.snapshot.id)}</strong>. <a href="https://dblp.org/xml/" target="_blank" rel="noreferrer">DBLP publication data ↗</a></p>`:''}${searched?`<p class="coverage-flow-snapshot">Publication search: ${fmt.format(searched.searched)} matched profiles · ${fmt.format(searched.withPublications)} with publications · ${fmt.format(searched.withoutPublications)} without.</p>`:''}<section class="coverage-flow-table"><h3>Stage counts</h3><p class="coverage-note">The same groups as the chart, listed for reference. Select a group to inspect its records.</p><div>${flow.stages.map((stage,i)=>`<div><h4>${i+1}. ${esc(stage)}</h4><ul>${flow.nodes.filter(n=>n.stage===i).map(n=>`<li><span>${esc(n.label)}</span><strong data-flow-table-node="${esc(n.id)}">${fmt.format(n.count)}</strong><small>${esc(n.unit)}</small></li>`).join('')}</ul></div>`).join('')}</div></section></div>`;
    document.body.append(reference);
    const shell=reference.querySelector('.dialog-shell');
    const counts=reference.querySelector('.coverage-flow-table');
    const sources=document.createElement('section');
    while(shell.children[2]&&shell.children[2]!==counts)sources.append(shell.children[2]);
    sources.hidden=true;
    sources.dataset.referencePanel='sources';
    counts.dataset.referencePanel='counts';
    shell.append(sources);
    const tabs=document.createElement('div');tabs.className='coverage-reference-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','Pipeline reference details');
    tabs.innerHTML=['counts','sources'].map((key,index)=>`<button type="button" role="tab" id="${id}-${key}-tab" aria-controls="${id}-${key}-panel" aria-selected="${index===0}" tabindex="${index===0?0:-1}" data-reference-tab="${key}">${index===0?'Stage counts':'Sources'}</button>`).join('');
    counts.before(tabs);
    for(const panel of [counts,sources]){panel.id=`${id}-${panel.dataset.referencePanel}-panel`;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',`${id}-${panel.dataset.referencePanel}-tab`);}
    const activateTab=button=>{
      tabs.querySelectorAll('button').forEach(tab=>{const active=tab===button;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;});
      for(const panel of [counts,sources])panel.hidden=panel.dataset.referencePanel!==button.dataset.referenceTab;
      reference.scrollTop=0;
    };
    tabs.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();
      const buttons=[...tabs.querySelectorAll('button')],index=buttons.indexOf(document.activeElement);
      const next=event.key==='Home'?0:event.key==='End'?1:(index+(event.key==='ArrowRight'?1:-1)+2)%2;
      activateTab(buttons[next]);buttons[next].focus();
    });
    const opener=host.closest('.coverage-flow-panel').querySelector('[data-flow-reference]');
    opener.setAttribute('aria-controls',`${id}-reference`);
    reference.id=`${id}-reference`;
    opener.addEventListener('click',()=>{reference.showModal();reference.scrollTop=0;});
    const availableGroups=new Set([...flow.nodes,...flow.links].filter(n=>n.groupId).map(n=>n.groupId));
    const stages=flow.stages.map((label,i)=>`<option value="${i}">${i+1}. ${esc(label)} → ${esc(flow.stages[i+1])}</option>`).slice(0,-1).join('');
    host.innerHTML=`<div class="coverage-flow-toolbar"><p class="coverage-note">Hover for details. Select a group to inspect its records.</p><div class="coverage-flow-step"><button type="button" data-flow-previous aria-label="Previous ${flow.id==='people'?'people':'publication'} flow steps">Previous</button><label>Steps <select aria-label="${flow.id==='people'?'People':'Publication'} flow steps">${stages}</select></label><button type="button" data-flow-next aria-label="Next ${flow.id==='people'?'people':'publication'} flow steps">Next</button></div></div><p class="coverage-flow-scale-note coverage-note" hidden>Each column totals 100% of what enters that step. Ribbon widths are normalized separately at each end.</p><div class="coverage-flow-headings" aria-hidden="true"></div><div class="coverage-sankey"></div><div class="coverage-flow-tooltip" id="${id}-tip" role="tooltip" hidden></div><p class="coverage-flow-notes"></p>`;
    const canvas=host.querySelector('.coverage-sankey'), headings=host.querySelector('.coverage-flow-headings');
    const select=host.querySelector('select'), tip=host.querySelector('.coverage-flow-tooltip');
    const switches=host.closest('.coverage-flow-panel').querySelectorAll('[data-flow-mode]');
    let current, selected, frame, mode='absolute';
    function draw() {
      const width=Math.max(240,host.clientWidth), compact=width<950;
      host.classList.toggle('is-compact',compact);
      const first=compact?Number(select.value):0, last=compact?first+1:flow.stages.length-1;
      host.querySelector('[data-flow-previous]').disabled=Number(select.value)===0;
      host.querySelector('[data-flow-next]').disabled=Number(select.value)>=flow.stages.length-2;
      current=layout(flow,width,first,last,mode);
      const labelValue=n=>mode==='percentage'?percent(current.totals[n.stage]?n.count/current.totals[n.stage]:0):fmt.format(n.count);
      switches.forEach(button=>{const active=button.dataset.flowMode===mode;button.classList.toggle('is-active',active);button.setAttribute('aria-pressed',String(active));});
      host.querySelector('.coverage-flow-scale-note').hidden=mode!=='percentage';
      host.querySelector('.coverage-flow-notes').textContent=flow.notes.map(note=>mode==='percentage'?note.replace('Widths follow counts. ',''):note).join(' ');
      reference.querySelectorAll('[data-flow-table-node]').forEach(el=>{const n=flow.nodes.find(n=>n.id===el.dataset.flowTableNode);el.textContent=labelValue(n);el.nextElementSibling.textContent=(mode==='percentage'?fmt.format(n.count)+' ':'')+n.unit;});
      headings.style.gridTemplateColumns=`repeat(${last-first+1},minmax(0,1fr))`;
      headings.innerHTML=flow.stages.slice(first,last+1).map((s,i)=>`<span><b>${first+i+1}</b>${esc(s)}${mode==='percentage'?`<small>100% = ${fmt.format(current.totals[first+i])}</small>`:''}</span>`).join('');
      const paths=current.links.map((l,i)=>`<path class="coverage-flow-ribbon coverage-flow-${l.kind}" d="${l.path}" fill="${colors[l.targetNode.color]||colors.sage}" data-flow-link="${i}"></path><path class="coverage-flow-hit" d="${l.center}" data-flow-link="${i}" tabindex="0" role="button" aria-label="${esc(l.sourceNode.label)} to ${esc(l.targetNode.label)}: ${fmt.format(l.value)} ${esc(l.targetNode.unit)}. Inspect records."/>`).join('');
      const nodes=current.nodes.map(n=>`<g class="coverage-flow-node ${n.groupId?'is-linked':''}" data-flow-node="${esc(n.id)}" tabindex="0" role="${n.groupId?'button':'img'}" aria-label="${esc(n.label)}: ${esc(labelValue(n))}${mode==='percentage'?` (${fmt.format(n.count)} of ${fmt.format(n.total)} entering this step)`:` ${esc(n.unit)}`}${n.groupId?'. Inspect records.':''}"><rect x="${n.x}" y="${n.y}" width="8" height="${n.height}" fill="${colors[n.color]||colors.sage}"/><foreignObject x="${n.x+12}" y="${n.labelY}" width="${n.labelWidth}" height="78"><div xmlns="http://www.w3.org/1999/xhtml" class="coverage-flow-label"><strong>${esc(labelValue(n))}</strong><span>${esc(n.label)}</span><small>${esc(shortUnit(n.unit))}</small></div></foreignObject></g>`).join('');
      canvas.innerHTML=`<svg viewBox="0 0 ${current.width} ${current.height}" role="group" aria-label="${flow.id==='people'?'People':'Publication'} resolution Sankey. Ribbon widths represent ${mode==='percentage'?'percentages within each step':'counts'}. Merged entries and versions are not lost records.">${paths}${nodes}</svg>`;
      tip.hidden=true;
    }
    function hide() {
      tip.hidden=true; selected=null;
      canvas.querySelectorAll('[aria-describedby]').forEach(el=>el.removeAttribute('aria-describedby'));
      canvas.querySelectorAll('.is-highlighted').forEach(el=>el.classList.remove('is-highlighted'));
    }
    function show(target, event) {
      const nodeElement=target.closest('[data-flow-node]'), linkElement=target.closest('[data-flow-link]');
      if (!nodeElement&&!linkElement) {hide();return;}
      const node=nodeElement&&current.nodes.find(n=>n.id===nodeElement.dataset.flowNode);
      const link=linkElement&&current.links[Number(linkElement.dataset.flowLink)];
      selected=node?.groupId||link?.groupId||link?.targetNode.groupId;
      const label=node?.label||`${link.sourceNode.label} → ${link.targetNode.label}`;
      const count=node?.count??link.value, total=node?.total??link.targetNode.total, stage=node?.stage??link.targetNode.stage;
      const detail=node||link.targetNode;
      const shares=node?`<span>${esc(percent(count/total))} of ${fmt.format(total)} entering ${esc(flow.stages[stage])}</span>`:
        `<span>${esc(percent(count/link.sourceNode.count))} of outgoing flow from ${esc(link.sourceNode.label)}</span><span>${esc(percent(count/link.targetNode.count))} of incoming flow to ${esc(link.targetNode.label)}</span>`;
      const unitChange=link&&(link.kind==='merged'||shortUnit(link.sourceNode.unit)!==shortUnit(link.targetNode.unit));
      const accounting=unitChange?`<small>Flow accounting: ${esc(link.sourceNode.unit)} → ${esc(link.targetNode.unit)}. ${flow.id==='people'?'Repeat entries and aliases are merged, not rejected people.':'Coauthor links and versions are not extra publications.'}</small>`:'';
      tip.innerHTML=`<strong>${esc(label)}</strong><span>${fmt.format(count)} ${esc(detail.unit)}</span>${shares}<p>${esc(detail.description)}</p>${detail.details?.length?`<ul>${detail.details.map(text=>`<li>${esc(text)}</li>`).join('')}</ul>`:''}${accounting}${selected?'<small>Select to inspect records</small>':''}`;
      tip.hidden=false;
      canvas.querySelectorAll('[aria-describedby]').forEach(el=>el.removeAttribute('aria-describedby'));
      (nodeElement||linkElement).setAttribute('aria-describedby',tip.id);
      const box=(nodeElement||linkElement).getBoundingClientRect();
      const x=event.clientX??box.left+box.width/2, y=event.clientY??box.top+box.height/2;
      const r=tip.getBoundingClientRect();
      tip.style.left=`${Math.max(12,Math.min(window.innerWidth-r.width-12,x+14))}px`;
      tip.style.top=`${Math.max(12,Math.min(window.innerHeight-r.height-12,y+14))}px`;
      canvas.querySelectorAll('[data-flow-link]').forEach(el=>{
        const l=current.links[Number(el.dataset.flowLink)];
        el.classList.toggle('is-highlighted',node?l.source===node.id||l.target===node.id:l===link);
      });
    }
    canvas.addEventListener('pointermove',event=>show(event.target,event));
    canvas.addEventListener('pointerleave',hide);
    canvas.addEventListener('focusin',event=>show(event.target,{}));
    canvas.addEventListener('focusout',hide);
    canvas.addEventListener('click',event=>{
      show(event.target,event);
      if(selected&&availableGroups.has(selected)){onGroup(selected);hide();}
    });
    canvas.addEventListener('keydown',event=>{
      if(event.key==='Escape') hide();
      if(event.key==='Enter'||event.key===' '){event.preventDefault();show(event.target,{});if(selected){onGroup(selected);hide();}}
    });
    // The full stage table provides the same inspection on compact screens,
    // without stepping through every pair of columns.
    reference.querySelectorAll('.coverage-flow-table li').forEach((item,index)=>{
      const node=flow.stages.flatMap((_,stage)=>flow.nodes.filter(n=>n.stage===stage))[index];
      if(!node?.groupId)return;
      const button=document.createElement('button');button.type='button';button.dataset.flowGroup=node.groupId;
      while(item.firstChild)button.append(item.firstChild);
      item.append(button);
    });
    reference.addEventListener('click',event=>{
      const tab=event.target.closest('[data-reference-tab]');
      if(tab){activateTab(tab);return;}
      const button=event.target.closest('[data-flow-group]');
      if(button){reference.close();onGroup(button.dataset.flowGroup);}
      else if(event.target.closest('[data-reference-close]')||event.target===reference)reference.close();
    });
    select.addEventListener('change',draw);
    host.querySelector('[data-flow-previous]').addEventListener('click',()=>{select.value=String(Math.max(0,Number(select.value)-1));draw();});
    host.querySelector('[data-flow-next]').addEventListener('click',()=>{select.value=String(Math.min(flow.stages.length-2,Number(select.value)+1));draw();});
    switches.forEach(button=>button.addEventListener('click',()=>{mode=button.dataset.flowMode;draw();}));
    window.addEventListener('scroll',hide,{passive:true});
    const observer=new ResizeObserver(()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(draw);});
    observer.observe(host); draw();
    return reference;
  }

  function render(host, flows, onGroup) {
    if(!flows) return;
    const serviceYears=[...new Set((flows.people.sources||[]).map(source=>Number(source.year)).filter(Number.isInteger))];
    const servicePeriod=serviceYears.length?`${Math.min(...serviceYears)}–${Math.max(...serviceYears)}`:'No recorded years';
    const snapshotDate=flows.papers.snapshot?.id?.match(/\d{4}-\d{2}-\d{2}/)?.[0];
    const snapshotLabel=snapshotDate?new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}).format(new Date(`${snapshotDate}T00:00:00Z`)):flows.papers.snapshot?.id||'Unavailable';
    const heading=(kind,title,scope)=>`<div class="coverage-flow-heading"><div><h3>${title}</h3><p class="insights-data-label">${esc(scope)}</p></div><div class="coverage-flow-actions"><button type="button" class="coverage-reference-button" data-flow-reference="${kind==='People'?'people':'papers'}" aria-haspopup="dialog">Counts and sources ↗</button><div class="viz-view-switch" role="group" aria-label="${kind} Sankey measurement"><button type="button" data-flow-mode="percentage" aria-pressed="false">Percentage</button><button type="button" data-flow-mode="absolute" class="is-active" aria-pressed="true">Absolute</button></div></div></div>`;
    host.innerHTML=`<article class="coverage-flow-panel"><p class="eyebrow">People · from service pages to the study</p>${heading('People','How many researchers make it through?',`Service-page entries · ${servicePeriod}`)}<div data-flow-host="people"></div></article><article class="coverage-flow-panel"><p class="eyebrow">Publications · from DBLP to the website</p>${heading('Publication','Where do the publication records go?',`DBLP snapshot · ${snapshotLabel}`)}<div data-flow-host="papers"></div></article>`;
    const references={people:mount(host.querySelector('[data-flow-host="people"]'),flows.people,onGroup),papers:mount(host.querySelector('[data-flow-host="papers"]'),flows.papers,onGroup)};
    const checks=[['people','nameRepairs','name repairs retained'],['people','technicalDuplicates','technical duplicates removed'],['people','invalidNames','invalid names'],['papers','authorAmbiguity','ambiguous author links rejected'],['papers','invalidWebsiteWorks','works absent from website input']];
    for(const kind of ['people','papers']) {
      const values=checks.filter(([k,key])=>k===kind&&flows[k].checks?.[key]>0);
      if(values.length) references[kind]?.querySelector('.dialog-shell').insertAdjacentHTML('beforeend',`<p class="coverage-flow-checks">${values.map(([k,key,label])=>`${fmt.format(flows[k].checks[key])} ${label}`).join(' · ')}</p>`);
    }
  }
  window.CoverageSankey={render,layout};
})();
