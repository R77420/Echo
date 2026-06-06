/**
 * app.js — Communication bridge Python ↔ JS pour pywebview.
 * Inclus dans toutes les pages.
 */

'use strict';

/* ------------------------------------------------------------------ */
/*  Attente pywebview                                                  */
/* ------------------------------------------------------------------ */

function onReady(cb) {
  if (window.pywebview && window.pywebview.api) { cb(); return; }
  window.addEventListener('pywebviewready', function h() {
    window.removeEventListener('pywebviewready', h);
    cb();
  });
}

/* ------------------------------------------------------------------ */
/*  Appels API                                                         */
/* ------------------------------------------------------------------ */

async function api(method, ...args) {
  try {
    return await window.pywebview.api[method](...args);
  } catch (err) {
    console.error('[api] ' + method + ':', err);
    return null;
  }
}

/* ------------------------------------------------------------------ */
/*  Polling get_updates()                                              */
/* ------------------------------------------------------------------ */

let _pollTimer = null;

function startPolling(onUpdates, ms = 200) {
  stopPolling();
  (async function loop() {
    const items = await api('get_updates');
    if (Array.isArray(items) && items.length > 0) {
      console.log('[poll]', items.length, 'update(s)', items);
      onUpdates(items);
    }
    _pollTimer = setTimeout(loop, ms);
  })();
}

function stopPolling() {
  if (_pollTimer !== null) { clearTimeout(_pollTimer); _pollTimer = null; }
}

/* ------------------------------------------------------------------ */
/*  Utilitaires DOM                                                    */
/* ------------------------------------------------------------------ */

const $ = id => document.getElementById(id);
const show = el => el && el.classList.remove('hidden');
const hide = el => el && el.classList.add('hidden');

function showBanner(el, msg, type = 'warn') {
  if (!el) return;
  el.textContent = msg;
  el.className = 'o-banner o-banner-' + type;
  show(el);
}
function clearBanner(el) {
  if (!el) return; el.textContent = ''; hide(el);
}

/** Peuple un <select> avec une liste de noms, préselectionne `selected`. */
function populateSelect(sel, items, selected) {
  if (!sel) return;
  sel.innerHTML = '';
  (items || []).forEach(name => {
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    if (name === selected) opt.selected = true;
    sel.appendChild(opt);
  });
  if (!items || items.length === 0) {
    const opt = document.createElement('option');
    opt.value = ''; opt.textContent = '(aucun)';
    sel.appendChild(opt);
  }
}

/** Formate une date ISO en "Aujourd'hui HHhMM" ou "JJ/MM/AAAA". */
function formatDate(isoStr) {
  try {
    const d = new Date(isoStr);
    const now = new Date();
    const pad = n => String(n).padStart(2,'0');
    const hhmm = pad(d.getHours()) + 'h' + pad(d.getMinutes());
    if (d.toDateString() === now.toDateString()) return 'Aujourd\'hui ' + hhmm;
    const hier = new Date(now); hier.setDate(now.getDate()-1);
    if (d.toDateString() === hier.toDateString()) return 'Hier ' + hhmm;
    return pad(d.getDate()) + '/' + pad(d.getMonth()+1) + '/' + d.getFullYear() + ' ' + hhmm;
  } catch { return isoStr; }
}

/** Échappe le HTML pour affichage. */
function esc(str) {
  return (str||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
