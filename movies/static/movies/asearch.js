// static/movies/search.js
(() => {
  const box = document.getElementById('searchBox');
  if (!box) return;

  let timer = null;
  const wait = 300; // ms debounce

  box.addEventListener('input', (e) => {
    clearTimeout(timer);
    const q = box.value.trim();

    timer = setTimeout(() => {
      const url = '/search_ajax/?q=' + encodeURIComponent(q);
      fetch(url, { credentials: 'same-origin' })
        .then(resp => {
          if (!resp.ok) throw new Error('Network response not ok');
          return resp.text();
        })
        .then(html => {
          const grid = document.getElementById('movieGrid');
          if (grid) grid.innerHTML = html;
        })
        .catch(err => {
          // very small fallback: client-side filter if server fails
          console.warn('Live search failed, falling back to client filter', err);
          clientSideFilter(q);
        });
    }, wait);
  });

  // fallback: simple client-side filter (works only on currently loaded cards)
  function clientSideFilter(q) {
    const cards = document.querySelectorAll('#movieGrid .card');
    if (!cards) return;
    const term = q.toLowerCase();
    cards.forEach(c => {
      const title = (c.dataset.title || c.querySelector('.title')?.textContent || '').toLowerCase();
      c.style.display = title.includes(term) ? '' : 'none';
    });
  }
})();