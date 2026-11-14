(() => {
  'use strict';
  const button = document.querySelector('[data-open-methodology]');
  if (!button) return;
  let loading = null;
  async function open(event) {
    event.stopImmediatePropagation();
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try {
      loading ||= new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = button.dataset.methodologyAppSrc;
        script.onload = resolve;
        script.onerror = () => { script.remove(); reject(new Error('Methodology unavailable')); };
        document.head.append(script);
      });
      await loading;
      button.removeEventListener('click', open);
      button.disabled = false;
      button.removeAttribute('aria-busy');
      // app.js now owns the shared, accessible methodology dialog and its
      // retry handling. Hand the original request to that same implementation.
      button.click();
    } catch {
      loading = null;
      const dialog = document.querySelector('#methodology-dialog');
      document.querySelector('#methodology-dialog-content').textContent = 'Methodology could not be loaded. Please try again.';
      dialog.showModal();
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }
  button.addEventListener('click', open);
  document.querySelector('#methodology-dialog-close')?.addEventListener('click', () => {
    document.querySelector('#methodology-dialog').close();
  });
})();
