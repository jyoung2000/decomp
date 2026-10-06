/* Pocket Notes: vanilla JS, hash routes (#/notes, #/about), localStorage state, SW registration. */
(function () {
  'use strict';
  var APP_VERSION = '1.0.0';
  var STORAGE_KEY = 'pocket-notes:v1';
  var SIMULATE_QUOTA = new URLSearchParams(location.search).has('simulate-quota');

  var state = load();
  var appEl = document.getElementById('app');
  var errorEl = document.getElementById('error');
  var bannerEl = document.getElementById('banner');
  var swStatusEl = document.getElementById('sw-status');

  function load() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && Array.isArray(parsed.notes)) return parsed;
      }
    } catch (e) { /* fall through to defaults */ }
    return { nextId: 1, notes: [] };
  }

  /* Returns true when persisted. On failure (quota) shows the error state and returns false. */
  function save(next) {
    try {
      if (SIMULATE_QUOTA) throw new DOMException('Simulated quota exceeded', 'QuotaExceededError');
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      state = next;
      showError('');
      return true;
    } catch (e) {
      showError('Storage is full. Your latest change was not saved.');
      return false;
    }
  }

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = !msg;
  }

  function showBanner(msg) {
    bannerEl.textContent = msg;
    bannerEl.hidden = !msg;
  }

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function renderNotes() {
    appEl.textContent = '';
    appEl.appendChild(el('h2', {}, 'Your notes'));
    var form = el('form', { id: 'note-form' });
    var input = el('input', { id: 'note-input', type: 'text', 'aria-label': 'New note', maxlength: '200', placeholder: 'Write a note' });
    form.appendChild(input);
    form.appendChild(el('button', { id: 'add-btn', type: 'submit' }, 'Add'));
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      var next = { nextId: state.nextId + 1, notes: state.notes.concat([{ id: state.nextId, text: text }]) };
      if (save(next)) renderNotes();
    });
    appEl.appendChild(form);
    appEl.appendChild(el('p', { id: 'count' }, state.notes.length + (state.notes.length === 1 ? ' note' : ' notes')));
    if (state.notes.length === 0) {
      appEl.appendChild(el('p', { id: 'empty' }, 'No notes yet.'));
    }
    var list = el('ul', { id: 'note-list' });
    state.notes.forEach(function (n) {
      var li = el('li', { 'data-id': String(n.id) });
      li.appendChild(el('span', { class: 'text' }, n.text));
      var del = el('button', { class: 'delete', type: 'button', 'aria-label': 'Delete note ' + n.id }, 'Delete');
      del.addEventListener('click', function () {
        var next = { nextId: state.nextId, notes: state.notes.filter(function (x) { return x.id !== n.id; }) };
        if (save(next)) renderNotes();
      });
      li.appendChild(del);
      list.appendChild(li);
    });
    appEl.appendChild(list);
  }

  function renderAbout() {
    appEl.textContent = '';
    appEl.appendChild(el('h2', {}, 'About'));
    appEl.appendChild(el('p', { id: 'version' }, 'Pocket Notes ' + APP_VERSION));
    appEl.appendChild(el('p', { id: 'storage-info' }, 'Notes stored: ' + state.notes.length));
    var cacheEl = el('p', { id: 'cache-info' }, 'Cache: none');
    appEl.appendChild(cacheEl);
    if (window.caches) {
      caches.keys().then(function (keys) {
        cacheEl.textContent = 'Cache: ' + (keys.length ? keys.sort().join(', ') : 'none');
      });
    }
  }

  function route() {
    var path = (location.hash || '#/notes').replace(/^#/, '');
    var isAbout = path === '/about';
    document.getElementById('nav-notes').className = isAbout ? '' : 'active';
    document.getElementById('nav-about').className = isAbout ? 'active' : '';
    document.title = isAbout ? 'About - Pocket Notes' : 'Pocket Notes';
    if (isAbout) renderAbout(); else renderNotes();
  }

  window.addEventListener('hashchange', route);
  route();

  /* Service worker (http/https only; not under file:// as in the Electron package). */
  if ('serviceWorker' in navigator && /^https?:$/.test(location.protocol)) {
    var hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener('controllerchange', function () {
      if (hadController) showBanner('A new version was installed.');
      hadController = true;
      swStatusEl.textContent = 'offline support: ready';
    });
    navigator.serviceWorker.register('sw.js').then(function () {
      return navigator.serviceWorker.ready;
    }).then(function () {
      swStatusEl.textContent = 'offline support: ready';
    }).catch(function () {
      swStatusEl.textContent = 'offline support: unavailable';
    });
  } else {
    swStatusEl.textContent = 'offline support: unavailable';
  }
})();
