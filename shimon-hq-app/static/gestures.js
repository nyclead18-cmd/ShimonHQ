/* Outlook-style gestures for Shimon HQ.
   Swipe right  -> the friendly action (mark read / mark done)
   Swipe left   -> the destructive one (dismiss / delete)
   Press & hold -> a sheet slides up with everything that row can do
   Works on the briefing, the board, the day view and the Joel board. */
(function () {
  'use strict';

  var THRESHOLD = 72;      // how far you have to pull before it fires
  var SLOP = 12;           // finger wobble we forgive before deciding the direction
  var HOLD_MS = 480;

  // ---------- what each kind of row can do ----------

  function rowKind(li) {
    if (li.classList.contains('brief')) return 'brief';
    if (li.classList.contains('noterow')) return 'note';
    if (li.id && li.id.indexOf('item-') === 0) return 'item';
    return '';
  }

  var ROWS = 'li.item, li.brief, li.noterow';

  function noteText(li) {
    var t = q(li, '.t');
    return t ? t.textContent.trim() : '';
  }

  function copyText(txt) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(flashCopied, function () { legacyCopy(txt); });
    } else {
      legacyCopy(txt);
    }
  }

  function legacyCopy(txt) {
    var ta = document.createElement('textarea');
    ta.value = txt;
    ta.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); flashCopied(); } catch (e) {}
    ta.remove();
  }

  function flashCopied() {
    var t = document.getElementById('copytoast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'copytoast';
      t.className = 'copytoast';
      t.textContent = 'Copied';
      document.body.appendChild(t);
    }
    t.classList.add('on');
    clearTimeout(flashCopied._t);
    flashCopied._t = setTimeout(function () { t.classList.remove('on'); }, 1200);
  }

  function removeNoteInPlace(li, form) {
    fetch(form.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}});
    var list = li.parentElement;
    li.remove();
    if (list && !list.querySelector('li')) { list.remove(); }
  }

  function q(li, sel) { return li.querySelector(sel); }

  // the two swipe actions, or null where a row has none
  function swipeRight(li) {
    var k = rowKind(li);
    if (k === 'brief') {
      var dot = q(li, '.dotbtn');
      if (!dot) return null;
      var read = li.classList.contains('readrow');
      return {label: read ? 'Unread' : 'Read', icon: read ? '●' : '✓',
              color: '#1F3A5F', run: function () { dot.click(); }};
    }
    if (k === 'item') {
      var pill = q(li, '.pill');
      if (!pill || li.classList.contains('done')) return null;
      return {label: 'Done', icon: '✓', color: '#3A6B3E',
              run: function () { setStatus(li, 'done'); }};
    }
    if (k === 'note') {
      return {label: 'Copy', icon: '⧉', color: '#1F3A5F',
              run: function () { copyText(noteText(li)); }};
    }
    return null;
  }

  function swipeLeft(li) {
    var k = rowKind(li);
    if (k === 'brief') {
      var f = q(li, '.delform');
      if (!f) return null;
      return {label: 'Dismiss', icon: '✕', color: '#8A8377',
              color2: true, run: function () { dismissInPlace(li, f); }};
    }
    if (k === 'item') {
      if (!itemId(li)) return null;
      return {label: 'Delete', icon: '✕', color: '#A33B2E',
              confirm: true, run: function () { deleteItem(li); }};
    }
    if (k === 'note') {
      var nf = q(li, '.ndelform');
      if (!nf) return null;
      return {label: 'Delete', icon: '✕', color: '#A33B2E',
              run: function () { removeNoteInPlace(li, nf); }};
    }
    return null;
  }

  function starOf(li) { return q(li, '.star'); }

  function itemId(li) {
    var pill = q(li, '.pill');
    return (pill && pill.getAttribute('data-id')) || '';
  }

  function deleteItem(li) {
    var id = itemId(li);
    if (!id) return;
    var f = document.createElement('form');
    f.method = 'post';
    f.action = '/items/' + id + '/delete';
    document.body.appendChild(f);
    f.submit();
  }

  function setStatus(li, st) {
    var pill = q(li, '.pill');
    var id = pill && pill.getAttribute('data-id');
    if (!id) return;
    fetch('/items/' + id + '/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded',
                'X-Requested-With': 'fetch'},
      body: 'status=' + encodeURIComponent(st)
    }).then(function (r) { return r.json(); }).then(function (d) {
      pill.className = 'pill ' + d.status;
      pill.textContent = d.status === 'open' ? 'Open'
        : d.status === 'waiting' ? 'Waiting' : 'Done';
      li.classList.remove('open', 'waiting', 'done', 'donerow');
      li.classList.add(d.status);
      if (d.status === 'done') { li.classList.add('donerow'); }
      if (window.applyFilter) { window.applyFilter(); }
    });
  }

  function dismissInPlace(li, form) {
    fetch(form.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}});
    var sec = li.closest('section');
    li.style.transition = 'height .18s ease, opacity .18s ease, margin .18s ease';
    li.style.height = li.offsetHeight + 'px';
    requestAnimationFrame(function () {
      li.style.height = '0'; li.style.opacity = '0'; li.style.paddingTop = '0';
      li.style.paddingBottom = '0'; li.style.overflow = 'hidden';
    });
    setTimeout(function () {
      li.remove();
      if (sec) {
        var c = sec.querySelector('.counts');
        if (c) { c.textContent = sec.querySelectorAll('li.brief:not(.readrow)').length + ' unread'; }
        if (!sec.querySelector('li.item')) { sec.remove(); }
      }
    }, 190);
  }

  // ---------- the hold-to-open sheet ----------
  // built from the row's own controls, so it can never drift out of step with the page

  function sheetActions(li) {
    var out = [];
    var k = rowKind(li);

    if (k === 'brief') {
      var dot = q(li, '.dotbtn');
      if (dot) {
        out.push({label: li.classList.contains('readrow') ? 'Mark unread' : 'Mark read',
                  run: function () { dot.click(); }});
      }
      var push = q(li, '.pushbtn');
      if (push) { out.push({label: 'Make a task', run: function () { push.click(); scrollTo(push); }}); }
      var onboard = q(li, '.outlookbtn.done');
      if (onboard) { out.push({label: 'Open it on the board', href: onboard.href}); }
    }

    if (k === 'item') {
      var st = li.classList.contains('done') ? 'done'
        : li.classList.contains('waiting') ? 'waiting' : 'open';
      if (st !== 'done') { out.push({label: 'Mark done', run: function () { setStatus(li, 'done'); }}); }
      if (st !== 'waiting') { out.push({label: 'Mark waiting', run: function () { setStatus(li, 'waiting'); }}); }
      if (st !== 'open') { out.push({label: 'Reopen', run: function () { setStatus(li, 'open'); }}); }
      var resp = q(li, '.respond input[name=body]');
      if (resp) { out.push({label: 'Add a response', run: function () { scrollTo(resp); resp.focus(); }}); }
      var fadd = q(li, '.fileinput');
      if (fadd) { out.push({label: 'Attach a file', run: function () { fadd.click(); }}); }
      var edit = q(li, 'details.editbox');
      if (edit) {
        out.push({label: 'Edit / due date / reminder', run: function () {
          edit.open = true; scrollTo(edit);
        }});
      }
    }

    if (k === 'note') {
      var when = q(li, '.nd');
      out.push({label: 'Copy this response', run: function () { copyText(noteText(li)); }});
      if (when) {
        out.push({label: 'Copy with the date', run: function () {
          copyText(when.textContent.trim() + ' - ' + noteText(li));
        }});
      }
      var nf2 = q(li, '.ndelform');
      if (nf2) {
        out.push({label: 'Delete this response', danger: true,
                  run: function () { removeNoteInPlace(li, nf2); }});
      }
      return out;
    }
    var st = starOf(li);
    if (st) {
      out.push({label: st.classList.contains('on') ? 'Take off today' : 'Put on today',
                run: function () { st.click(); }});
    }

    if (itemId(li)) {
      out.push({label: 'Move to top', run: function () {
        fetch('/items/' + itemId(li) + '/top', {method: 'POST'})
          .then(function (r) { if (r.ok) { location.reload(); } });
      }});
      var pinned = !!q(li, '.pinmark');
      out.push({label: pinned ? 'Unpin' : 'Pin to top', run: function () {
        fetch('/items/' + itemId(li) + '/pin', {method: 'POST'})
          .then(function (r) { if (r.ok) { location.reload(); } });
      }});
      var ck = q(li, '.checklist');
      if (ck) {
        out.push({label: 'Add a checklist step', run: function () {
          ck.hidden = false;
          li.classList.add('expanded');
          var inp = ck.querySelector('.ckadd input');
          if (inp) { scrollTo(inp); inp.focus(); }
        }});
      }
      out.push({label: 'Archive (put away, not deleted)', run: function () {
        fetch('/items/' + itemId(li) + '/arch', {method: 'POST'})
          .then(function (r) {
            if (!r.ok) { return; }
            li.style.transition = 'opacity .18s ease';
            li.style.opacity = '0';
            setTimeout(function () { li.remove(); if (window.applyFilter) { window.applyFilter(); } }, 200);
          });
      }});
    }


    // anything on the row that points at a map or an email
    Array.prototype.forEach.call(li.querySelectorAll('a.maplink, a.dirlink, a.outlookbtn:not(.done)'),
      function (a) {
        var t = (a.getAttribute('data-sheet') || a.textContent || '').trim();
        if (t) { out.push({label: t, href: a.href, external: a.target === '_blank'}); }
      });

    if (k === 'brief') {
      var bd = q(li, '.delform');
      if (bd) {
        out.push({label: 'Dismiss', danger: true,
                  run: function () { dismissInPlace(li, bd); }});
      }
    } else if (itemId(li)) {
      out.push({label: 'Delete', danger: true, run: function () { deleteItem(li); }});
    }
    return out;
  }

  function scrollTo(el) {
    setTimeout(function () { el.scrollIntoView({block: 'center', behavior: 'smooth'}); }, 60);
  }

  var sheet, sheetList, sheetTitle, sheetOpenedAt = 0;

  function buildSheet() {
    sheet = document.createElement('div');
    sheet.className = 'sheet-wrap';
    sheet.innerHTML =
      '<div class="sheet-scrim"></div>' +
      '<div class="sheet" role="dialog" aria-modal="true">' +
      '<div class="sheet-grab"></div>' +
      '<div class="sheet-title"></div>' +
      '<div class="sheet-sub"></div>' +
      '<div class="sheet-list"></div>' +
      '<button type="button" class="sheet-cancel">Cancel</button>' +
      '</div>';
    document.body.appendChild(sheet);
    sheetList = sheet.querySelector('.sheet-list');
    sheetTitle = sheet.querySelector('.sheet-title');
    sheet.querySelector('.sheet-scrim').addEventListener('click', guardedClose);
    sheet.querySelector('.sheet-cancel').addEventListener('click', guardedClose);
  }

  function openSheet(li, atX, atY) {
    var acts = sheetActions(li);
    if (!acts.length) return;
    if (!sheet) { buildSheet(); }
    var t = q(li, '.t');
    sheetTitle.textContent = t ? t.textContent.trim().slice(0, 90) : '';
    // say where this task lives, so the menu never loses its row
    var crumbs = [];
    var proj = li.closest && li.closest('.project');
    var ph = proj && proj.querySelector('.proj-head h3');
    if (ph) { crumbs.push(ph.textContent.trim().slice(0, 40)); }
    var card = li.closest && li.closest('section.card');
    var sh = card && card.querySelector('.sec-head h2');
    if (sh) { crumbs.push(sh.textContent.trim().slice(0, 40)); }
    var sub = sheet.querySelector('.sheet-sub');
    if (sub) {
      sub.textContent = crumbs.join(' · ');
      sub.hidden = !crumbs.length;
    }
    sheetList.innerHTML = '';
    acts.forEach(function (a) {
      var b = document.createElement(a.href ? 'a' : 'button');
      b.className = 'sheet-act' + (a.danger ? ' danger' : '');
      b.textContent = a.label;
      if (a.href) {
        b.href = a.href;
        if (a.external) { b.target = '_blank'; b.rel = 'noopener'; }
        b.addEventListener('click', function (ev) {
          if (new Date().getTime() - sheetOpenedAt < 500) { ev.preventDefault(); return; }
          closeSheet();
        });
      } else {
        b.type = 'button';
        b.addEventListener('click', function () {
          if (new Date().getTime() - sheetOpenedAt < 500) { return; }
          closeSheet(); a.run();
        });
      }
      sheetList.appendChild(b);
    });
    sheetOpenedAt = new Date().getTime();
    sheet.classList.add('on');
    document.body.classList.add('sheet-open');
    // at a desk the menu lands beside the click, not across the room
    var panel = sheet.querySelector('.sheet');
    panel.classList.remove('at-point');
    panel.style.left = panel.style.top = panel.style.bottom = '';
    if (atX != null && window.matchMedia('(min-width:700px)').matches) {
      panel.classList.add('at-point');
      var w = panel.offsetWidth || 260, h = panel.offsetHeight || 320;
      var x = Math.min(Math.max(8, atX + 4), window.innerWidth - w - 8);
      var y = Math.min(Math.max(8, atY + 4), window.innerHeight - h - 8);
      panel.style.left = x + 'px';
      panel.style.top = y + 'px';
      panel.style.bottom = 'auto';
    }
  }

  // lifting your finger after a long press fires a click right where the sheet
  // just appeared - ignore anything that lands in the first moments
  function guardedClose() {
    if (new Date().getTime() - sheetOpenedAt < 500) { return; }
    closeSheet();
  }

  function closeSheet() {
    if (!sheet) return;
    sheet.classList.remove('on');
    document.body.classList.remove('sheet-open');
  }

  // belt and braces: the page must never be left unscrollable by a stuck sheet
  setInterval(function () {
    if (document.body.classList.contains('sheet-open') &&
        !(sheet && sheet.classList.contains('on'))) {
      document.body.classList.remove('sheet-open');
    }
  }, 1500);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeSheet(); }
  });

  // ---------- the swipe itself ----------

  function movers(li) {
    return Array.prototype.filter.call(li.children, function (c) {
      return !c.classList.contains('swipe-bg');
    });
  }

  // .swipe-bg = the coloured action panel that stays put (absolute, so it never
  // becomes a grid track).  .swipe-fg = an opaque curtain in the row's own colour
  // that travels with the content, so the panel only shows through the gap.
  // rows alternate between a colour and no colour at all, and the list behind
  // them is see-through too - keep climbing until something is actually painted
  function opaqueBg(el) {
    for (var n = el; n && n !== document.documentElement; n = n.parentElement) {
      var c = getComputedStyle(n).backgroundColor;
      if (c && c !== 'transparent' && !/rgba\(\s*0,\s*0,\s*0,\s*0\s*\)/.test(c)) {
        return c;
      }
    }
    return getComputedStyle(document.body).backgroundColor || '#FFFEFB';
  }

  function ensureBg(li) {
    var bg = li.querySelector(':scope > .swipe-bg');
    if (!bg) {
      bg = document.createElement('div');
      bg.className = 'swipe-bg';
      bg.innerHTML = '<span class="sb-left"></span><span class="sb-right"></span>';
      var fg = document.createElement('div');
      fg.className = 'swipe-fg';
      fg.style.background = opaqueBg(li);
      li.insertBefore(fg, li.firstChild);
      li.insertBefore(bg, li.firstChild);
      li.classList.add('swipeable');
    }
    li.classList.add('swiping');
    return bg;
  }

  // put the row back exactly as it was: no leftover colour band, no stray nodes
  function clearSwipe(li) {
    li.classList.remove('swiping');
    setTimeout(function () {
      if (li.classList.contains('swiping')) return;   // a new swipe started
      li.classList.remove('swipeable');
      var n = li.querySelector(':scope > .swipe-bg');
      if (n) { n.remove(); }
      n = li.querySelector(':scope > .swipe-fg');
      if (n) { n.remove(); }
    }, 260);
  }

  function shift(li, dx) {
    movers(li).forEach(function (c) {
      c.style.transform = dx ? 'translateX(' + dx + 'px)' : '';
    });
  }

  function release(li, animate) {
    movers(li).forEach(function (c) {
      c.style.transition = animate ? 'transform .18s ease' : '';
      c.style.transform = '';
      if (animate) {
        setTimeout(function () { c.style.transition = ''; }, 200);
      }
    });
  }

  var cur = null;
  var swallowClick = false;
  var swallowTimer = null;

  // Lifting your finger after a long press fires a click at that spot - and by
  // then the sheet has slid up underneath it, so the click would land on
  // whichever row of the menu happens to be there. Eat that one click whatever
  // it hits; the real tap comes afterwards.
  document.addEventListener('click', function (ev) {
    if (!swallowClick) return;
    swallowClick = false;
    clearTimeout(swallowTimer);
    ev.stopPropagation();
    ev.preventDefault();
  }, true);

  function onStart(ev) {
    if (ev.touches && ev.touches.length > 1) return;
    var li = ev.target.closest(ROWS);
    if (!li || !rowKind(li)) return;
    // Never hijack a real control. Text boxes are strictly off limits: a tap that
    // lingers even slightly is how you get a cursor into one, and stealing that
    // stops you writing a response at all.
    if (ev.target.closest('button, a, select, textarea, label, summary, input')) return;
    if (sheet && sheet.classList.contains('on')) return;
    var t = ev.touches ? ev.touches[0] : ev;
    cur = {li: li, x0: t.clientX, y0: t.clientY, dir: 0, dx: 0,
           right: swipeRight(li), left: swipeLeft(li), held: false};
    cur.hold = setTimeout(function () {
      if (!cur || cur.dir) return;
      cur.held = true;
      swallowClick = true;
      clearTimeout(swallowTimer);
      swallowTimer = setTimeout(function () { swallowClick = false; }, 700);
      if (navigator.vibrate) { try { navigator.vibrate(12); } catch (e) {} }
      openSheet(li);
      cancel();
    }, HOLD_MS);
  }

  function onMove(ev) {
    if (!cur) return;
    var t = ev.touches ? ev.touches[0] : ev;
    var dx = t.clientX - cur.x0, dy = t.clientY - cur.y0;

    if (!cur.dir) {
      if (Math.abs(dy) > SLOP && Math.abs(dy) > Math.abs(dx)) { cancel(); return; }  // scrolling
      if (Math.abs(dx) < SLOP) return;
      cur.dir = dx > 0 ? 1 : -1;
      clearTimeout(cur.hold);
      var act = cur.dir > 0 ? cur.right : cur.left;
      if (!act) { cancel(); return; }
      var bg = ensureBg(cur.li);
      bg.style.setProperty('--sw', act.color);
      bg.querySelector(cur.dir > 0 ? '.sb-left' : '.sb-right').textContent =
        act.icon + '  ' + act.label;
      bg.classList.toggle('from-left', cur.dir > 0);
      bg.classList.toggle('from-right', cur.dir < 0);
    }

    if (ev.cancelable) { ev.preventDefault(); }
    // a little resistance past the trigger point so it feels like Outlook
    var raw = dx - (cur.dir > 0 ? SLOP : -SLOP);
    cur.dx = Math.abs(raw) > THRESHOLD
      ? cur.dir * (THRESHOLD + (Math.abs(raw) - THRESHOLD) * 0.35)
      : raw;
    shift(cur.li, cur.dx);
    var bg2 = cur.li.querySelector(':scope > .swipe-bg');
    if (bg2) { bg2.classList.toggle('armed', Math.abs(cur.dx) >= THRESHOLD); }
  }

  function onEnd() {
    if (!cur) return;
    clearTimeout(cur.hold);
    var c = cur; cur = null;
    if (!c.dir) return;
    var act = c.dir > 0 ? c.right : c.left;
    var fired = Math.abs(c.dx) >= THRESHOLD && act;
    release(c.li, true);
    var bg = c.li.querySelector(':scope > .swipe-bg');
    if (bg) { bg.classList.remove('armed'); }
    clearSwipe(c.li);
    if (!fired) return;
    if (act.confirm) {
      if (!window.confirm('Delete this?')) { return; }
    }
    if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
    act.run();
  }

  function cancel() {
    if (!cur) return;
    clearTimeout(cur.hold);
    release(cur.li, false);
    clearSwipe(cur.li);
    cur = null;
  }

  document.addEventListener('touchstart', onStart, {passive: true});
  document.addEventListener('touchmove', onMove, {passive: false});
  document.addEventListener('touchend', onEnd);
  document.addEventListener('touchcancel', cancel);

  // mouse equivalent so the same thing works at a desk: right-click = the sheet
  document.addEventListener('contextmenu', function (ev) {
    var li = ev.target.closest(ROWS);
    if (!li || !rowKind(li)) return;
    if (ev.target.closest('a, input, textarea, select')) return;
    ev.preventDefault();
    openSheet(li, ev.clientX, ev.clientY);
  });
})();
