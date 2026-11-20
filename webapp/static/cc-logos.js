/* Command Center logo stages (change 73).
   Every client logo is shown the same way, while keeping its own identity:
   - empty margins are trimmed, so every logo has the same visual weight;
   - the stage behind it is tinted with the logo's own brand colour (a monochrome logo gets a neutral stage);
   - a logo drawn in white (made for dark headers) gets a dark stage, unless someone chose Light on the Setup tab;
   - a logo that comes with its own solid box is shown as a rounded badge;
   - small files are never stretched past their real resolution.
   Runs in the browser on our own copy of the image (same origin), so nothing is sent anywhere. */
(function () {
  var MAX_OUT = 900;

  function lum(r, g, b) { return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255; }
  function hsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, h = 0, s = 0;
    if (mx !== mn) {
      var d = mx - mn;
      s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
      h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h /= 6;
    }
    return [h, s, l];
  }
  function dist(a, b) { return Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]); }

  function analyse(img) {
    var nw = img.naturalWidth || 300, nh = img.naturalHeight || 150;
    var k = Math.min(1, 480 / Math.max(nw, nh));
    var w = Math.max(1, Math.round(nw * k)), h = Math.max(1, Math.round(nh * k));
    var cv = document.createElement('canvas'); cv.width = w; cv.height = h;
    var cx = cv.getContext('2d', { willReadFrequently: true });
    cx.drawImage(img, 0, 0, w, h);
    var px = cx.getImageData(0, 0, w, h).data;
    function at(x, y) { var i = (y * w + x) * 4; return [px[i], px[i + 1], px[i + 2], px[i + 3]]; }

    // what's behind the logo: transparent, white, or its own solid box
    var pts = [[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1], [w >> 1, 0], [w >> 1, h - 1], [0, h >> 1], [w - 1, h >> 1]].map(function (p) { return at(p[0], p[1]); });
    var clear = pts.filter(function (p) { return p[3] < 24; }).length >= 5;
    var bg = [0, 0, 0];
    if (!clear) { pts.forEach(function (p) { bg[0] += p[0] / 8; bg[1] += p[1] / 8; bg[2] += p[2] / 8; }); }
    var whiteBg = !clear && lum(bg[0], bg[1], bg[2]) > 0.93;
    var badge = !clear && !whiteBg && pts.every(function (p) { return dist(p, bg) < 60; });

    var x0 = w, y0 = h, x1 = -1, y1 = -1, exposed = 0, n = 0, buckets = {};
    function alphaAt(xx, yy) { return (xx < 0 || yy < 0 || xx >= w || yy >= h) ? 0 : px[(yy * w + xx) * 4 + 3]; }
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        var i = (y * w + x) * 4, a = px[i + 3];
        var on = (clear || badge) ? a > 24 : a > 24 && dist([px[i], px[i + 1], px[i + 2]], bg) > 42;
        if (!on) continue;
        if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
        if ((x + y) % 2) continue;  // sample half the pixels for colour
        var r = px[i], g = px[i + 1], b = px[i + 2];
        if (badge && dist([r, g, b], bg) < 42) continue;  // the box itself isn't the brand colour
        n += a / 255;
        // white that touches the empty background (white lettering) vanishes on a light stage;
        // white enclosed by colour (a white house inside a red roof) reads fine on any stage
        if (clear && lum(r, g, b) > 0.78 && (alphaAt(x - 2, y) < 24 || alphaAt(x + 2, y) < 24 || alphaAt(x, y - 2) < 24 || alphaAt(x, y + 2) < 24)) exposed += a / 255;
        var c = hsl(r, g, b);
        if (c[1] > 0.28 && c[2] > 0.16 && c[2] < 0.84) {
          var key = Math.round(c[0] * 18) % 18, bk = buckets[key] || (buckets[key] = { w: 0, r: 0, g: 0, b: 0 });
          var wt = c[1] * (a / 255); bk.w += wt; bk.r += r * wt; bk.g += g * wt; bk.b += b * wt;
        }
      }
    }
    if (x1 < 0) return null;
    var top = null;
    Object.keys(buckets).forEach(function (k2) { if (!top || buckets[k2].w > top.w) top = buckets[k2]; });
    var brand = top && top.w > 3 ? [Math.round(top.r / top.w), Math.round(top.g / top.w), Math.round(top.b / top.w)] : null;
    if (badge && !brand) brand = bg.map(Math.round);
    return { k: k, box: badge ? [0, 0, w - 1, h - 1] : [x0, y0, x1, y1], light: clear && n > 0 && exposed / n > 0.08, badge: badge, brand: brand };
  }

  function stage(img) {
    if (img.dataset.staged) return;
    img.dataset.staged = '1';
    var holder = img.closest('.tile-logo, .hero-logo');
    if (!holder) return;
    var info;
    try { info = analyse(img); } catch (e) { info = null; }  // an image we can't read is simply shown as it is
    if (!info) { holder.classList.add('lg-ready'); return; }
    var nw = img.naturalWidth || 300, nh = img.naturalHeight || 150;
    var isSvg = img.dataset.type === 'svg';  // vector: already sharp, and without a fixed size it can't be cropped reliably
    var sx = info.box[0] / info.k, sy = info.box[1] / info.k, sw = (info.box[2] - info.box[0] + 1) / info.k, sh = (info.box[3] - info.box[1] + 1) / info.k;
    sw = Math.min(sw, nw - sx); sh = Math.min(sh, nh - sy);
    var o = Math.min(1, MAX_OUT / Math.max(sw, sh)), out = document.createElement('canvas');
    out.width = Math.max(1, Math.round(sw * o)); out.height = Math.max(1, Math.round(sh * o));
    if (!isSvg) {
      try {
        out.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, out.width, out.height);
        img.src = out.toDataURL('image/png');
      } catch (e) { /* keep the original */ }
      // never stretch a small file past its real size (it would blur): at most 1.25 screen pixels per image pixel
      img.style.maxWidth = 'min(var(--lg-w, 76%), ' + Math.round(sw * 1.25 / Math.max(1, (window.devicePixelRatio || 1) / 1.6)) + 'px)';
    }
    var ratio = sw / sh;
    holder.style.setProperty('--lg-w', ratio > 3 ? '80%' : ratio > 1.6 ? '72%' : '58%');
    if (info.brand) {
      holder.style.setProperty('--brand', 'rgb(' + info.brand.join(',') + ')');
      if (holder.classList.contains('hero-logo') && !holder.classList.contains('setup-logo')) {
        document.documentElement.style.setProperty('--client-brand', 'rgb(' + info.brand.join(',') + ')');  // the blog covers use it
      }
      if (lum(info.brand[0], info.brand[1], info.brand[2]) < 0.3) holder.style.setProperty('--dark-mix', '14%');  // keep a dark brand mark visible
    }
    if (isSvg && !info.badge) {  // vector files often carry wide margins: zoom to the artwork instead of cropping
      var bw = (info.box[2] - info.box[0] + 1) / (nw * info.k), bh = (info.box[3] - info.box[1] + 1) / (nh * info.k);
      var z = Math.min(2.4, 0.9 / Math.max(bw, bh, 0.05));
      if (z > 1.08) {
        var cxp = ((info.box[0] + info.box[2]) / 2) / (nw * info.k), cyp = ((info.box[1] + info.box[3]) / 2) / (nh * info.k);
        img.style.transform = 'scale(' + z.toFixed(2) + ') translate(' + ((0.5 - cxp) * 100).toFixed(1) + '%,' + ((0.5 - cyp) * 100).toFixed(1) + '%)';
      }
    }
    var forced = holder.dataset.bg;  // Light / Dark chosen by a person on the Setup tab wins
    if (forced === 'dark' || (forced !== 'light' && info.light)) holder.classList.add('lg-dark');
    if (info.badge) holder.classList.add('lg-badge');
    holder.classList.add('lg-ready');
  }

  // clients with no logo: a monogram in a colour of their own, the same every time
  var PALETTE = ['#0A686A', '#2E6FB5', '#7A4FC4', '#B5562E', '#2F8A5B', '#A6487E', '#6B7A1E', '#C2862B'];
  function monogram(el) {
    var name = el.getAttribute('data-name') || el.textContent, h = 0;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    var holder = el.closest('.tile-logo, .hero-logo');
    if (holder) { holder.style.setProperty('--brand', PALETTE[h % PALETTE.length]); holder.classList.add('lg-ready', 'lg-mono'); }
  }

  // messages after an action: a close button; "done" messages fade away on their own, problems stay until closed
  function flashes() {
    document.querySelectorAll('.flash').forEach(function (f) {
      if (f.querySelector('.fl-x')) return;
      var x = document.createElement('button'); x.type = 'button'; x.className = 'fl-x'; x.setAttribute('aria-label', 'Dismiss'); x.textContent = '×';
      function go() { f.classList.add('leaving'); setTimeout(function () { f.remove(); }, 320); }
      x.addEventListener('click', go); f.appendChild(x);
      f.setAttribute('role', f.classList.contains('bad') ? 'alert' : 'status');
      if (f.classList.contains('good')) setTimeout(go, 7000);
    });
  }

  // view-only (change 78): fields read-only, change buttons gone; "Sync now" stays. The server refuses changes anyway.
  function viewOnly() {
    if (!document.querySelector('.cc[data-edit="0"]')) return;
    document.querySelectorAll('.cc form').forEach(function (f) {
      if (f.classList.contains('cc-allow') || (f.getAttribute('method') || '').toLowerCase() !== 'post') return;
      f.classList.add('ro');
      f.querySelectorAll('input, select, textarea').forEach(function (el) { if (el.type !== 'hidden') el.disabled = true; });
      f.querySelectorAll('button, input[type=submit]').forEach(function (b) { b.remove(); });
    });
    document.querySelectorAll('.cc-owner-only, details.dl-actions').forEach(function (e) { e.remove(); });
  }

  function run() {
    viewOnly();
    flashes();
    // blog cards: an image that won't load leaves the drawn cover showing
    document.querySelectorAll('.pc-visual img').forEach(function (im) {
      if (im.complete && !im.naturalWidth) im.remove(); else im.addEventListener('error', function () { im.remove(); }, { once: true });
    });
    document.querySelectorAll('.tile-logo img, .hero-logo img').forEach(function (img) {
      if (img.complete && img.naturalWidth) stage(img); else img.addEventListener('load', function () { stage(img); }, { once: true });
    });
    document.querySelectorAll('.tile-logo .tile-initials, .hero-logo .tile-initials').forEach(monogram);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
})();
