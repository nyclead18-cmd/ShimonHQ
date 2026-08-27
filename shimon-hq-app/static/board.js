// jump to a task linked from Joel / People / Calendar — after layout settles
function jumpToLinkedItem() {
  var m = /^#item-(\d+)$/.exec(location.hash || '');
  if (!m) return;
  var el = document.getElementById('item-' + m[1]);
  if (!el) return;
  var sec = el.closest('section.card');
  if (sec) { sec.classList.add('show-done'); }   // reveal it if it is a done row
  el.classList.remove('flash');
  var land = function () {
    el.scrollIntoView({block: 'center', behavior: 'auto'});
    void el.offsetWidth;
    el.classList.add('flash');
  };
  land();
  if (document.fonts && document.fonts.ready) { document.fonts.ready.then(land); }
  setTimeout(land, 350);
}
window.addEventListener('hashchange', jumpToLinkedItem);
window.addEventListener('load', jumpToLinkedItem);

var MODE = 'all';
var q = document.getElementById('q');

function applyFilter() {
  var needle = (q && q.value ? q.value : '').toLowerCase().trim();
  var filtering = needle !== '' || MODE !== 'all';
  // "Mine" hides sections other people have shared in. Somebody running this
  // with a dozen colleagues needs to be able to see only their own work without
  // anybody having to unshare anything.
  var mineOnly = MODE === 'mine';
  document.querySelectorAll('li.item').forEach(function (li) {
    var okText = !needle || li.textContent.toLowerCase().indexOf(needle) !== -1;
    var st = li.classList.contains('open') ? 'open' : li.classList.contains('waiting') ? 'waiting' : 'done';
    var sec = li.closest('section.card');
    var okOwner = !mineOnly || !sec || sec.dataset.mine === '1';
    var okMode = (MODE === 'all' || mineOnly)
      ? (st !== 'done' || (sec && sec.classList.contains('show-done')) || needle)
      : st === MODE;
    li.style.display = (okText && okMode && okOwner)
      ? (li.classList.contains('donerow') ? 'grid' : '')
      : 'none';
  });
  document.querySelectorAll('.project').forEach(function (pr) {
    var any = Array.prototype.some.call(pr.querySelectorAll('li.item'), function (li) { return li.style.display !== 'none'; });
    pr.style.display = (any || !filtering) ? '' : 'none';
  });
  // the tiles count what the board is currently showing - saying "10 on me"
  // above four visible tasks reads as a bug even when it is not
  var tally = {open: 0, waiting: 0, done: 0};
  document.querySelectorAll('li.item').forEach(function (li) {
    var sec = li.closest('section.card');
    if (mineOnly && sec && sec.dataset.mine !== '1') { return; }
    var st = li.classList.contains('open') ? 'open'
           : li.classList.contains('waiting') ? 'waiting' : 'done';
    tally[st] += 1;
  });
  document.querySelectorAll('.stat .num[data-count]').forEach(function (n) {
    n.textContent = tally[n.getAttribute('data-count')];
  });
  document.querySelectorAll('section.card').forEach(function (sec) {
    if (mineOnly && sec.dataset.mine !== '1') { sec.style.display = 'none'; return; }
    var any = Array.prototype.some.call(sec.querySelectorAll('li.item'), function (li) { return li.style.display !== 'none'; });
    sec.style.display = (any || !filtering) ? '' : 'none';
  });
}
window.applyFilter = applyFilter;   // the gesture layer re-runs it after a swipe
if (q) { q.addEventListener('input', applyFilter); }
document.querySelectorAll('.fpill').forEach(function (b) {
  b.addEventListener('click', function () {
    document.querySelectorAll('.fpill').forEach(function (x) { x.classList.remove('on'); });
    b.classList.add('on');
    MODE = b.getAttribute('data-f');
    applyFilter();
  });
});

document.addEventListener('click', function (ev) {
  var pill = ev.target.closest('.pill');
  if (pill) {
    var id = pill.getAttribute('data-id');
    fetch('/items/' + id + '/cycle', {method: 'POST'})
      .then(function (r) { return r.json(); })
      .then(function (d) {
        pill.className = 'pill ' + d.status;
        pill.textContent = d.status === 'open' ? 'Open' : d.status === 'waiting' ? 'Waiting' : 'Done';
        var li = pill.closest('li');
        li.classList.remove('open', 'waiting', 'done', 'donerow');
        li.classList.add(d.status);
        if (d.status === 'done') { li.classList.add('donerow'); }
        applyFilter();
      });
    return;
  }
  var tog = ev.target.closest('[data-toggle-done]');
  if (tog) {
    document.getElementById('sec-' + tog.getAttribute('data-toggle-done')).classList.toggle('show-done');
    applyFilter();
  }
  var del = ev.target.closest('.del');
  if (del && del.classList.contains('nowarn')) { return; }   // briefing dismiss: one tap
  if (del && !del.classList.contains('arm')) {
    ev.preventDefault();
    del.classList.add('arm');
    del.textContent = 'sure?';
    setTimeout(function () { del.classList.remove('arm'); del.textContent = '✕'; }, 2500);
    return;
  }
  // armed response-delete: remove in place, no reload
  if (del && del.classList.contains('arm') && del.closest('.ndelform')) {
    ev.preventDefault();
    var f = del.closest('form');
    fetch(f.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}});
    var noteLi = f.closest('li');
    var list = noteLi.parentElement;
    noteLi.remove();
    if (!list.querySelector('li')) { list.remove(); }
    return;
  }
  // armed file-delete: remove chip in place
  if (del && del.classList.contains('arm') && del.closest('.fdelform')) {
    ev.preventDefault();
    var ff = del.closest('form');
    fetch(ff.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}});
    var chip = ff.closest('.fchip');
    var box = chip.parentElement;
    chip.remove();
    if (!box.querySelector('.fchip')) { box.hidden = true; }
  }
});

// file uploads save in place
document.addEventListener('change', function (ev) {
  var inp = ev.target.closest('.fileinput');
  if (!inp || !inp.files.length) return;
  var id = inp.getAttribute('data-id');
  var label = inp.closest('label');
  label.classList.add('busy');
  var fd = new FormData();
  fd.append('file', inp.files[0]);
  fetch('/items/' + id + '/files', {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body: fd})
    .then(function (r) { return r.json(); })
    .then(function (d) {
      label.classList.remove('busy');
      if (!d.id) { alert(d.error || 'Upload failed'); return; }
      var body = inp.closest('.body');
      var box = body.querySelector('.files');
      box.hidden = false;
      var chip = document.createElement('span');
      chip.className = 'fchip';
      var a = document.createElement('a');
      a.href = '/files/' + d.id; a.target = '_blank'; a.rel = 'noopener'; a.textContent = d.filename;
      var df = document.createElement('form');
      df.className = 'fdelform'; df.method = 'post'; df.action = '/files/' + d.id + '/delete';
      df.innerHTML = '<button type="submit" class="del fdel" title="Remove file">✕</button>';
      chip.appendChild(a); chip.appendChild(df);
      box.appendChild(chip);
      inp.value = '';
    })
    .catch(function () { label.classList.remove('busy'); });
});

// matches the server's `stamp` filter: 8/26 - 9:34 AM
function stampOf(iso) {
  if (!iso || iso.length < 16) { return (iso || '').slice(0, 10); }
  var mo = parseInt(iso.slice(5, 7), 10), da = parseInt(iso.slice(8, 10), 10);
  var h = parseInt(iso.slice(11, 13), 10), mi = iso.slice(14, 16);
  var ap = h < 12 ? 'AM' : 'PM';
  return mo + '/' + da + ' \u00b7 ' + ((h % 12) || 12) + ':' + mi + ' ' + ap;
}

// responses save in place — no page reload
document.addEventListener('submit', function (ev) {
  var form = ev.target.closest('form.respond');
  if (!form) return;
  ev.preventDefault();
  var input = form.querySelector('input[name=body]');
  var body = (input.value || '').trim();
  if (!body) return;
  var sendBtn = form.querySelector('button[type=submit]');
  if (sendBtn) { sendBtn.disabled = true; }
  function failed(msg) {
    if (sendBtn) { sendBtn.disabled = false; }
    var warn = form.parentElement.querySelector('.savewarn');
    if (!warn) {
      warn = document.createElement('div');
      warn.className = 'savewarn';
      form.parentElement.appendChild(warn);
    }
    warn.textContent = msg;          // the text stays in the box, ready to resend
    input.focus();
  }
  fetch(form.action, {
    method: 'POST',
    headers: {'X-Requested-With': 'fetch'},
    body: new FormData(form)
  }).then(function (r) {
    if (!r.ok) { throw new Error(r.status === 503 ? 'busy' : 'http ' + r.status); }
    return r.json();
  }).then(function (d) {
    if (sendBtn) { sendBtn.disabled = false; }
    var oldWarn = form.parentElement.querySelector('.savewarn');
    if (oldWarn) { oldWarn.remove(); }
    if (!d.id) { failed('Not saved. Tap the arrow to try again.'); return; }
    var itemBody = form.closest('.body');
    var list = itemBody.querySelector('ul.notes');
    if (!list) {
      list = document.createElement('ul');
      list.className = 'notes';
      itemBody.insertBefore(list, itemBody.querySelector('.files'));
    }
    var li = document.createElement('li');
    li.className = 'noterow';
    li.id = 'note-' + d.id;
    var nd = document.createElement('span'); nd.className = 'nd'; nd.textContent = stampOf(d.created_at);
    var tx = document.createElement('span'); tx.className = 't'; tx.setAttribute('dir', 'auto'); tx.textContent = d.body;
    var df = document.createElement('form');
    df.className = 'ndelform'; df.method = 'post'; df.action = '/notes/' + d.id + '/delete';
    df.innerHTML = '<button type="submit" class="del ndel" title="Remove response">✕</button>';
    li.appendChild(nd); li.appendChild(tx); li.appendChild(df);
    list.appendChild(li);
    input.value = '';
    input.focus();
  }).catch(function (e) {
    failed(String(e.message) === 'busy'
      ? 'Busy for a moment. Tap the arrow to send it again.'
      : 'Could not save that. Tap the arrow to try again.');
  });
});

jumpToLinkedItem();

/* ---------- push notifications ---------- */
(function () {
  var btn = document.getElementById('notifbtn');
  if (!btn) return;

  function setLabel(state) {
    btn.textContent = state === 'on' ? '⏰ On'
                    : state === 'busy' ? '…'
                    : state === 'blocked' ? '⏰ Blocked'
                    : '⏰ Notify me';
    btn.classList.toggle('on', state === 'on');
  }

  function b64ToUint8(base64) {
    var pad = '='.repeat((4 - base64.length % 4) % 4);
    var raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
    return out;
  }

  var supported = ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
  if (!supported) { setLabel('off'); btn.title = 'This browser cannot do push notifications'; }

  navigator.serviceWorker && navigator.serviceWorker.ready.then(function (reg) {
    return reg.pushManager.getSubscription();
  }).then(function (sub) {
    setLabel(sub ? 'on' : (Notification.permission === 'denied' ? 'blocked' : 'off'));
  }).catch(function () { setLabel('off'); });

  btn.addEventListener('click', function () {
    if (!supported) {
      alert('Open the site from your Home Screen app (Add to Home Screen) to turn on notifications.');
      return;
    }
    if (btn.classList.contains('on')) {
      fetch('/push/test', {method: 'POST'}).then(function (r) { return r.json(); })
        .then(function (d) { if (!d.sent) { alert('No device is subscribed yet.'); } });
      return;
    }
    setLabel('busy');
    Notification.requestPermission().then(function (perm) {
      if (perm !== 'granted') { setLabel(perm === 'denied' ? 'blocked' : 'off'); return; }
      return fetch('/push/key').then(function (r) { return r.json(); }).then(function (d) {
        if (!d.key) { setLabel('off'); alert('Push keys are not ready on the server yet.'); return; }
        return navigator.serviceWorker.ready.then(function (reg) {
          return reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: b64ToUint8(d.key)
          });
        }).then(function (sub) {
          return fetch('/push/subscribe', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(sub)
          });
        }).then(function () {
          setLabel('on');
          return fetch('/push/test', {method: 'POST'});
        });
      });
    }).catch(function () { setLabel('off'); });
  });
})();

/* ---------- the edit form, built when you actually open it ----------
   Rendering it under every row cost more than a third of the page, for a form
   that is almost never opened. One copy lives in a <template>; opening "edit"
   clones it and fills it from the row's own data attributes. */
(function () {
  // Looked up when first needed, not now: this script is loaded before the
  // <template> it clones, so grabbing it at startup would find nothing and
  // silently disable editing everywhere.
  function template() { return document.getElementById('edittpl'); }

  function build(box) {
    if (box.dataset.built) { return; }
    var tpl = template();
    if (!tpl) { return; }
    var li = box.closest('li.item');
    var id = box.dataset.id;
    if (!li || !id) { return; }
    box.dataset.built = '1';
    var frag = tpl.content.cloneNode(true);
    var edit = frag.querySelector('form.edit');
    var del = frag.querySelector('form.delform');
    edit.action = '/items/' + id + '/edit';
    del.action = '/items/' + id + '/delete';
    var d = li.dataset;
    function set(name, value) {
      var f = edit.querySelector('[name=' + name + ']');
      if (f) { f.value = value || ''; }
    }
    function txt(sel, strip) {
      var el = li.querySelector(sel);
      if (!el) { return ''; }
      var v = (el.textContent || '').trim();
      return strip && v.indexOf(strip) === 0 ? v.slice(strip.length).trim() : v;
    }
    // read these off the row rather than repeating them in an attribute - the
    // same text twice on every row was a sixth of the page
    set('title', txt('.t .tt'));
    set('note', txt('.t .n', '\u2014'));
    set('waiting_on', txt('.metaline .w b'));
    set('due_date', d.due); set('remind_at', d.rem);
    set('section_id', d.sec); set('project_id', d.proj);
    box.appendChild(frag);
  }

  // `toggle` does not bubble, so listen in the capture phase
  document.addEventListener('toggle', function (ev) {
    var box = ev.target;
    if (box && box.classList && box.classList.contains('editbox') && box.open) {
      build(box);
    }
  }, true);

  // a tap on the summary should have the form ready before the panel opens
  document.addEventListener('pointerdown', function (ev) {
    var sum = ev.target.closest && ev.target.closest('.editbox > summary');
    if (sum) { build(sum.parentElement); }
  }, true);
})();
