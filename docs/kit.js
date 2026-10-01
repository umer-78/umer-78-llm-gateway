// Small helpers for the demo page: formatting, controls, and SVG charts drawn at the container's
// real width (redrawn on resize), so text stays readable on a phone.
export const $ = (sel, root = document) => root.querySelector(sel);
export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
export const pct = (x, d = 1) => (x == null ? '–' : `${(100 * x).toFixed(d)}%`);
export const pts = (x, d = 1) => (x == null ? '–' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(100 * x).toFixed(d)}`);
export const usd = (x, d = 3) => (x == null ? '–' : `$${x.toFixed(d)}`);
export const int = (x) => (x == null ? '–' : Math.round(x).toLocaleString('en-US'));
export const num = (x, d = 2) => (x == null ? '–' : x.toFixed(d));

export async function load(url = 'data.json') {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.json();
}

export function fail(err) {
  const main = $('main') || document.body;
  main.insertAdjacentHTML('afterbegin', `<p class="card err">Could not load the data: ${esc(err.message)}</p>`);
  console.error(err);
}

export function kpis(el, items) {
  el.innerHTML = items.map((k) => `<div class="kpi"><div class="label">${esc(k.label)}</div><div class="big">${esc(k.value)}</div>${k.note ? `<div class="note">${esc(k.note)}</div>` : ''}</div>`).join('');
}

// A row of toggle buttons; calls onChange(value) now and on every change.
export function seg(el, options, value, onChange) {
  el.classList.add('seg');
  el.setAttribute('role', 'group');
  el.innerHTML = options.map((o) => {
    const [v, label] = Array.isArray(o) ? o : [o, o];
    return `<button type="button" data-v="${esc(v)}" aria-pressed="${String(v) === String(value)}">${esc(label)}</button>`;
  }).join('');
  el.onclick = (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    el.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    onChange(b.dataset.v);
  };
  onChange(String(value));
}

export function select(el, options, value, onChange) {
  el.innerHTML = options.map((o) => {
    const [v, label] = Array.isArray(o) ? o : [o, o];
    return `<option value="${esc(v)}"${String(v) === String(value) ? ' selected' : ''}>${esc(label)}</option>`;
  }).join('');
  el.onchange = () => onChange(el.value);
  onChange(el.value);
}

// Horizontal bars: rows [{label, value, text?, color?, dim?}], scaled to opts.max (default: the largest).
export function bars(el, rows, opts = {}) {
  const max = opts.max ?? Math.max(...rows.map((r) => Math.abs(r.value)), 1e-9);
  const fmt = opts.fmt || ((v) => String(v));
  el.classList.add('bars');
  el.style.setProperty('--valw', `${Math.min(9, Math.max(4.5, ...rows.map((r) => String(r.text ?? fmt(r.value)).length * 0.62 + 0.4)))}em`);
  const grow = !el._grown;
  el._grown = true;
  el.innerHTML = rows.map((r) => `<div class="bar${r.dim ? ' dim' : ''}" title="${esc(r.title || r.label)}"><span class="name">${esc(r.label)}</span>` +
    `<span class="track"><span class="fill" style="display:block;width:${Math.max(0, Math.min(100, (100 * Math.abs(r.value)) / max)).toFixed(2)}%;--c:${r.color || 'var(--accent)'}"></span></span>` +
    `<span class="val">${esc(r.text ?? fmt(r.value))}</span></div>`).join('');
  if (grow) growIn(el);
}

// bars start empty and grow to their width once the page has painted
function growIn(el) {
  const fills = [...el.querySelectorAll('.fill')];
  const widths = fills.map((f) => f.style.width);
  fills.forEach((f) => { f.style.transition = 'none'; f.style.width = '0%'; });
  requestAnimationFrame(() => requestAnimationFrame(() => fills.forEach((f, i) => { f.style.transition = `width 1.1s cubic-bezier(.16,1,.3,1) ${i * 60}ms`; f.style.width = widths[i]; })));
}

export function table(el, cols, rows, opts = {}) {
  // cols: [{key, label, num?, fmt?, html?}]; rows: objects; opts.hl(row) -> highlight; opts.cls(row) -> a row class
  const head = cols.map((c) => `<th${c.num ? ' class="num"' : ''}>${esc(c.label)}</th>`).join('');
  const body = rows.map((r) => `<tr class="${opts.hl && opts.hl(r) ? 'hl' : ''} ${(opts.cls && opts.cls(r)) || ''}">` + cols.map((c) => {
    const v = c.fmt ? c.fmt(r[c.key], r) : r[c.key];
    return `<td${c.num ? ' class="num"' : ''}>${c.html ? v : esc(v ?? '')}</td>`;
  }).join('') + '</tr>').join('');
  // a table wider than the screen scrolls sideways, so keyboard users can focus it to scroll
  const label = opts.label || el.closest('section')?.querySelector('h2, h3')?.textContent.trim() || 'Table';
  el.innerHTML = `<div class="tbl"><button type="button" class="dl" data-dl="csv" aria-label="Download this table as CSV" title="Download CSV">CSV</button><div class="scroll" tabindex="0" role="region" aria-label="${esc(label)}"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></div>`;
}

function niceTicks(min, max, n = 5) {
  if (max <= min) return [min];
  const raw = (max - min) / n, p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(min / step - 1e-9) * step; v <= max + step * 1e-6; v += step) out.push(+v.toPrecision(12));
  return out;
}

function logTicks(min, max) {
  const out = [];
  for (let e = Math.floor(Math.log10(min)); e <= Math.ceil(Math.log10(max)); e++) {
    for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= min * 0.999 && v <= max * 1.001) out.push(+v.toPrecision(6)); }
  }
  return out;
}

const observed = new WeakMap();
function onWidth(el, draw) {
  // the observer calls whatever draw is current, so redrawing with a new spec replaces the old one
  const run = () => { const w = Math.round(el.clientWidth); if (w > 0 && w !== el._w) { el._w = w; el._draw(w); } };
  el._draw = draw;
  el._w = 0;
  if (!observed.has(el)) {
    const ro = new ResizeObserver(run);
    ro.observe(el);
    observed.set(el, ro);
  }
  run();
}

// Lines and points. spec: {height, x:{label,log,min,max,fmt,ticks}, y:{...}, series:[{name,color,points:[{x,y,label,title}],line,dots,dash,width}], vline:{x,label}, hline:{y,label}, onPick}
export function xy(el, spec) {
  el.classList.add('chart');
  el.dataset.spec = '1';
  onWidth(el, (W) => {
    const H = spec.height || 280;
    const all = spec.series.flatMap((s) => s.points);
    const xs = all.map((p) => p.x), ys = all.map((p) => p.y);
    const X = spec.x, Y = spec.y;
    let x0 = X.min ?? Math.min(...xs), x1 = X.max ?? Math.max(...xs);
    let y0 = Y.min ?? Math.min(...ys), y1 = Y.max ?? Math.max(...ys);
    if (!X.log && x0 === x1) { x0 -= 1; x1 += 1; }
    if (y0 === y1) { y0 -= 1; y1 += 1; }
    if (!Y.log && Y.pad !== 0 && Y.min == null) y0 -= (y1 - y0) * 0.06;
    if (!Y.log && Y.pad !== 0 && Y.max == null) y1 += (y1 - y0) * 0.06;
    const xt = X.ticks || (X.log ? logTicks(x0, x1) : niceTicks(x0, x1, W < 480 ? 4 : 6));
    const yt = Y.ticks || (Y.log ? logTicks(y0, y1).filter((t, i, a) => a.length < 8 || /^[1]/.test(String(t))) : niceTicks(y0, y1, 5));
    const fx = X.fmt || String, fy = Y.fmt || String;
    const ml = Math.max(0, ...yt.map((t) => fy(t).length)) * 6.4 + 14, mr = spec.marginRight ?? 16, mt = 12, mb = X.label ? 42 : 26;
    const sx = (v) => ml + (X.log ? (Math.log(v) - Math.log(x0)) / (Math.log(x1) - Math.log(x0)) : (v - x0) / (x1 - x0)) * (W - ml - mr);
    const sy = (v) => mt + (1 - (Y.log ? (Math.log(Math.max(v, y0 * 1e-3)) - Math.log(y0)) / (Math.log(y1) - Math.log(y0)) : (v - y0) / (y1 - y0))) * (H - mt - mb);
    const clip = `c${Math.random().toString(36).slice(2, 8)}`;
    const first = !el._animated;          // only the first drawing animates; redraws on input stay instant
    el._animated = true;
    let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(spec.label || '')}"><defs><clipPath id="${clip}"><rect x="${ml - 8}" y="${mt - 8}" width="${W - ml - mr + 16}" height="${H - mt - mb + 16}"/></clipPath></defs>`;
    s += '<g class="grid">' + yt.map((t) => `<line x1="${ml}" x2="${W - mr}" y1="${sy(t)}" y2="${sy(t)}"/>`).join('') + '</g>';
    s += yt.map((t) => `<text x="${ml - 8}" y="${sy(t) + 4}" text-anchor="end">${esc(fy(t))}</text>`).join('');
    s += xt.map((t) => `<text x="${sx(t)}" y="${H - mb + 16}" text-anchor="middle">${esc(fx(t))}</text>`).join('');
    s += `<line class="axis" x1="${ml}" x2="${W - mr}" y1="${H - mb}" y2="${H - mb}"/>`;
    if (X.label) s += `<text x="${(ml + W - mr) / 2}" y="${H - 6}" text-anchor="middle">${esc(X.label)}</text>`;
    if (Y.label) s += `<text x="${ml}" y="${mt - 2}" text-anchor="start" dy="-2">${esc(Y.label)}</text>`;
    if (spec.hline) s += `<line x1="${ml}" x2="${W - mr}" y1="${sy(spec.hline.y)}" y2="${sy(spec.hline.y)}" style="stroke:var(--muted);stroke-dasharray:4 4"/>` +
      (spec.hline.label ? `<text x="${W - mr}" y="${sy(spec.hline.y) - 5}" text-anchor="end">${esc(spec.hline.label)}</text>` : '');
    if (spec.vline) s += `<line x1="${sx(spec.vline.x)}" x2="${sx(spec.vline.x)}" y1="${mt}" y2="${H - mb}" style="stroke:var(--accent);stroke-width:1.5"/>` +
      (spec.vline.label ? `<text x="${sx(spec.vline.x) + (sx(spec.vline.x) > W * 0.7 ? -6 : 6)}" y="${mt + 10}" text-anchor="${sx(spec.vline.x) > W * 0.7 ? 'end' : 'start'}" class="lab">${esc(spec.vline.label)}</text>` : '');
    const labels = [], taken = [];
    spec.series.forEach((se, si) => {
      const c = se.color || `var(--c${(si % 6) + 1})`;
      if (se.line !== false && se.points.length > 1) {
        const d = se.points.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join('');
        s += `<path clip-path="url(#${clip})"${first && !se.dash ? ' pathLength="1" class="fx-draw"' : ''} d="${d}" fill="none" style="stroke:${c};stroke-width:${se.width || 2.2}${se.dash ? ';stroke-dasharray:5 4' : ''}"><title>${esc(se.name)}</title></path>`;
      }
      if (se.dots) se.points.forEach((p, pi) => {
        s += `<circle class="pt${first ? ' fx-pop' : ''}" tabindex="0" data-s="${si}" data-p="${pi}" cx="${sx(p.x)}" cy="${sy(p.y)}" r="${p.r || se.r || 5}" style="--d:${Math.min(pi, 30) * 25 + 300}ms;fill:${p.color || c};stroke:var(--surface);stroke-width:1.5"><title>${esc(p.title || p.label || se.name)}</title></circle>`;
        if (p.label) labels.push({ p, cx: sx(p.x), cy: sy(p.y) });
        taken.push({ x0: sx(p.x) - 6, x1: sx(p.x) + 6, y0: sy(p.y) - 6, y1: sy(p.y) + 6 });
      });
    });
    // labels go where they overlap no other label or dot; one that fits nowhere is left to its tooltip
    labels.sort((a, b) => (b.p.keep ? 1 : 0) - (a.p.keep ? 1 : 0));
    for (const { p, cx, cy } of labels) {
      const tw = p.label.length * 6.7;
      for (const [dx, dy, end] of [[9, 4, 0], [-9, 4, 1], [8, -9, 0], [-8, -9, 1], [8, 17, 0], [-8, 17, 1]]) {
        const x0 = end ? cx + dx - tw : cx + dx, box = { x0, x1: x0 + tw, y0: cy + dy - 10, y1: cy + dy + 3 };
        if (box.x0 < 0 || box.x1 > W || box.y0 < 0 || taken.some((q) => box.x0 < q.x1 && box.x1 > q.x0 && box.y0 < q.y1 && box.y1 > q.y0)) continue;
        taken.push(box);
        s += `<text class="lab" x="${cx + dx}" y="${cy + dy}" text-anchor="${end ? 'end' : 'start'}">${esc(p.label)}</text>`;
        break;
      }
    }
    el.innerHTML = s + '</svg><button type="button" class="dl" data-dl="svg" aria-label="Download this chart as SVG" title="Download SVG">SVG</button>';
    if (spec.onPick) {
      const pick = (e) => { const c = e.target.closest('circle.pt'); if (c) spec.onPick(spec.series[c.dataset.s].points[c.dataset.p], spec.series[c.dataset.s]); };
      el.onclick = pick;
      el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(e); } };
    }
  });
}

export function legend(el, series) {
  el.classList.add('legend');
  el.innerHTML = series.map((s, i) => `<span style="--c:${s.color || `var(--c${(i % 6) + 1})`}">${esc(s.name)}</span>`).join('');
}

// Signed bars around a centre line: rows [{label, value, text?, color?}], scaled to opts.max (default: the largest |value|).
export function diverge(el, rows, opts = {}) {
  const max = opts.max ?? Math.max(...rows.map((r) => Math.abs(r.value)), 1e-9);
  const fmt = opts.fmt || ((v) => String(v));
  el.classList.add('bars');
  el.style.setProperty('--valw', `${Math.min(9, Math.max(4.5, ...rows.map((r) => String(r.text ?? fmt(r.value)).length * 0.62 + 0.4)))}em`);
  const grow = !el._grown;
  el._grown = true;
  el.innerHTML = rows.map((r) => {
    const w = Math.min(50, (50 * Math.abs(r.value)) / max);
    const c = r.color || (r.value < 0 ? 'var(--bad)' : 'var(--good)');
    return `<div class="bar" title="${esc(r.title || r.label)}"><span class="name">${esc(r.label)}</span>` +
      `<span class="track dv"><span class="fill" style="position:absolute;top:0;bottom:0;left:${r.value < 0 ? 50 - w : 50}%;width:${w.toFixed(2)}%;--c:${c}"></span></span>` +
      `<span class="val">${esc(r.text ?? fmt(r.value))}</span></div>`;
  }).join('');
  if (grow) growIn(el);
}

// Downloads: every chart as an SVG file with its colours resolved, every table as CSV.
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'data';
function save(name, type, text) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const titleOf = (el) => el.closest('.card')?.querySelector('h2')?.textContent || document.title.split(' ·')[0];

export function svgFile(svg) {
  const copy = svg.cloneNode(true), live = [svg, ...svg.querySelectorAll('*')], dead = [copy, ...copy.querySelectorAll('*')];
  live.forEach((node, i) => {
    const cs = getComputedStyle(node), keep = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'opacity', 'font-family', 'font-size', 'font-weight'];
    dead[i].setAttribute('style', keep.map((k) => `${k}:${cs.getPropertyValue(k)}`).join(';'));
    dead[i].removeAttribute('class');
    dead[i].removeAttribute('pathLength');
  });
  copy.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  bg.setAttribute('width', '100%');
  bg.setAttribute('height', '100%');
  bg.setAttribute('fill', getComputedStyle(document.body).backgroundColor);
  copy.prepend(bg);
  return new XMLSerializer().serializeToString(copy);
}

function tableCsv(table) {
  const cell = (c) => { const t = c.textContent.replace(/\s+/g, ' ').trim(); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  return [...table.rows].map((r) => [...r.cells].map(cell).join(',')).join('\n') + '\n';
}

// one delegated listener serves every download button on the page
document.addEventListener('click', (e) => {
  const b = e.target.closest?.('button[data-dl]');
  if (!b) return;
  const host = b.parentElement;
  if (b.dataset.dl === 'svg') { const svg = host.querySelector('svg[role="img"]'); if (svg) save(`${slug(titleOf(host))}.svg`, 'image/svg+xml', svgFile(svg)); }
  else { const t = host.querySelector('table'); if (t) save(`${slug(titleOf(host))}.csv`, 'text/csv', tableCsv(t)); }
});
