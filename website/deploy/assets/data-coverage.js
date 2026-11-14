(() => {
  'use strict';
  const root = document.querySelector('#data-coverage');
  if (!root) return;
  const $ = selector => root.querySelector(selector);
  const fmt = new Intl.NumberFormat('en-US');
  const pct = new Intl.NumberFormat('en-US', {style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1});
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const share = (count, total) => !total ? '\u2014' : count > 0 && count/total < 0.001 ? '<0.1%' : pct.format(count/total);
  const dialog = document.querySelector('#coverage-dialog');
  const groups = new Map(), cache = new Map(), nodeDetails = new Map();
  const candidatePages = new Map(), candidatePageSize = 6;
  const normalize = value => String(value).normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
  const nameRules = {exact_alias:'Exact normalized name',first_last_variant:'First/last-name variant',initial_last_variant:'Initial/last-name variant',reviewed_service_alias:'Reviewed service-name alias'};
  let data, selection, records = [], filtered = [], search = '', page = 0, generation = 0;
  function registerGroups(value) {
    if (!value || typeof value !== 'object') return;
    if (value.id && value.detailUrl && Number.isFinite(value.count)) groups.set(value.id, value);
    Object.values(value).forEach(registerGroups);
  }
  async function init() {
    root.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('data/coverage_summary.json', {cache: 'no-cache'});
      if (!response.ok) throw Error('Coverage unavailable');
      data = await response.json();
      registerGroups(data);
      for (const flow of [data.flows?.people, data.flows?.papers]) {
        for (const node of flow?.nodes || []) if (node.groupId) nodeDetails.set(node.groupId, node);
      }
      window.CoverageSankey?.render($('#coverage-flows'), data.flows, openGroup);
      $('#coverage-status').hidden = true;
      root.setAttribute('aria-busy', 'false');
    } catch {
      $('#coverage-status').textContent = 'Coverage data is unavailable. Rebuild the website data to generate it.';
      root.setAttribute('aria-busy', 'false');
    }
  }
  function candidateBody(record) {
    const review=record.identityReview, candidates=review.candidates, current=candidatePages.get(record.id)||0;
    const start=current*candidatePageSize, visible=candidates.slice(start,start+candidatePageSize);
    const cards=visible.map(c=>`<article class="coverage-candidate"><div class="coverage-candidate-heading"><a href="${escape(c.url)}" target="_blank" rel="noreferrer">${escape(c.label)} ↗</a><small>DBLP ${escape(c.pid)}</small>${c.selected||review.currentPid===c.pid?'<small class="coverage-current-match">Current match</small>':''}</div><dl><div><dt>Name match</dt><dd>${escape(c.matchTypes.map(t=>nameRules[t]||t).join(' · ')||'Not recorded')}${c.matchedAliases.length?`<small>Matched as: ${escape(c.matchedAliases.join('; '))}</small>`:''}</dd></div><div><dt>DBLP affiliations</dt><dd>${c.affiliations.length?escape(c.affiliations.join('; ')):'None recorded in the snapshot'}</dd></div><div><dt>Organization-text overlap</dt><dd>${c.affiliationOverlap==null?'Unavailable: affiliation evidence is missing':pct.format(c.affiliationOverlap)}</dd></div>${c.orcids.length?`<div><dt>Verified ORCID</dt><dd>${c.orcids.map(o=>`<a href="https://orcid.org/${escape(o)}" target="_blank" rel="noreferrer">${escape(o)} ↗</a>`).join('<br>')}</dd></div>`:''}${c.alreadyResolved&&!c.selected&&review.currentPid!==c.pid?'<div><dt>Elsewhere in the data</dt><dd>This DBLP identity is already matched to another service name.</dd></div>':''}</dl></article>`).join('');
    const pages=Math.ceil(candidates.length/candidatePageSize);
    return `${cards}${pages>1?`<div class="coverage-candidate-pages"><span>${fmt.format(start+1)}–${fmt.format(start+visible.length)} of ${fmt.format(candidates.length)} profiles</span><div><button type="button" data-candidate-record="${escape(record.id)}" data-candidate-page="${current-1}" ${current?'':'disabled'}>Previous</button><button type="button" data-candidate-record="${escape(record.id)}" data-candidate-page="${current+1}" ${current+1<pages?'':'disabled'}>Next</button></div></div>`:''}`;
  }
  function candidateReview(record, first) {
    const review=record.identityReview;
    if(!review) return '';
    const query=normalize(search.trim());
    if(query&&!candidatePages.has(record.id)){
      const match=review.candidates.findIndex(c=>normalize(JSON.stringify(c)).includes(query));
      if(match>=0)candidatePages.set(record.id,Math.floor(match/candidatePageSize));
    }
    const snapshots=[...new Set(review.candidates.map(c=>c.snapshot).filter(Boolean))];
    return `<div class="coverage-service-evidence"><span>Service affiliations</span><p>${escape(review.serviceAffiliations.join('; ')||'None recorded')}</p><small>${escape(review.serviceVenues.join(' · '))}</small></div><p class="coverage-identity-reason">${escape(review.reason)}</p>${review.recordedNote?`<details><summary>Recorded resolution note</summary><p>${escape(review.recordedNote)}</p></details>`:''}${review.candidates.length?`<details class="coverage-candidate-review" ${first?'open':''}><summary>Compare ${fmt.format(review.candidates.length)} DBLP profiles · ${fmt.format(review.exactCandidateCount)} exact-name matches</summary>${snapshots.length?`<p class="coverage-note">Snapshot: ${escape(snapshots.join(', '))}</p>`:''}<div class="coverage-candidate-list">${candidateBody(record)}</div></details>`:review.recordedCandidateCount?`<p>Candidate details are unavailable (${fmt.format(review.recordedCandidateCount)} candidates recorded).</p>`:'<p>No usable DBLP person candidate is recorded.</p>'}`;
  }
  function stageEvidence(record) {
    const values = Object.entries(record.evidence || {}).filter(([, value]) => value !== '' && value != null);
    return values.length ? `<dl class="coverage-stage-evidence">${values.map(([label, value]) => `<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`).join('')}</dl>` : '';
  }
  function reviewSources(record) {
    const urls=(record.reviewSources||[]).filter(url=>/^https?:\/\//i.test(url));
    return urls.length?`<details><summary>${escape(record.reviewSourceLabel||'Identity review sources')}</summary><ul>${urls.map((url,index)=>`<li><a href="${escape(url)}" target="_blank" rel="noreferrer">Review source ${index+1} \u2197</a></li>`).join('')}</ul></details>`:'';
  }
  function renderRecords() {
    const query = search.trim().normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
    filtered = records.filter(record => normalize(`${record.label} ${record.id} ${record.detail} ${JSON.stringify(record.evidence || {})} ${(record.reviewSources||[]).join(' ')} ${record.reviewSourceLabel||''} ${record.identityReview?JSON.stringify(record.identityReview):''}`).includes(query));
    const pages = Math.max(1, Math.ceil(filtered.length/30)); page = Math.min(page, pages-1);
    const visible = filtered.slice(page*30, page*30+30);
    document.querySelector('#coverage-result-count').textContent = filtered.length ? `${fmt.format(page*30+1)}\u2013${fmt.format(page*30+visible.length)} of ${fmt.format(filtered.length)} ${selection.unit || 'records'}` : 'No records match your search.';
    document.querySelector('#coverage-records').innerHTML = visible.map((record,index) => `<article class="coverage-record">${record.profileId ? `<button type="button" data-coverage-profile="${escape(record.profileId)}">${escape(record.label)}</button>` : record.url ? `<a href="${escape(record.url)}" target="_blank" rel="noreferrer">${escape(record.label)} \u2197</a>` : `<strong>${escape(record.label)}</strong>`}${record.identityReview?'':`<p>${escape(record.detail)}</p><small>${escape(record.id)}</small>`}${stageEvidence(record)}${reviewSources(record)}${candidateReview(record,index===0)}${record.alternatives?.length ? `<ul>${record.alternatives.map(a => `<li>Candidate: <a href="${escape(a.url)}" target="_blank" rel="noreferrer">${escape(a.label)} \u2197</a></li>`).join('')}</ul>` : ''}${record.sources?.length ? `<details><summary>Service sources</summary><ul>${record.sources.map((url,index) => `<li><a href="${escape(url)}" target="_blank" rel="noreferrer">Source ${index+1} \u2197</a></li>`).join('')}</ul></details>` : ''}</article>`).join('');
    document.querySelector('#coverage-pages').innerHTML = pages > 1 ? `<button type="button" data-coverage-page="${page-1}" ${page ? '' : 'disabled'}>Previous</button><span>Page ${page+1} of ${pages}</span><button type="button" data-coverage-page="${page+1}" ${page+1<pages ? '' : 'disabled'}>Next</button>` : '';
    document.querySelector('#coverage-download').disabled = !filtered.length;
    document.querySelector('#coverage-download').textContent=records.some(r=>r.identityReview)?'Download candidate CSV':'Download CSV';
  }
  async function openGroup(id, parentId = null) {
    selection = groups.get(id);
    if (!selection) return;
    const current = ++generation;
    records = []; filtered = []; search = ''; page = 0;
    candidatePages.clear();
    const overview=document.querySelector('#coverage-identity-overview');overview.hidden=true;overview.replaceChildren();
    document.querySelector('#coverage-search').value = '';
    document.querySelector('#coverage-selection-title').textContent = selection.label;
    document.querySelector('#coverage-selection-note').textContent = selection.description;
    const context = document.querySelector('#coverage-selection-context');
    context.textContent = [selection.scenario ? 'What-if scenario' : selection.reviewOnly ? 'Needs review' : '', selection.stage, selection.scope,
      selection.unit ? `${fmt.format(selection.representedCount ?? selection.count)} ${selection.unit}` : ''].filter(Boolean).join(' \u00b7 ');
    context.hidden = !context.textContent;
    const related = document.querySelector('#coverage-related-groups');
    const choices = (nodeDetails.get(id)?.relatedGroupIds || []).filter(key => groups.get(key)?.count);
    related.innerHTML = (parentId ? `<button type="button" data-coverage-related="${escape(parentId)}">\u2190 ${escape(groups.get(parentId)?.label || 'Back')}</button>` : '')
      + choices.map(key => {
        const group = groups.get(key);
        const scope = [group.scenario ? 'What-if scenario' : group.reviewOnly ? 'Needs review' : '', group.scope].filter(Boolean).join(' \u00b7 ');
        return `<button type="button" data-coverage-related="${escape(key)}" data-coverage-parent="${escape(id)}"><span class="coverage-related-label">${escape(group.label)} <span class="coverage-related-count"><strong>${fmt.format(group.count)}</strong>${group.unit ? ` ${escape(group.unit)}` : ''}</span></span>${scope ? `<small class="coverage-related-scope">${escape(scope)}</small>` : ''}</button>`;
      }).join('');
    related.hidden = !related.innerHTML;
    document.querySelector('#coverage-result-count').textContent = 'Loading records\u2026';
    document.querySelector('#coverage-records').replaceChildren(); document.querySelector('#coverage-pages').replaceChildren();
    document.querySelector('#coverage-download').disabled = true;
    if (!dialog.open) dialog.showModal(); dialog.scrollTop = 0;
    try {
      const cacheKey = selection.detailUrl;
      if (!cache.has(cacheKey)) cache.set(cacheKey, fetch(selection.detailUrl).then(async response => {
        if (!response.ok) throw Error('Records unavailable');
        const payload = await response.json();
        if (!Array.isArray(payload.records)) throw Error('Invalid records');
        return payload.records;
      }).catch(error => {cache.delete(cacheKey); throw error;}));
      // Large source lists should not accumulate for the whole browsing session.
      while (cache.size > 3) cache.delete(cache.keys().next().value);
      const loaded = await cache.get(cacheKey);
      if (current !== generation || !dialog.open) return;
      if (loaded.length !== selection.count) throw Error('Coverage totals changed; reload this page.');
      records = loaded;
      if (selection.recordsUnavailable) {
        document.querySelector('#coverage-result-count').textContent = selection.recordsUnavailable;
        return;
      }
      if(records.some(r=>r.identityReview)){
        const total=data.people.find(p=>p.id==='name-matching').total;
        const exact=records.filter(r=>r.identityReview?.status==='ambiguous_exact_name').length;
        const relaxed=records.filter(r=>r.identityReview?.status==='ambiguous_variant').length;
        overview.innerHTML=`<p><strong>${fmt.format(records.length)} of ${fmt.format(total)} name groups (${share(records.length,total)})</strong>${selection.id==='names-ambiguous'?` · ${fmt.format(exact)} exact-name conflicts · ${fmt.format(relaxed)} relaxed-name conflicts`:''}</p><p>Compare source affiliations with DBLP profiles. Text overlap is not identity confidence.</p>`;
        overview.hidden=false;
      }
      renderRecords();
    } catch {
      if (current === generation && dialog.open) document.querySelector('#coverage-result-count').textContent = 'Records could not be loaded. Close this list and try again.';
    }
  }
  root.addEventListener('click', event => {
    const group = event.target.closest('[data-coverage-group]');
    if (group) openGroup(group.dataset.coverageGroup);
  });
  document.querySelector('#coverage-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { if (!dialog.open) generation++; });
  document.querySelector('#coverage-search').addEventListener('input', event => {search = event.target.value; page = 0; candidatePages.clear();renderRecords();});
  dialog.addEventListener('click', event => {
    const related = event.target.closest('[data-coverage-related]');
    if (related) { openGroup(related.dataset.coverageRelated, related.dataset.coverageParent || null); return; }
    const candidate=event.target.closest('[data-candidate-page]');
    if(candidate){
      const record=records.find(r=>r.id===candidate.dataset.candidateRecord);
      const next=Number(candidate.dataset.candidatePage);
      if(record&&next>=0&&next*candidatePageSize<record.identityReview.candidates.length){
        candidatePages.set(record.id,next);
        candidate.closest('.coverage-candidate-list').innerHTML=candidateBody(record);
      }
    }
    const button = event.target.closest('[data-coverage-page]');
    if (button) { page = Number(button.dataset.coveragePage); renderRecords(); dialog.scrollTop = 0; }
    const profile = event.target.closest('[data-coverage-profile]');
    if (profile) {dialog.close(); document.dispatchEvent(new CustomEvent('dbml:open-person', {detail: {id: profile.dataset.coverageProfile}}));}
  });
  document.querySelector('#coverage-download').addEventListener('click', () => {
    const field = value => {let s=String(value??''); if (/^[=+\-@]/.test(s)) s="'"+s; return '"'+s.replaceAll('"','""')+'"';};
    const hasCandidates=records.some(r=>r.identityReview);
    const evidenceKeys=[...new Set(filtered.flatMap(r=>[...Object.keys(r.evidence||{}),...(r.reviewSources?.length?['Review sources','Review source type']:[])]))];
    const contextHeaders=['Selected component','Pipeline stage','Population scope','Counting unit'];
    const context=[selection.label,selection.stage||'',selection.scope||'',selection.unit||'records'];
    const evidence=r=>evidenceKeys.map(key=>key==='Review sources'?(r.reviewSources||[]).join('; '):key==='Review source type'&&r.reviewSources?.length?r.reviewSourceLabel||'Identity review sources':r.evidence?.[key]??'');
    const rows = hasCandidates ? [[...contextHeaders,'Service name','Name group','Service affiliations','Service venues','Resolution status','Recorded resolution note','DBLP PID','DBLP name','Current match','Name rules','Matched aliases','DBLP affiliations','Organization-text overlap (not confidence)','Verified ORCIDs','DBLP snapshot','Already resolved elsewhere','DBLP URL',...evidenceKeys],...filtered.flatMap(r=>(r.identityReview?.candidates.length?r.identityReview.candidates:[{}]).map(c=>[...context,r.label,r.id,r.identityReview?.serviceAffiliations.join('; '),r.identityReview?.serviceVenues.join('; '),r.identityReview?.status,r.identityReview?.recordedNote,c.pid,c.label,Boolean(c.pid&&(c.selected||r.identityReview?.currentPid===c.pid)),c.matchTypes?.join('; '),c.matchedAliases?.join('; '),c.affiliations?.join('; '),c.affiliationOverlap,c.orcids?.join('; '),c.snapshot,Boolean(c.alreadyResolved&&!c.selected&&r.identityReview?.currentPid!==c.pid),c.url,...evidence(r)]))] : [[...contextHeaders,'ID','Name or title','Details','URL','Service sources','Alternative candidates',...evidenceKeys], ...filtered.map(r => [...context,r.id,r.label,r.detail,r.url,(r.sources||[]).join('; '),(r.alternatives||[]).map(a=>a.label+' '+a.url).join('; '),...evidence(r)])];
    const url = URL.createObjectURL(new Blob(['\uFEFF'+rows.map(row=>row.map(field).join(',')).join('\r\n')+'\r\n'], {type:'text/csv;charset=utf-8'}));
    const anchor = document.createElement('a'); anchor.href=url; anchor.download=`coverage-${selection.id}${hasCandidates?'-candidates':''}.csv`; anchor.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  const disclosure = $('#coverage-details');
  if (disclosure) {
    root.setAttribute('aria-busy', 'false');
    let started = false;
    const loadWhenOpen = () => {
      if (!disclosure.open || started) return;
      started = true;
      init();
    };
    disclosure.addEventListener('toggle', loadWhenOpen);
    loadWhenOpen();
  } else if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {if(entries.some(e=>e.isIntersecting)){observer.disconnect();init();}}, {rootMargin:'250px'});
    observer.observe(root);
  } else init();
})();
