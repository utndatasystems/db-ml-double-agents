(() => {
  const target = document.querySelector('#focus-groups');
  const policy = window.CROSSOVER_DATA?.populationFocus;
  if (!target || !policy) return;
  const fmt = new Intl.NumberFormat('en-US');
  const labels = ['DB-focused', 'ML-focused', 'Mixed', 'Limited evidence'];
  const api = window.DBVenueOverlap;
  const escape = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const reasons = api.limitedEvidenceGroups.map(group => ({ ...group,
    count: policy.candidates.filter(person => api.limitedEvidenceReason(person.focus) === group.key).length,
  }));
  target.innerHTML = policy.groups.map((group, index) => {
    const width = policy.candidateCount ? 100 * group.count / policy.candidateCount : 0;
    const label = `<span class="overlap-bar-label">${group.label}<small>${index === 0 ? 'Included' : 'Outside study'}</small></span>`;
    const value = `<span class="overlap-bar-value"><strong>${fmt.format(group.count)}</strong></span>`;
    if (group.label !== 'Limited evidence') return `<button type="button" class="overlap-bar-row focus-group focus-group-${index}" data-focus-group="${index}">${label}<span class="overlap-bar-track" aria-hidden="true"><i style="width:${width}%"></i></span>${value}</button>`;
    return `<div class="overlap-bar-row focus-group focus-group-${index}" data-focus-group="${index}" role="group" aria-label="Limited evidence: ${fmt.format(group.count)} people">
      <button type="button" class="focus-row-action" aria-label="Inspect all ${fmt.format(group.count)} people with limited evidence">${label}</button>
      <div class="overlap-bar-track focus-segment-track" role="group" aria-label="Reasons for limited evidence">
        <div class="focus-limited-fill" style="width:${width}%">${reasons.filter(reason => reason.count).map(reason => `<button type="button" class="focus-limited-segment" data-focus-reason="${reason.key}" style="width:${100 * reason.count / group.count}%" aria-label="${escape(`${reason.label}: ${fmt.format(reason.count)} of ${fmt.format(group.count)} people. ${reason.description} Select to inspect people.`)}"></button>`).join('')}</div>
      </div>
      <button type="button" class="focus-row-action" aria-label="Inspect all ${fmt.format(group.count)} people with limited evidence">${value}</button>
    </div>`;
  }).join('');
  document.querySelector('#focus-summary').textContent = `${fmt.format(policy.includedCount)} of ${fmt.format(policy.candidateCount)} matched PC members qualify; ${fmt.format(policy.serviceFallbackCount || 0)} through the repeated-service fallback. Labels describe publication balance in the selected venues, not seniority. Committee service covers 2022–2025.`;
  target.addEventListener('click', event => {
    const segment = event.target.closest('[data-focus-reason]');
    const button = event.target.closest('[data-focus-group]');
    if (button) {
      hideTooltip();
      document.dispatchEvent(new CustomEvent('dbml:show-focus-list', {detail: {label: labels[Number(button.dataset.focusGroup)], reason: segment?.dataset.focusReason || null}}));
    }
  });
  const tooltip = document.createElement('div');
  tooltip.id = 'focus-segment-tooltip';
  tooltip.className = 'focus-segment-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  tooltip.hidden = true;
  document.body.append(tooltip);
  let activeSegment = null, closeTimer = null;
  function hideTooltip() {
    window.clearTimeout(closeTimer);
    activeSegment?.removeAttribute('aria-describedby');
    activeSegment = null;
    tooltip.hidden = true;
  }
  function showTooltip(segment) {
    hideTooltip();
    const reason = reasons.find(item => item.key === segment.dataset.focusReason);
    const total = policy.groups.find(group => group.label === 'Limited evidence').count;
    activeSegment = segment;
    tooltip.innerHTML = `<strong>${escape(reason.label)}</strong><span>${fmt.format(reason.count)} of ${fmt.format(total)} with limited evidence</span><p>${escape(reason.description)}</p>`;
    tooltip.hidden = false;
    segment.setAttribute('aria-describedby', tooltip.id);
    const rect = segment.getBoundingClientRect(), bounds = tooltip.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    const left = Math.max(12, Math.min(rect.left + rect.width / 2 - bounds.width / 2, viewportWidth - bounds.width - 12));
    const top = rect.bottom + 8 + bounds.height <= window.innerHeight - 12
      ? rect.bottom + 8 : Math.max(12, rect.top - bounds.height - 8);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }
  function scheduleHide() {
    window.clearTimeout(closeTimer);
    closeTimer = window.setTimeout(() => {
      if (!activeSegment?.matches(':hover, :focus-visible') && !tooltip.matches(':hover')) hideTooltip();
    }, 160);
  }
  target.querySelectorAll('[data-focus-reason]').forEach(segment => {
    segment.addEventListener('pointerenter', () => showTooltip(segment));
    segment.addEventListener('pointerleave', scheduleHide);
    segment.addEventListener('focus', () => showTooltip(segment));
    segment.addEventListener('blur', scheduleHide);
  });
  tooltip.addEventListener('pointerenter', () => window.clearTimeout(closeTimer));
  tooltip.addEventListener('pointerleave', scheduleHide);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTooltip(); });
  window.addEventListener('scroll', hideTooltip, { passive: true });
  window.addEventListener('resize', hideTooltip);
})();
