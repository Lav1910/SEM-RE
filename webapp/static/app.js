/* Shared UI: Analyze box (clear, scope, suggestions), How it works popover, feedback dialog. */
(function () {
  var SCOPE_HINT = {root: 'Root Domain', url: 'Exact URL', subdomain: 'Subdomain', subfolder: 'Subfolder'};
  function clean(v) { return v.trim().replace(/^https?:\/\//i, '').replace(/^www\./i, ''); }
  function guess(v) {
    var c = clean(v), host = c.split('/')[0], path = c.slice(host.length).replace(/\/+$/, '');
    var dots = host.split('.').length;
    if (path) return /\.[a-z0-9]{2,5}$/i.test(path) || path.split('/').length > 3 ? 'url' : 'subfolder';
    return dots > 2 && !/^(co|com|org|net)\.[a-z]{2}$/i.test(host.split('.').slice(-2).join('.')) ? 'subdomain' : 'root';
  }

  document.querySelectorAll('.sq').forEach(function (box) {
    var input = box.querySelector('input'), clear = box.querySelector('.sq-clear'), sel = box.querySelector('select'),
        list = box.querySelector('.sq-list'), url = box.dataset.suggest, timer, active = -1, touched = false, items = [], seq = 0;
    sel.addEventListener('change', function () { touched = true; });
    function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; }
    function choose(it) {
      input.value = it.value; if (it.scope) { sel.value = it.scope; touched = true; }
      clear.hidden = !input.value; close(); input.focus();
    }
    function render() {
      list.innerHTML = '';
      if (!items.length) { close(); return; }
      items.forEach(function (it, i) {
        var li = document.createElement('li'); li.setAttribute('role', 'option'); li.id = input.id + '-opt-' + i;
        if (it.head) { li.className = 'sq-head'; li.textContent = it.head; li.setAttribute('aria-disabled', 'true'); list.appendChild(li); return; }
        var main = document.createElement('span'); main.className = 'sq-main'; main.textContent = it.value;
        var sub = document.createElement('span'); sub.className = 'sq-sub'; sub.textContent = it.sub || '';
        li.appendChild(main); li.appendChild(sub);
        li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(it); });
        list.appendChild(li);
      });
      list.hidden = false; input.setAttribute('aria-expanded', 'true');
    }
    function move(d) {
      var opts = items.map(function (it, i) { return it.head ? -1 : i; }).filter(function (i) { return i >= 0; });
      if (!opts.length) return;
      var pos = opts.indexOf(active); pos = pos < 0 ? (d > 0 ? 0 : opts.length - 1) : (pos + d + opts.length) % opts.length;
      active = opts[pos];
      list.querySelectorAll('li').forEach(function (li, i) { li.classList.toggle('on', i === active); });
      input.setAttribute('aria-activedescendant', input.id + '-opt-' + active);
    }
    input.addEventListener('input', function () {
      clear.hidden = !input.value;
      var v = clean(input.value);
      if (!touched && v.indexOf('.') > 0) sel.value = guess(input.value);
      clearTimeout(timer);
      if (v.length < 2) { close(); return; }
      timer = setTimeout(function () {
        var local = [];
        if (v.indexOf('.') > 0) {
          var host = v.split('/')[0], g = guess(input.value);
          local.push({head: 'Analyze as'});
          local.push({value: v.replace(/\/+$/, ''), scope: g, sub: SCOPE_HINT[g]});
          if (g !== 'root') { var parts = host.split('.'); local.push({value: parts.slice(-2).join('.'), scope: 'root', sub: 'Root Domain'}); }
        }
        var my = ++seq;
        fetch(url + '?q=' + encodeURIComponent(v.split('/')[0]), {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; })
          .then(function (rows) {
            if (my !== seq || document.activeElement !== input) return;
            items = local.slice();
            if (rows.length) {
              items.push({head: 'Audited before'});
              rows.forEach(function (r) { items.push({value: r.domain, sub: (r.name ? r.name + ' · ' : '') + r.n + ' audit' + (r.n === 1 ? '' : 's')}); });
            }
            render();
          });
      }, 160);
    });
    input.addEventListener('keydown', function (e) {
      if (list.hidden) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter' && active >= 0) { e.preventDefault(); choose(items[active]); }
      else if (e.key === 'Escape') { close(); }
    });
    input.addEventListener('blur', function () { setTimeout(close, 120); });
    clear.addEventListener('click', function () { input.value = ''; clear.hidden = true; touched = false; sel.value = 'root'; close(); input.focus(); });
  });

  /* How it works */
  document.querySelectorAll('.hiw').forEach(function (w) {
    var b = w.querySelector('.hiw-btn'), pop = w.querySelector('.hiw-pop');
    b.addEventListener('click', function (e) { e.stopPropagation(); pop.hidden = !pop.hidden; b.setAttribute('aria-expanded', pop.hidden ? 'false' : 'true'); });
    document.addEventListener('click', function (e) { if (!w.contains(e.target)) { pop.hidden = true; b.setAttribute('aria-expanded', 'false'); } });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { pop.hidden = true; b.setAttribute('aria-expanded', 'false'); } });
  });

  /* Feedback */
  var dlg = document.getElementById('feedback');
  document.querySelectorAll('[data-feedback]').forEach(function (b) {
    b.addEventListener('click', function () { if (dlg && dlg.showModal) { dlg.showModal(); var t = dlg.querySelector('textarea'); if (t) t.focus(); } });
  });
  if (dlg) dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
})();

/* Dashboard: Locations dropdown filled from the builder's website */
(function () {
  var box = document.querySelector('[data-locs]'); if (!box) return;
  var form = box.closest('form'), urlIn = form.querySelector('input[name=url]'), scopeSel = form.querySelector('select[name=scope]');
  var btn = box.querySelector('.loc-btn'), btnText = box.querySelector('.loc-btn-text'), panel = box.querySelector('.loc-panel'),
      list = box.querySelector('.loc-list'), allBox = box.querySelector('input[name=loc_all]'), note = box.querySelector('.loc-note'),
      test = form.querySelector('#test_mode'), cityH = form.querySelector('#city'), stateH = form.querySelector('#state');
  var meta = {cap: 5, cost_per_city: 0.27}, lastUrl = '', timer, seq = 0;

  function boxes() { return Array.prototype.slice.call(list.querySelectorAll('input[name=loc]')); }
  function cap() { return test && test.checked ? 2 : meta.cap; }
  function perCity() { return (test && test.checked ? meta.cost_per_city / 3 : meta.cost_per_city); }
  function money(v) { return '$' + v.toFixed(2); }
  function costTxt(n) { return meta.cost_per_city == null ? '' : ' · about ' + money(n * perCity()) + ' extra'; }
  function summarize() {
    var bs = boxes(), extra = bs.length, max = cap() - 1;
    if (!list.children.length) { btnText.textContent = 'Add a city'; note.textContent = ''; return; }
    if (allBox.checked) {
      var n = Math.min(extra, max);
      bs.forEach(function (b) { b.checked = true; b.disabled = false; });
      btnText.textContent = 'All locations (' + (extra + 1) + ')';
      note.textContent = n ? 'Main market + top ' + n + ' by search demand' + costTxt(n) : 'Main market only';
      if (extra > max) note.textContent += ' · ' + (extra - max) + ' more listed under “More areas”';
      return;
    }
    var picked = bs.filter(function (b) { return b.checked; }).length;
    bs.forEach(function (b) { b.disabled = !b.checked && picked >= max; });
    btnText.textContent = picked ? (picked + 1) + ' locations' : 'Main market only';
    note.textContent = (picked + 1) + ' of ' + (extra + 1) + ' locations' + (picked ? costTxt(picked) : '') +
      (picked >= max ? ' · limit of ' + cap() + ' per audit' : '');
  }
  function toManual() {  // leave "All locations" but keep the current ticks, trimmed to the per-audit limit
    allBox.checked = false;
    var on = boxes().filter(function (b) { return b.checked; });
    on.slice(Math.max(0, cap() - 1)).forEach(function (b) { b.checked = false; });
  }
  function row(label, sources, main, checked) {
    var l = document.createElement('label'); l.className = 'loc-row' + (main ? ' main' : '');
    var cb = document.createElement('input'); cb.type = 'checkbox';
    if (main) { cb.checked = true; cb.disabled = true; } else { cb.name = 'loc'; cb.value = label; cb.checked = !!checked; }
    cb.addEventListener('change', function () { if (allBox.checked) toManual(); summarize(); });
    var t = document.createElement('span'); t.className = 'loc-name'; t.textContent = label;
    var s = document.createElement('span'); s.className = 'loc-src';
    s.textContent = main ? 'Main' : (sources && sources.length ? sources[0] : '');
    l.appendChild(cb); l.appendChild(t); l.appendChild(s); list.appendChild(l);
  }
  function load() {
    var u = urlIn.value.trim();
    if (u.indexOf('.') < 1 || u === lastUrl) return;
    lastUrl = u; var my = ++seq;
    list.innerHTML = ''; allBox.checked = true; cityH.value = ''; stateH.value = '';
    btn.disabled = true; btn.classList.add('loading'); btnText.textContent = 'Finding service areas…'; note.textContent = 'Reading the website (about 15 seconds)';
    fetch(box.dataset.locs + '?url=' + encodeURIComponent(u) + '&scope=' + encodeURIComponent(scopeSel ? scopeSel.value : 'root'), {credentials: 'same-origin'})
      .then(function (r) { return r.json(); }).catch(function () { return {error: 'Couldn’t scan the site. Add the city below.', locations: []}; })
      .then(function (d) {
        if (my !== seq) return;
        btn.disabled = false; btn.classList.remove('loading');
        meta.cap = d.cap || meta.cap; meta.cost_per_city = d.cost_per_city === undefined ? meta.cost_per_city : d.cost_per_city;
        if (d.market) { cityH.value = d.market.city; stateH.value = d.market.state; box.querySelector('.loc-add-state').value = d.market.state_code; }
        (d.locations || []).forEach(function (l) { row(l.label, l.sources, l.main, true); });
        summarize();
        if (d.error) note.textContent = d.error;
        else if (d.source === 'last audit') note.textContent += ' · from the last audit';
      });
  }
  urlIn.addEventListener('blur', function () { setTimeout(load, 150); });
  urlIn.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(load, 1200); });
  if (scopeSel) scopeSel.addEventListener('change', function () { lastUrl = ''; load(); });
  if (test) test.addEventListener('change', summarize);
  allBox.addEventListener('change', function () { if (!allBox.checked) boxes().forEach(function (b) { b.checked = false; }); summarize(); });
  btn.addEventListener('click', function (e) { e.stopPropagation(); panel.hidden = !panel.hidden; btn.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true'); });
  document.addEventListener('click', function (e) { if (!box.querySelector('.loc-dd').contains(e.target)) { panel.hidden = true; btn.setAttribute('aria-expanded', 'false'); } });
  box.querySelector('.loc-add-btn').addEventListener('click', function () {
    var c = box.querySelector('.loc-add-city'), st = box.querySelector('.loc-add-state').value, name = c.value.trim().replace(/,.*$/, '');
    if (!name) { c.focus(); return; }
    name = name.replace(/\b\w/g, function (m) { return m.toUpperCase(); });
    if (!list.children.length) {  // nothing found: the first city typed becomes the main market
      cityH.value = name; stateH.value = st; row(name + ', ' + st, ['entered'], true, true);
    } else {
      if (allBox.checked) toManual();
      var room = boxes().filter(function (b) { return b.checked; }).length < cap() - 1;
      row(name + ', ' + st, ['added'], false, room);
    }
    c.value = ''; summarize();
  });
  box.querySelector('.loc-add-city').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); box.querySelector('.loc-add-btn').click(); } });
  form.addEventListener('submit', function () { boxes().forEach(function (b) { b.disabled = false; }); });
})();
