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
  document.querySelectorAll('li.item').forEach(function (li) {
    var okText = !needle || li.textContent.toLowerCase().indexOf(needle) !== -1;
    var st = li.classList.contains('open') ? 'open' : li.classList.contains('waiting') ? 'waiting' : 'done';
    var okMode = MODE === 'all' ? (st !== 'done' || li.closest('section').classList.contains('show-done') || needle) : st === MODE;
    li.style.display = (okText && okMode)
      ? (li.classList.contains('donerow') ? 'grid' : '')
      : 'none';
  });
  document.querySelectorAll('.project').forEach(function (pr) {
    var any = Array.prototype.some.call(pr.querySelectorAll('li.item'), function (li) { return li.style.display !== 'none'; });
    pr.style.display = (any || !filtering) ? '' : 'none';
  });
  document.querySelectorAll('section.card').forEach(function (sec) {
    var any = Array.prototype.some.call(sec.querySelectorAll('li.item'), function (li) { return li.style.display !== 'none'; });
    sec.style.display = (any || !filtering) ? '' : 'none';
  });
}
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
        li.className = 'item ' + d.status + (d.status === 'done' ? ' donerow' : '');
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

// responses save in place — no page reload
document.addEventListener('submit', function (ev) {
  var form = ev.target.closest('form.respond');
  if (!form) return;
  ev.preventDefault();
  var input = form.querySelector('input[name=body]');
  var body = (input.value || '').trim();
  if (!body) return;
  fetch(form.action, {
    method: 'POST',
    headers: {'X-Requested-With': 'fetch'},
    body: new FormData(form)
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.id) return;
    var itemBody = form.closest('.body');
    var list = itemBody.querySelector('ul.notes');
    if (!list) {
      list = document.createElement('ul');
      list.className = 'notes';
      itemBody.insertBefore(list, itemBody.querySelector('.files'));
    }
    var dt = d.created_at ? (parseInt(d.created_at.slice(5, 7), 10) + '/' + parseInt(d.created_at.slice(8, 10), 10)) : '';
    var li = document.createElement('li');
    var nd = document.createElement('span'); nd.className = 'nd'; nd.textContent = dt;
    var tx = document.createElement('span'); tx.setAttribute('dir', 'auto'); tx.textContent = d.body;
    var df = document.createElement('form');
    df.className = 'ndelform'; df.method = 'post'; df.action = '/notes/' + d.id + '/delete';
    df.innerHTML = '<button type="submit" class="del ndel" title="Remove response">✕</button>';
    li.appendChild(nd); li.appendChild(tx); li.appendChild(df);
    list.appendChild(li);
    input.value = '';
    input.focus();
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
