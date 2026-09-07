/* Doplňky k HTMX: sheet/modal, filtr, hold-to-delete, toasty. */

const HOLD_MS = 900;

function closePanel() { document.getElementById('panel').innerHTML = ''; }
function closeModal() { document.getElementById('modal').innerHTML = ''; }

/* Filtr uvnitř aktuálního výpisu — klientský, bez requestu. */
function filterView(query) {
  const needle = (query || '').trim().toLowerCase();
  document.querySelectorAll('#results .cell').forEach((cell) => {
    cell.classList.toggle('hide', !!needle && !(cell.dataset.name || '').includes(needle));
  });
}

/* Navigace swapuje jen #view, takže zvýraznění tabu musí dorovnat JS. */
function syncTabs() {
  const path = location.pathname;
  document.querySelectorAll('.bar-item, .tab').forEach((a) => {
    a.classList.toggle('on', a.getAttribute('href') === path);
  });
}

document.addEventListener('DOMContentLoaded', syncTabs);
document.body.addEventListener('htmx:afterSwap', syncTabs);
document.body.addEventListener('htmx:historyRestore', syncTabs);

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('modal').innerHTML.trim()) closeModal();
  else closePanel();
});

/* Mazání jde jen podržením — proti omylu na dotykové obrazovce. */
let holdTimer = null;

/* pointerleave a spol. umí přijít i s document jako cílem, a ten closest nemá. */
function holdButton(target) {
  return target instanceof Element ? target.closest('[data-hold]') : null;
}

document.addEventListener('pointerdown', (e) => {
  const btn = holdButton(e.target);
  if (!btn) return;
  e.preventDefault();
  btn.classList.add('holding');
  navigator.vibrate?.(8);
  holdTimer = setTimeout(() => {
    btn.classList.remove('holding');
    navigator.vibrate?.([12, 40, 12]);
    btn.disabled = true;
    btn.querySelector('.hold-label').textContent = 'Mažu…';
    btn.closest('form').requestSubmit();
  }, HOLD_MS);
});

['pointerup', 'pointercancel', 'pointerleave'].forEach((name) => {
  document.addEventListener(name, (e) => {
    const btn = holdButton(e.target);
    if (btn && holdTimer) {
      clearTimeout(holdTimer);
      holdTimer = null;
      btn.classList.remove('holding');
    }
  });
});

/* Toasty samy odcházejí. */
function reapToasts() {
  document.querySelectorAll('.toast:not([data-reaped])').forEach((t) => {
    t.dataset.reaped = '1';
    setTimeout(() => {
      t.classList.add('out');
      setTimeout(() => t.remove(), 300);
    }, t.classList.contains('toast-error') ? 7000 : 3200);
  });
}

document.body.addEventListener('htmx:afterSwap', reapToasts);
document.body.addEventListener('htmx:oobAfterSwap', reapToasts);

/* Síťová chyba nesmí skončit tichem. */
function toast(message, kind = 'ok') {
  const host = document.getElementById('toasts');
  if (!host) return;
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.innerHTML = `<span class="toast-icon">${kind === 'error' ? '!' : '✓'}</span>`;
  el.appendChild(Object.assign(document.createElement('span'), { textContent: message }));
  el.onclick = () => el.remove();
  host.appendChild(el);
  reapToasts();
}

document.body.addEventListener('htmx:sendError', () => toast('Server neodpovídá.', 'error'));
document.body.addEventListener('htmx:responseError', (e) =>
  toast(`Chyba serveru (HTTP ${e.detail.xhr.status}).`, 'error'));

document.body.addEventListener('htmx:historyRestore', () => { closePanel(); closeModal(); });
