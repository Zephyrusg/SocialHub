'use strict';
document.documentElement.classList.add('js');
const filters = document.querySelector('[data-feature-filters]');
if (filters) {
  const search = document.getElementById('feature-search');
  const category = document.getElementById('feature-category');
  const state = document.getElementById('feature-state');
  const rows = [...document.querySelectorAll('[data-feature]')];
  const apply = () => {
    const query = search.value.trim().toLocaleLowerCase();
    let count = 0;
    for (const row of rows) {
      const match = (!query || row.dataset.search.includes(query)) &&
        (!category.value || row.dataset.category === category.value) &&
        (!state.value || row.dataset.state === state.value);
      row.hidden = !match;
      if (match) count++;
    }
    document.getElementById('feature-count').textContent = `${count} of ${rows.length} features`;
    document.getElementById('feature-empty').hidden = count !== 0;
  };
  search.addEventListener('input', apply);
  category.addEventListener('change', apply);
  state.addEventListener('change', apply);
  filters.addEventListener('submit', event => { event.preventDefault(); apply(); });
  document.querySelector('[data-clear-filters]').addEventListener('click', () => {
    search.value = ''; category.value = ''; state.value = ''; apply(); search.focus();
  });
  apply();
}
document.addEventListener('submit', event => {
  const form = event.target;
  if (event.defaultPrevented || form.method.toLowerCase() !== 'post') return;
  form.setAttribute('aria-busy', 'true');
  const button = event.submitter;
  if (button) {
    button.dataset.originalLabel = button.innerHTML;
    button.disabled = true;
    if (button.dataset.loadingLabel) button.textContent = button.dataset.loadingLabel;
  }
});
window.addEventListener('pageshow', () => {
  for (const button of document.querySelectorAll('[data-original-label]')) {
    button.innerHTML = button.dataset.originalLabel;
    button.disabled = false;
    delete button.dataset.originalLabel;
  }
  for (const form of document.querySelectorAll('[aria-busy]')) form.removeAttribute('aria-busy');
});
