/* Drafts: anything you type into a form on HQ is kept on this device until the form
   is actually submitted. Refresh, switch apps, come back tomorrow - it is still there.
   Passwords, searches, files and one-off codes are left alone. */
(function () {
  if (!('localStorage' in window)) return;
  var PREFIX = 'hqdraft:' + location.pathname + ':';
  var SKIP_TYPES = {password: 1, hidden: 1, file: 1, search: 1, submit: 1, button: 1, checkbox: 1, radio: 1};
  var SKIP_NAMES = /^(code|current|new|password|q|username|totp|csrf)/i;

  function fields(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      if (!el.name || el.disabled) return false;
      if (el.tagName === 'SELECT') return false;
      if (el.tagName === 'TEXTAREA') return true;
      if (el.tagName !== 'INPUT') return false;
      if (SKIP_TYPES[el.type]) return false;
      if (SKIP_NAMES.test(el.name)) return false;
      if (el.autocomplete && /password|one-time-code/.test(el.autocomplete)) return false;
      return true;
    });
  }
  function keyOf(form, el) {
    var id = form.id || form.getAttribute('action') || '';
    var uid = form.querySelector('input[name=uid]'); if (uid) id += '#' + uid.value;
    return PREFIX + id + ':' + el.name;
  }
  function save(form, el) {
    try {
      var k = keyOf(form, el), v = el.value;
      if (v === el.defaultValue || v === '') localStorage.removeItem(k);
      else localStorage.setItem(k, JSON.stringify({v: v, t: Date.now()}));
    } catch (e) {}
  }
  function restore(form) {
    var n = 0;
    fields(form).forEach(function (el) {
      try {
        var raw = localStorage.getItem(keyOf(form, el)); if (!raw) return;
        var d = JSON.parse(raw);
        if (Date.now() - d.t > 14 * 86400000) { localStorage.removeItem(keyOf(form, el)); return; }
        if (el.value === el.defaultValue && d.v !== el.value) { el.value = d.v; el.classList.add('draft-restored'); n++; }
      } catch (e) {}
    });
    if (n) {
      var note = document.createElement('div');
      note.className = 'draft-note';
      note.textContent = n === 1 ? 'Unsaved text restored.' : 'Unsaved text restored in ' + n + ' fields.';
      var btn = document.createElement('button'); btn.type = 'button'; btn.textContent = 'Clear';
      btn.onclick = function () { fields(form).forEach(function (el) { if (el.classList.contains('draft-restored')) { el.value = el.defaultValue; el.classList.remove('draft-restored'); } localStorage.removeItem(keyOf(form, el)); }); note.remove(); };
      note.appendChild(btn);
      form.insertBefore(note, form.firstChild);
    }
  }
  function forget(form) { fields(form).forEach(function (el) { try { localStorage.removeItem(keyOf(form, el)); } catch (e) {} }); }

  Array.prototype.forEach.call(document.querySelectorAll('form'), function (form) {
    if (form.method && form.method.toLowerCase() === 'get') return;   // searches, filters
    if (form.hasAttribute('data-no-draft')) return;
    var fs = fields(form); if (!fs.length) return;
    restore(form);
    fs.forEach(function (el) { el.addEventListener('input', function () { save(form, el); }); });
    form.addEventListener('submit', function () { forget(form); });
    form.addEventListener('reset', function () { forget(form); });
  });
})();
