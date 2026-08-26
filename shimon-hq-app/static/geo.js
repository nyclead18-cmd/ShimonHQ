/* Travel times measured from where you actually are.
   The browser only knows your position while a page is open, so every visit
   quietly refreshes the fix; the "Leave now" reminder, which runs on the server
   with no browser, uses the most recent one and falls back to your saved
   addresses once it goes stale. Only the latest fix is ever stored. */
(function () {
  'use strict';
  if (!navigator.geolocation) return;

  var KEY = 'hq_geo_on';
  function wants() { try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; } }
  function remember(v) { try { localStorage.setItem(KEY, v ? '1' : '0'); } catch (e) {} }

  function send(pos) {
    var b = 'lat=' + pos.coords.latitude + '&lng=' + pos.coords.longitude;
    fetch('/where', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'fetch'},
      body: b
    }).then(function () { paint(true); });
  }

  function grab(loud) {
    navigator.geolocation.getCurrentPosition(send, function (err) {
      if (loud) {
        alert(err.code === 1
          ? 'Location is blocked for this site. Turn it on in your browser settings for shimonhq.onrender.com and tap again.'
          : 'Could not get a location fix just now.');
      }
      if (err.code === 1) { remember(false); paint(false); }
    }, {enableHighAccuracy: false, timeout: 9000, maximumAge: 120000});
  }

  var btn;
  function paint(on) {
    if (!btn) return;
    btn.classList.toggle('on', !!on);
    btn.title = on ? 'Travel times start from where you are' : 'Use my location for travel times';
  }

  function mount() {
    var right = document.querySelector('header.top .right');
    if (!right) return;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'geobtn';
    btn.innerHTML = '&#128205;';
    var bell = right.querySelector('#notifbtn');
    if (bell) { right.insertBefore(btn, bell.nextSibling); } else { right.appendChild(btn); }
    paint(wants());
    btn.addEventListener('click', function () {
      if (btn.classList.contains('on')) {
        remember(false); paint(false);
        fetch('/where/forget', {method: 'POST', headers: {'X-Requested-With': 'fetch'}});
        return;
      }
      remember(true);
      grab(true);
    });
    if (wants()) { grab(false); }     // silent refresh on every visit
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else { mount(); }
})();
