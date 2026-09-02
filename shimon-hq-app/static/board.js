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

/* A tap on the pill no longer flips the status - one stray tap was closing
   tasks and they would vanish from the list. The pill now opens a small menu
   and the status only changes when a choice is actually made. */
function closeStatusMenu() {
  var m = document.querySelector('.statusmenu');
  if (m) { m.remove(); }
}
function openStatusMenu(pill) {
  closeStatusMenu();
  var li = pill.closest('li.item');
  var id = pill.getAttribute('data-id');
  var menu = document.createElement('div');
  menu.className = 'statusmenu';
  [['open', 'Open'], ['waiting', 'Waiting'], ['done', 'Done']].forEach(function (s) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'statuschoice ' + s[0] + (pill.classList.contains(s[0]) ? ' now' : '');
    b.textContent = s[1];
    b.addEventListener('click', function (e) {
      e.stopPropagation();
      closeStatusMenu();
      if (pill.classList.contains(s[0])) { return; }   // already there - nothing to do
      var fd = new FormData();
      fd.append('status', s[0]);
      fetch('/items/' + id + '/status', {method: 'POST', body: fd})
        .then(function (r) { return r.json(); })
        .then(function (d) {
          pill.className = 'pill ' + d.status;
          pill.textContent = d.status === 'open' ? 'Open' : d.status === 'waiting' ? 'Waiting' : 'Done';
          li.classList.remove('open', 'waiting', 'done', 'donerow');
          li.classList.add(d.status);
          if (d.status === 'done') { li.classList.add('donerow'); }
          if (window.applyFilter) { window.applyFilter(); }
        });
    });
    menu.appendChild(b);
  });
  li.appendChild(menu);
  menu.style.top = (pill.offsetTop + pill.offsetHeight + 3) + 'px';
  menu.style.left = pill.offsetLeft + 'px';
}
document.addEventListener('click', function (ev) {
  var pill = ev.target.closest('.pill');
  if (pill && pill.getAttribute('data-id')) {
    ev.preventDefault();
    var open = document.querySelector('.statusmenu');
    if (open && open.closest('li.item') === pill.closest('li.item')) { closeStatusMenu(); }
    else { openStatusMenu(pill); }
    return;
  }
  if (!ev.target.closest('.statusmenu')) { closeStatusMenu(); }
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

  // show him back what he typed - 30000000 in a box he wrote "30m" into is a
  // small insult every time he opens it
  function short(v) {
    var n = parseFloat(v);
    if (!n) { return ''; }
    if (n >= 1e9 && n % 1e8 === 0) { return (n / 1e9) + 'b'; }
    if (n >= 1e6 && n % 1e5 === 0) { return (n / 1e6) + 'm'; }
    if (n >= 1e3 && n % 1e2 === 0) { return (n / 1e3) + 'k'; }
    return String(n);
  }

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
    // deal numbers only where they mean something
    var deal = edit.querySelector('.dealfields');
    if (deal && d.kind === 'pipeline') {
      deal.hidden = false;
      var amt = edit.querySelector('[name=amount]');
      var eb = edit.querySelector('[name=ebitda]');
      if (amt) { amt.value = short(d.amount); }
      if (eb) { eb.value = short(d.ebitda); }
      set('units', d.units); set('tenure', d.tenure); set('stage', d.stage);
    }
    // list chips: light the people this task is already with, keep the hidden
    // field in step as chips are toggled
    var lt = edit.querySelector('[name=ltags]');
    if (lt) {
      var on = (d.ltags || '').split(',').filter(Boolean);
      lt.value = on.join(',');
      frag.querySelectorAll('.lchip').forEach(function (c) {
        if (on.indexOf(c.getAttribute('data-luid')) !== -1) { c.classList.add('on'); }
        c.addEventListener('click', function () {
          c.classList.toggle('on');
          var now = [];
          edit.querySelectorAll('.lchip.on').forEach(function (x) {
            now.push(x.getAttribute('data-luid'));
          });
          lt.value = now.join(',');
        });
      });
    }
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

/* ---------- Pulse: update the read without a page reload ---------- */
(function () {
  var form = document.querySelector('.refreshform');
  if (!form) { return; }
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var btn = form.querySelector('button');
    var body = document.getElementById('readbody');
    var empty = document.getElementById('readempty');
    var pending = document.getElementById('readpending');
    btn.disabled = true;
    btn.textContent = 'reading…';
    fetch(form.action, {
      method: 'POST',
      headers: {'X-Requested-With': 'fetch'},
      body: new FormData(form)
    }).then(function (r) {
      if (!r.ok) { throw new Error('http ' + r.status); }
      return r.json();
    }).then(function (d) {
      if (body) { body.textContent = d.body; }
      if (empty) { empty.hidden = true; }
      if (pending) { pending.hidden = false; }
      btn.disabled = false;
      btn.textContent = 'Update now';
    }).catch(function () {
      // say so rather than leave a button that looks like it worked
      btn.disabled = false;
      btn.textContent = 'try again';
      setTimeout(function () { btn.textContent = 'Update now'; }, 2500);
    });
  });
})();

/* ---------- the Today star ---------- */
document.addEventListener('click', function (ev) {
  var b = ev.target.closest && ev.target.closest('.star');
  if (!b) { return; }
  ev.preventDefault();
  var id = b.getAttribute('data-star');
  if (!id || b.dataset.busy) { return; }
  b.dataset.busy = '1';
  fetch('/items/' + id + '/today', {
    method: 'POST', headers: {'X-Requested-With': 'fetch'}
  }).then(function (r) {
    if (!r.ok) { throw new Error('http ' + r.status); }
    return r.json();
  }).then(function (d) {
    b.classList.toggle('on', !!d.today);
    b.innerHTML = d.today ? '&#9733;' : '&#9734;';
    b.title = d.today ? 'On today' : 'Put on today';
    // a star just added has been there no time at all
    if (!d.today) {
      var li = b.closest('li.item');
      var tag = li && li.querySelector('.staleday');
      if (tag) { tag.remove(); }
    }
    delete b.dataset.busy;
  }).catch(function () {
    b.title = 'Could not save that';
    delete b.dataset.busy;
  });
}, false);

/* ---------- the initials button: one tap on / off the list with the person
   across the table (JL on Shimon's board, SH on Joel's) ---------- */
document.addEventListener('click', function (ev) {
  var b = ev.target.closest && ev.target.closest('.cptag');
  if (!b) { return; }
  ev.preventDefault();
  ev.stopPropagation();
  var id = b.getAttribute('data-cptag');
  if (!id || b.dataset.busy) { return; }
  b.dataset.busy = '1';
  fetch('/items/' + id + '/quicktag', {
    method: 'POST', headers: {'X-Requested-With': 'fetch'}
  }).then(function (r) {
    if (!r.ok) { throw new Error('http ' + r.status); }
    return r.json();
  }).then(function (d) {
    b.classList.toggle('on', !!d.on);
    b.title = d.on ? 'On the list with ' + d.who + ' — tap to take it off'
                   : 'Put this on the list with ' + d.who;
    var li = b.closest('li.item');
    if (li) {
      // keep the row's tag record straight, or the share menu would undo this;
      // the lit button itself is the only marker - no extra chip
      var now = (li.dataset.ltags || '').split(',').filter(Boolean);
      var at = now.indexOf(String(d.uid));
      if (d.on && at === -1) { now.push(String(d.uid)); }
      if (!d.on && at !== -1) { now.splice(at, 1); }
      li.dataset.ltags = now.join(',');
    }
    delete b.dataset.busy;
  }).catch(function () {
    b.title = 'Could not save that';
    delete b.dataset.busy;
  });
}, false);

/* ---------- pipeline: sort by any column ---------- */
(function () {
  var table = document.querySelector('table.dealtable');
  if (!table) { return; }
  document.querySelectorAll('table.dealtable').forEach(function (t) {
    t.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        var k = th.getAttribute('data-k');
        var num = th.classList.contains('num');
        var up = th.classList.contains('sorted') && !th.classList.contains('up');
        t.querySelectorAll('th').forEach(function (x) { x.classList.remove('sorted', 'up'); });
        th.classList.add('sorted');
        if (up) { th.classList.add('up'); }
        var body = t.querySelector('tbody');
        var rows = Array.prototype.slice.call(body.querySelectorAll('tr'));
        rows.sort(function (a, b) {
          var x = a.dataset[k] || '', y = b.dataset[k] || '';
          if (num) { x = parseFloat(x) || 0; y = parseFloat(y) || 0; return up ? x - y : y - x; }
          // blanks last whichever way it is pointing - an empty cell is not a small one
          if (!x !== !y) { return x ? -1 : 1; }
          return up ? String(y).localeCompare(String(x)) : String(x).localeCompare(String(y));
        });
        rows.forEach(function (r) { body.appendChild(r); });
      });
    });
  });
})();

/* ---------- tap a row to open it - and on the board it POPS OUT ----------
   In narrow Keep columns an in-place expansion squeezed the edit form into
   twelve characters. An opened row now lifts out of its column into a
   readable card in the middle of the screen; a tap outside puts it back. */
(function () {
  function collapseAll() {
    document.querySelectorAll('li.item.expanded').forEach(function (x) {
      x.classList.remove('expanded');
      x.style.left = x.style.top = '';
      x.style.transform = '';
    });
    var sc = document.querySelector('.expscrim');
    if (sc) { sc.remove(); }
  }
  // the card opens like a balloon, right where the row is - no flying
  function popAt(li) {
    var r = li.getBoundingClientRect();
    li.classList.add('expanded');
    var w = Math.min(540, window.innerWidth * 0.94);
    li.style.left = Math.min(Math.max(8, r.left - 12), window.innerWidth - w - 8) + 'px';
    var h = Math.min(li.offsetHeight || 200, window.innerHeight * 0.84);
    li.style.top = Math.min(Math.max(8, r.top - 12), window.innerHeight - h - 12) + 'px';
  }
  function syncScrim() {
    var open = document.querySelector('.grid.board li.item.expanded');
    var sc = document.querySelector('.expscrim');
    if (open && !sc) {
      sc = document.createElement('div');
      sc.className = 'expscrim';
      sc.addEventListener('click', collapseAll);
      document.body.appendChild(sc);
    } else if (!open && sc) {
      sc.remove();
    }
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { collapseAll(); }
  });
  document.addEventListener('click', function (ev) {
    // a tap on a real control is that control's business
    if (ev.target.closest('button, a, input, select, textarea, label, summary, form')) {
      var chip = ev.target.closest('.ncount');
      if (!chip) { return; }
      ev.preventDefault();
      var cli = chip.closest('li.item');
      if (cli.closest('.grid.board')) { collapseAll(); popAt(cli); }
      else { cli.classList.add('expanded'); }
      syncScrim();
      return;
    }
    var li = ev.target.closest('li.item');
    if (!li) { return; }
    if (li.closest('.grid.board')) {
      var was = li.classList.contains('expanded');
      collapseAll();
      if (!was) { popAt(li); }
      syncScrim();
    } else {
      li.classList.toggle('expanded');
    }
  }, false);
})();

/* ---------- a soft press moves the open card (v100) ----------
   Grab the popped-out card by its title, meta line, stamps or any bare spot
   and slide it wherever it is comfortable. Buttons, links and the text boxes
   keep doing their jobs, and a tap that never moved still folds the card. */
(function () {
  var drag = null;
  document.addEventListener('pointerdown', function (ev) {
    var li = ev.target.closest && ev.target.closest('.grid.board li.item.expanded');
    if (!li) { return; }
    if (ev.target.closest('button, a, input, select, textarea, label, summary,'
        + ' form, .checklist, .notes, .photostrip, .files')) { return; }
    var m = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(li.style.transform || '');
    var r = li.getBoundingClientRect();
    drag = {li: li, x: ev.clientX, y: ev.clientY,
            dx: m ? parseFloat(m[1]) : 0, dy: m ? parseFloat(m[2]) : 0,
            w: r.width, h: r.height,
            baseL: r.left - (m ? parseFloat(m[1]) : 0),
            baseT: r.top - (m ? parseFloat(m[2]) : 0),
            moved: false};
  }, true);
  document.addEventListener('pointermove', function (ev) {
    if (!drag) { return; }
    var nx = drag.dx + ev.clientX - drag.x;
    var ny = drag.dy + ev.clientY - drag.y;
    if (!drag.moved) {
      if (Math.abs(nx - drag.dx) + Math.abs(ny - drag.dy) < 7) { return; }
      drag.moved = true;
      drag.li.style.userSelect = 'none';
    }
    // keep a good handful of the card on screen
    nx = Math.max(56 - drag.w - drag.baseL,
         Math.min(nx, window.innerWidth - drag.baseL - 56));
    ny = Math.max(8 - drag.baseT,
         Math.min(ny, window.innerHeight - drag.baseT - 64));
    drag.li.style.transform = 'translate(' + nx + 'px,' + ny + 'px)';
    if (ev.cancelable) { ev.preventDefault(); }
  }, {passive: false});
  function endDrag() {
    if (drag && drag.moved) {
      drag.li.style.userSelect = '';
      // the lift-off at the end of a slide must not also fold the card
      var stop = function (e) { e.stopPropagation(); e.preventDefault(); };
      document.addEventListener('click', stop, true);
      setTimeout(function () { document.removeEventListener('click', stop, true); }, 120);
    }
    drag = null;
  }
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);
})();

/* ---------- tap the dot, get the next color ---------- */
document.addEventListener('click', function (ev) {
  var d = ev.target.closest && ev.target.closest('[data-color-sec]');
  if (!d) { return; }
  ev.preventDefault();
  var sec = d.closest('section.card');
  fetch('/sections/' + d.getAttribute('data-color-sec') + '/color', {
    method: 'POST', headers: {'X-Requested-With': 'fetch'}
  }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
    if (j && sec) {
      sec.style.setProperty('--sa', j.color);
      // rows carry their own copy of the color when set inline - repaint them too
      sec.querySelectorAll('li.item').forEach(function (li) {
        if (li.style.getPropertyValue('--sa')) { li.style.setProperty('--sa', j.color); }
      });
    }
  });
}, false);


/* ---------- team chips: one tap names who you are waiting on ---------- */
document.addEventListener('click', function (ev) {
  var chip = ev.target.closest && ev.target.closest('.tchip');
  if (!chip || chip.classList.contains('lchip')) { return; }
  ev.preventDefault();
  var form = chip.closest('form');
  var field = form && form.querySelector('[name=waiting_on]');
  if (field) {
    field.value = field.value === chip.getAttribute('data-team') ? '' : chip.getAttribute('data-team');
  }
}, false);

/* ---------- fold a box to its header ---------- */
document.addEventListener('click', function (ev) {
  var pf = ev.target.closest && ev.target.closest('[data-pfold]');
  if (pf) {
    ev.preventDefault();
    var proj = pf.closest('.project');
    fetch('/projects/' + pf.getAttribute('data-pfold') + '/fold', {method: 'POST'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j) { return; }
        proj.classList.toggle('pfolded', j.folded);
        pf.innerHTML = j.folded ? '▸' : '▾';
      });
    return;
  }
  var f = ev.target.closest && ev.target.closest('.foldbtn');
  if (!f) { return; }
  ev.preventDefault();
  var card = f.closest('section.card');
  fetch('/sections/' + f.getAttribute('data-fold') + '/fold', {method: 'POST'})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (!j) { return; }
      card.classList.toggle('folded', j.folded);
      f.innerHTML = j.folded ? '▸' : '▾';
    });
}, false);

/* ---------- drag boxes into your own order ----------
   Pointer events so one code path serves mouse and thumb. The handle is the
   only place a drag can start, so rows keep their swipes and holds. */
(function () {
  var dragging = null;
  function cards() {
    return Array.prototype.slice.call(document.querySelectorAll('.grid section.card'));
  }
  document.addEventListener('pointerdown', function (ev) {
    var h = ev.target.closest && ev.target.closest('.draghandle');
    if (!h) { return; }
    ev.preventDefault();
    dragging = h.closest('section.card');
    dragging.classList.add('dragging');
    try { h.setPointerCapture(ev.pointerId); } catch (e) {}
  });
  document.addEventListener('pointermove', function (ev) {
    if (!dragging) { return; }
    ev.preventDefault();
    var under = document.elementsFromPoint(ev.clientX, ev.clientY)
      .map(function (el) { return el.closest && el.closest('.grid section.card'); })
      .filter(function (c) { return c && c !== dragging; })[0];
    cards().forEach(function (c) { c.classList.remove('dropwait'); });
    if (!under) { return; }
    under.classList.add('dropwait');
    var r = under.getBoundingClientRect();
    var before = ev.clientY < r.top + r.height / 2;
    under.parentNode.insertBefore(dragging, before ? under : under.nextSibling);
  }, {passive: false});
  document.addEventListener('pointerup', function () {
    if (!dragging) { return; }
    dragging.classList.remove('dragging');
    cards().forEach(function (c) { c.classList.remove('dropwait'); });
    dragging = null;
    var ids = cards().map(function (c) { return c.getAttribute('data-sec'); })
      .filter(Boolean).join(',');
    var fd = new FormData();
    fd.append('ids', ids);
    fetch('/sections/order', {method: 'POST', body: fd});
  });
})();

/* ---------- the three-dot menu closes when you look away ---------- */
document.addEventListener('click', function (ev) {
  document.querySelectorAll('details.sharepop[open]').forEach(function (d) {
    if (!d.contains(ev.target)) { d.removeAttribute('open'); }
  });
});

/* ---------- drag a task to another box, or another project ----------
   Same pointer-event pattern as box dragging: the little handle is the only
   place a drag can start, so swipes and holds on the row keep working.
   Dropping tells the server which section, which project, and which task it
   now sits above - the server renumbers and both ends are permission-checked. */
(function () {
  var li = null, ghost = null, raf = 0, lastXY = null;
  function clear() {
    document.querySelectorAll('.dropline').forEach(function (x) { x.classList.remove('dropline'); });
    document.querySelectorAll('section.card.dropwait, .project.dropwait')
      .forEach(function (x) { x.classList.remove('dropwait'); });
  }
  // the row itself stays put as a faint slot; a lifted copy rides the finger,
  // and the rows it passes slide out of the way - the Keep feel
  function makeGhost(row, x, y) {
    var r = row.getBoundingClientRect();
    var wrap = document.createElement('div');
    wrap.className = 'grid board ghostwrap';
    var ul = document.createElement('ul');
    ul.className = 'items';
    var copy = row.cloneNode(true);
    copy.classList.remove('dragging', 'expanded');
    ul.appendChild(copy);
    wrap.appendChild(ul);
    wrap.style.width = r.width + 'px';
    wrap.style.left = r.left + 'px';
    wrap.style.top = r.top + 'px';
    wrap._dx = x - r.left;
    wrap._dy = y - r.top;
    document.body.appendChild(wrap);
    return wrap;
  }
  function flip(mutate) {
    var lists = [];
    if (li && li.parentNode) { lists.push(li.parentNode); }
    var before = new Map();
    lists.forEach(function (ul) {
      Array.prototype.forEach.call(ul.children, function (row) {
        var r = row.getBoundingClientRect();
        if (r.bottom > -50 && r.top < window.innerHeight + 50) {
          before.set(row, r.top);
        }
      });
    });
    mutate();
    if (li && li.parentNode && lists.indexOf(li.parentNode) < 0) {
      Array.prototype.forEach.call(li.parentNode.children, function (row) {
        if (!before.has(row)) {
          var r = row.getBoundingClientRect();
          before.set(row, r.top - 0.01);   // new list: nudge so it still animates
        }
      });
    }
    before.forEach(function (oldTop, row) {
      if (row === li) { return; }
      var now = row.getBoundingClientRect().top;
      var dy = oldTop - now;
      if (Math.abs(dy) < 2) { return; }
      row.style.transition = 'none';
      row.style.transform = 'translateY(' + dy + 'px)';
      void row.offsetWidth;
      row.style.transition = 'transform .15s ease';
      row.style.transform = '';
      setTimeout(function () { row.style.transition = ''; }, 180);
    });
  }
  document.addEventListener('pointerdown', function (ev) {
    var h = ev.target.closest && ev.target.closest('.idrag');
    if (!h) { return; }
    ev.preventDefault();
    li = h.closest('li.item');
    ghost = makeGhost(li, ev.clientX, ev.clientY);
    li.classList.add('dragging');
    try { h.setPointerCapture(ev.pointerId); } catch (e) {}
  });
  document.addEventListener('pointermove', function (ev) {
    if (!li) { return; }
    ev.preventDefault();
    lastXY = ev;
    if (raf) { return; }
    raf = requestAnimationFrame(function () { raf = 0; track(lastXY); });
  }, {passive: false});
  function track(ev) {
    if (!li) { return; }
    if (ghost) {
      ghost.style.left = (ev.clientX - ghost._dx) + 'px';
      ghost.style.top = (ev.clientY - ghost._dy) + 'px';
    }
    clear();
    // near an edge, the page walks along so far-away boxes can be reached
    if (ev.clientY < 90) { window.scrollBy(0, -14); }
    else if (ev.clientY > window.innerHeight - 90) { window.scrollBy(0, 14); }
    var els = document.elementsFromPoint(ev.clientX, ev.clientY);
    var row = els.map(function (el) { return el.closest && el.closest('.grid.board li.item'); })
      .filter(function (x) { return x && x !== li; })[0];
    if (row) {
      var r = row.getBoundingClientRect();
      var ref = ev.clientY < r.top + r.height / 2 ? row : row.nextSibling;
      if (ref !== li && ref !== li.nextSibling) {
        flip(function () { row.parentNode.insertBefore(li, ref); });
      }
      return;
    }
    // anywhere on a project - its header, its + task row, the blank space
    // under a short list - means "into this project", same bucket or not
    var proj = els.map(function (el) { return el.closest && el.closest('.grid.board .project'); })
      .filter(Boolean)[0];
    if (proj) {
      var pul = proj.querySelector('ul.items');
      if (pul && li.parentNode !== pul) {
        flip(function () { pul.appendChild(li); });
        proj.classList.add('dropwait');
      } else if (pul) { proj.classList.add('dropwait'); }
      return;
    }
    // an empty patch of list (the loose tasks area) takes the row directly
    var bare = els.map(function (el) { return el.closest && el.closest('.grid.board ul.items'); })
      .filter(Boolean)[0];
    if (bare) {
      if (li.parentNode !== bare) { flip(function () { bare.appendChild(li); }); }
      return;
    }
    var card = els.map(function (el) { return el.closest && el.closest('.grid.board section.card'); })
      .filter(Boolean)[0];
    if (card && !card.contains(li)) {
      // an empty stretch of another box - land at the end of its loose list
      var ul = card.querySelector('ul.items.loose') || card.querySelector('ul.items');
      if (ul) { flip(function () { ul.appendChild(li); }); card.classList.add('dropwait'); }
    }
  }
  document.addEventListener('pointerup', function () {
    if (!li) { return; }
    var moved = li;
    li = null;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    // the lifted card settles into its slot
    if (ghost) {
      var g = ghost; ghost = null;
      var end = moved.getBoundingClientRect();
      g.style.transition = 'left .16s ease, top .16s ease, opacity .16s ease';
      g.style.left = end.left + 'px';
      g.style.top = end.top + 'px';
      g.style.opacity = '0.4';
      setTimeout(function () { g.remove(); moved.classList.remove('dragging'); }, 170);
    } else {
      moved.classList.remove('dragging');
    }
    clear();
    var ul = moved.closest('ul.items');
    var card = moved.closest('section.card');
    if (!ul || !card) { return; }
    var next = moved.nextElementSibling;
    while (next && !next.classList.contains('item')) { next = next.nextElementSibling; }
    var fd = new FormData();
    fd.append('section_id', card.getAttribute('data-sec'));
    fd.append('project_id', ul.getAttribute('data-proj') || '');
    fd.append('before_id', next ? next.id.replace('item-', '') : '');
    fetch('/items/' + moved.querySelector('.idrag').getAttribute('data-idrag') + '/move',
      {method: 'POST', body: fd}).then(function (r) {
        if (!r.ok) { alert('That move did not save - it is back where it was on reload.'); return; }
        moved.setAttribute('data-sec', card.getAttribute('data-sec'));
        var sa = card.style.getPropertyValue('--sa');
        if (sa && moved.style.getPropertyValue('--sa')) { moved.style.setProperty('--sa', sa); }
      });
  });
})();

/* ---------- checklists: Keep's checkboxes, living on the row ---------- */
document.addEventListener('change', function (ev) {
  var t = ev.target.closest && ev.target.closest('.cktick');
  if (!t) { return; }
  var row = t.closest('.ckrow');
  fetch('/checks/' + t.getAttribute('data-ck') + '/toggle', {method: 'POST'})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (!j) { t.checked = !t.checked; return; }
      row.classList.toggle('ckdone', !!j.done);
      updateCkChip(t.closest('li.item'));
    });
});
document.addEventListener('click', function (ev) {
  var d = ev.target.closest && ev.target.closest('[data-ckdel]');
  if (!d) { return; }
  ev.preventDefault();
  var li = d.closest('li.item');
  fetch('/checks/' + d.getAttribute('data-ckdel') + '/delete', {method: 'POST'})
    .then(function (r) { if (r.ok) { d.closest('.ckrow').remove(); updateCkChip(li); } });
});
document.addEventListener('submit', function (ev) {
  var f = ev.target.closest && ev.target.closest('form.ckadd');
  if (!f) { return; }
  ev.preventDefault();
  var inp = f.querySelector('[name=body]');
  var body = (inp.value || '').trim();
  if (!body) { return; }
  var fd = new FormData();
  fd.append('body', body);
  fetch('/items/' + f.getAttribute('data-id') + '/checks', {method: 'POST', body: fd})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (!j) { return; }
      var lab = document.createElement('label');
      lab.className = 'ckrow';
      lab.innerHTML = '<input type="checkbox" class="cktick" data-ck="' + j.id + '"> ' +
        '<span dir="auto"></span> <button type="button" class="ckdel" data-ckdel="' + j.id + '" title="Remove step">✕</button>';
      lab.querySelector('span').textContent = j.body;
      f.parentNode.insertBefore(lab, f);
      inp.value = '';
      updateCkChip(f.closest('li.item'));
    });
});
function updateCkChip(li) {
  if (!li) { return; }
  var rows = li.querySelectorAll('.ckrow');
  var done = li.querySelectorAll('.ckrow.ckdone').length;
  var chip = li.querySelector('.ckchip');
  if (!rows.length) { if (chip) { chip.remove(); } return; }
  if (!chip) {
    chip = document.createElement('span');
    chip.className = 'ckchip';
    chip.title = 'Checklist';
    var meta = li.querySelector('.metaline');
    if (meta) { meta.appendChild(chip); }
  }
  chip.innerHTML = '☑ ' + done + '/' + rows.length;
}

/* ---------- "+ New..." inside the section and project dropdowns ---------- */
document.addEventListener('change', function (ev) {
  var sel = ev.target.closest && ev.target.closest(
    'form.edit select[name=section_id], form.edit select[name=project_id]');
  if (!sel) { return; }
  var field = sel.name === 'section_id' ? 'new_section' : 'new_project';
  var inp = sel.closest('form').querySelector('[name=' + field + ']');
  if (!inp) { return; }
  inp.hidden = sel.value !== '__new__';
  if (sel.value === '__new__') { inp.focus(); }
});

/* ---------- right-click on open space: create things where you clicked ----------
   The board's blank stretches now answer: a task or project born under the
   cursor, and Archive lives here instead of a too-easy arrow on the header. */
(function () {
  var menu = null;
  function close() { if (menu) { menu.remove(); menu = null; } }
  document.addEventListener('click', function (ev) {
    if (menu && !menu.contains(ev.target)) { close(); }
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { close(); } });
  function act(label, fn, danger) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'statuschoice' + (danger ? ' spot-danger' : '');
    b.textContent = label;
    b.addEventListener('click', function () { close(); fn(); });
    return b;
  }
  function openForm(det) {
    if (!det) { return; }
    det.setAttribute('open', '');
    det.scrollIntoView({block: 'center', behavior: 'smooth'});
    var inp = det.querySelector('input[name=title]');
    if (inp) { setTimeout(function () { inp.focus(); }, 250); }
  }
  document.addEventListener('contextmenu', function (ev) {
    if (ev.target.closest('li.item, a, input, textarea, select, button, summary, form, .sheet-wrap')) { return; }
    var card = ev.target.closest('.grid.board section.card');
    if (!card) { return; }
    ev.preventDefault();
    close();
    var proj = ev.target.closest('.project');
    var secName = (card.querySelector('.sec-head h2') || {textContent: ''}).textContent.trim().replace(/\s+/g, ' ');
    menu = document.createElement('div');
    menu.className = 'statusmenu spotmenu';
    if (proj) {
      var pname = ((proj.querySelector('.proj-head h3') || {textContent: ''}).textContent || '').trim();
      menu.appendChild(act('+ Task in ' + pname.slice(0, 26), function () {
        openForm(proj.querySelector('.addrow details'));
      }));
    }
    var secAdds = Array.prototype.filter.call(
      card.querySelectorAll('.addrow details'),
      function (d) { return !d.closest('.project'); });
    menu.appendChild(act('+ Task in ' + secName.slice(0, 26), function () { openForm(secAdds[0]); }));
    if (secAdds[1]) {
      menu.appendChild(act('+ New project here', function () { openForm(secAdds[1]); }));
    }
    var addsec = document.querySelector('.addsec details');
    if (addsec) {
      menu.appendChild(act('+ New section', function () { openForm(addsec); }));
    }
    if (proj) {
      var ul = proj.querySelector('ul.items');
      var pid = ul && ul.getAttribute('data-proj');
      if (pid) {
        // move the whole project to another bucket
        Array.prototype.forEach.call(
          document.querySelectorAll('.grid.board section.card'),
          function (other) {
            if (other === card) { return; }
            var name = (other.querySelector('.sec-head h2') || {textContent: ''})
              .textContent.trim().replace(/\s+/g, ' ');
            var sid = other.getAttribute('data-sec');
            if (!sid || !name) { return; }
            menu.appendChild(act('Move project → ' + name.slice(0, 24), function () {
              var fd = new FormData();
              fd.append('section_id', sid);
              fetch('/projects/' + pid + '/move', {method: 'POST', body: fd})
                .then(function (r) { if (r.ok) { location.reload(); } });
            }));
          });
        // rename, right where the name is
        menu.appendChild(act('Rename this project', function () {
          var h3 = proj.querySelector('.proj-head h3');
          if (!h3) { return; }
          var inp = document.createElement('input');
          inp.type = 'text';
          inp.value = h3.textContent.trim();
          inp.className = 'renamer';
          inp.dir = 'auto';
          h3.textContent = '';
          h3.appendChild(inp);
          inp.focus();
          inp.select();
          var done = false;
          function save() {
            if (done) { return; }
            done = true;
            var t = inp.value.trim();
            if (!t) { location.reload(); return; }
            var fd = new FormData();
            fd.append('title', t);
            fetch('/projects/' + pid + '/rename', {method: 'POST', body: fd})
              .then(function (r) { return r.ok ? r.json() : null; })
              .then(function (j) { h3.textContent = j ? j.title : t; });
          }
          inp.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); save(); }
            if (e.key === 'Escape') { done = true; location.reload(); }
          });
          inp.addEventListener('blur', save);
        }));
        // what kind of box is this - tasks, codes, phones, or notes
        var curKind = (proj.className.match(/pk-(\w+)/) || [0, 'tasks'])[1];
        [['tasks', 'a task list'], ['codes', 'a codes box (doors, combos, passwords)'],
         ['phones', 'a phone book'], ['notes', 'a notes box']].forEach(function (k) {
          if (k[0] === curKind) { return; }
          menu.appendChild(act('Make this ' + k[1], function () {
            var fd = new FormData();
            fd.append('kind', k[0]);
            fetch('/projects/' + pid + '/kind', {method: 'POST', body: fd})
              .then(function (r) { if (r.ok) { location.reload(); } });
          }));
        });
        menu.appendChild(act('Archive this project (put away)', function () {
          fetch('/projects/' + pid + '/arch', {method: 'POST'})
            .then(function (r) { if (r.ok) { location.reload(); } });
        }, true));
      }
    }
    menu.style.position = 'fixed';
    menu.style.visibility = 'hidden';
    document.body.appendChild(menu);
    var w = menu.offsetWidth || 200, h = menu.offsetHeight || 160;
    menu.style.left = Math.min(Math.max(8, ev.clientX), window.innerWidth - w - 8) + 'px';
    menu.style.top = Math.min(Math.max(8, ev.clientY), window.innerHeight - h - 8) + 'px';
    menu.style.visibility = '';
  });
})();

/* ---------- fold all / open all ---------- */
document.addEventListener('click', function (ev) {
  var b = ev.target.closest && ev.target.closest('[data-foldall]');
  if (!b) { return; }
  var fd = new FormData();
  fd.append('mode', b.getAttribute('data-foldall'));
  fetch('/board/foldall', {method: 'POST', body: fd})
    .then(function (r) { if (r.ok) { location.reload(); } });
});

/* ---------- boxes with a trade: codes, phones, notes ----------
   The rows are ordinary tasks underneath; the dressing changes. A code row
   copies itself on a tap of the little button; a phone row dials. */
(function () {
  function flash(el, msg) {
    var t = document.createElement('span');
    t.className = 'copied-note';
    t.textContent = msg;
    el.appendChild(t);
    setTimeout(function () { t.remove(); }, 1200);
  }
  function decorate() {
    document.querySelectorAll('.project.pk-codes li.item, .project.pk-phones li.item')
      .forEach(function (li) {
        if (li.querySelector('.copybtn')) { return; }
        var body = li.querySelector('.body');
        if (!body) { return; }
        var isPhone = !!li.closest('.pk-phones');
        var n = li.querySelector('.t .n');
        var value = (n ? n.textContent.replace(/^\s*—\s*/, '') : '').trim();
        if (!value) {
          var tt = li.querySelector('.t .tt');
          value = tt ? tt.textContent.trim() : '';
        }
        if (!value) { return; }
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'copybtn';
        btn.textContent = isPhone ? '☎ call' : '⧉ copy';
        btn.addEventListener('click', function (e) {
          e.stopPropagation();
          if (isPhone) {
            var num = value.replace(/[^\d+]/g, '');
            if (num.length >= 7) { location.href = 'tel:' + num; return; }
          }
          (navigator.clipboard ? navigator.clipboard.writeText(value)
            : Promise.reject()).then(function () { flash(btn, 'copied'); },
            function () { flash(btn, value); });
        });
        var t = li.querySelector('.t');
        if (t) { t.appendChild(btn); }
      });
  }
  decorate();
  window.decorateKinds = decorate;
})();

/* rotating the phone re-fits whatever is open */
window.addEventListener('resize', function () {
  var li = document.querySelector('.grid.board li.item.expanded');
  if (!li) { return; }
  var w = Math.min(540, window.innerWidth * 0.94);
  li.style.left = Math.min(Math.max(8, parseFloat(li.style.left) || 8),
    window.innerWidth - w - 8) + 'px';
  var h = Math.min(li.offsetHeight || 200, window.innerHeight * 0.84);
  li.style.top = Math.min(Math.max(8, parseFloat(li.style.top) || 8),
    window.innerHeight - h - 12) + 'px';
});
