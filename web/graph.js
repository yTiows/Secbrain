/* Canvas knowledge-graph renderer: force layout with spatial-hash repulsion, cluster gravity,
   level-of-detail (labels/edges by zoom), pan/zoom/pinch, hit-testing, focus highlighting. No dependencies. */
(function () {
  const TYPE_COLORS = {
    fact: '#6fb3e0', concept: '#a58cf0', idea: '#f0c05e', project: '#ef8a5e', decision: '#e5675e', experience: '#e58fc0',
    procedure: '#6fd49a', instruction: '#4fc9b0', preference: '#d7a8f0', research: '#8aa9ff', reference: '#9aa7b4',
    document: '#7d8b99', observation: '#c3d36a', hypothesis: '#f0a05e', question: '#5ec8e5', entity: '#e8e2a0'
  };
  const EDGE_COLORS = { contradicts: '#ea6f66', replaces: '#e5b45e', updates: '#e5b45e', depends_on: '#5ec8e5', part_of: '#4a5968', references: '#3d4b59' };

  class GraphView {
    constructor(canvas, opts) {
      this.c = canvas; this.ctx = canvas.getContext('2d'); this.opts = opts || {};
      this.nodes = []; this.edges = []; this.byId = new Map(); this.clusters = [];
      this.k = 1; this.tx = 0; this.ty = 0; this.alpha = 1; this.sel = null; this.hover = null;
      this.filter = { types: null, q: '' }; this.running = false; this.dpr = window.devicePixelRatio || 1;
      this.pointers = new Map(); this.drag = null; this.fitted = false; this.userMoved = false; this.refit = false;
      this._bind(); this.resize(); new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    }
    static color(t) { return TYPE_COLORS[t] || '#9aa7b4'; }
    static get colors() { return TYPE_COLORS; }
    setData(d) {
      const old = this.byId; const n = d.nodes.length; const R = Math.sqrt(n) * 38 + 60;
      this.nodes = d.nodes.map((x, i) => {
        const p = old.get(x.id); const a = (i / n) * Math.PI * 2 + (x.cluster || 0);
        return Object.assign({}, x, { x: p ? p.x : Math.cos(a) * R * (0.4 + Math.random() * .6), y: p ? p.y : Math.sin(a) * R * (0.4 + Math.random() * .6), vx: 0, vy: 0, r: 3 + Math.sqrt(x.degree || 0) * 1.3 + (x.importance || 0.5) * 3 });
      });
      this.byId = new Map(this.nodes.map(x => [x.id, x]));
      this.edges = d.edges.filter(e => this.byId.has(e.src) && this.byId.has(e.dst)).map(e => Object.assign({}, e, { a: this.byId.get(e.src), b: this.byId.get(e.dst) }));
      this.clusters = d.clusters || [];
      this.adj = new Map(); for (const e of this.edges) { (this.adj.get(e.src) || this.adj.set(e.src, []).get(e.src)).push(e); (this.adj.get(e.dst) || this.adj.set(e.dst, []).get(e.dst)).push(e); }
      this.alpha = 1; this.fitted = old.size > 0 && this.fitted; this.refit = old.size > 0; this.kick();
    }
    setFilter(f) { Object.assign(this.filter, f); this.draw(); }
    select(id, center) { this.sel = id; if (center && this.byId.has(id)) this.centerOn(this.byId.get(id)); this.draw(); }
    resize() {
      const r = this.c.parentElement.getBoundingClientRect(); const ow = this.w, oh = this.h; this.w = r.width; this.h = r.height;
      if (ow && oh && this.fitted) { this.tx += (r.width - ow) / 2; this.ty += (r.height - oh) / 2; }   // keep the view centred when the layout changes
      this.c.width = Math.max(1, r.width * this.dpr); this.c.height = Math.max(1, r.height * this.dpr);
      if (!this.fitted && this.nodes.length) this.fit(); this.draw();
    }
    kick() { if (!this.running) { this.running = true; requestAnimationFrame(() => this.tick()); } }
    tick() {
      const steps = this.nodes.length > 600 ? 1 : 2;
      for (let s = 0; s < steps; s++) this.step();
      this.alpha *= 0.985;
      if (this.alpha < 0.09 && !this.userMoved && !this.refit && this.nodes.length) { this.refit = true; this.fit(); }   // final fit once the layout has settled
      if (!this.fitted && this.alpha < 0.55 && this.nodes.length) { this.fit(); this.fitted = true; }
      this.draw();
      if (this.alpha > 0.02 || this.dragNode) requestAnimationFrame(() => this.tick()); else this.running = false;
    }
    step() {
      const N = this.nodes, a = this.alpha; if (!N.length) return;
      const cell = 60, grid = new Map();
      for (const n of N) { const key = Math.floor(n.x / cell) + ',' + Math.floor(n.y / cell); (grid.get(key) || grid.set(key, []).get(key)).push(n); }
      for (const n of N) {
        const cx = Math.floor(n.x / cell), cy = Math.floor(n.y / cell);
        for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
          const g = grid.get((cx + i) + ',' + (cy + j)); if (!g) continue;
          for (const m of g) {
            if (m === n) continue; let dx = n.x - m.x, dy = n.y - m.y, d2 = dx * dx + dy * dy;
            if (d2 > cell * cell * 4) continue; if (d2 < 1) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
            const f = (900 / d2) * a; const d = Math.sqrt(d2); n.vx += dx / d * f; n.vy += dy / d * f;
          }
        }
      }
      for (const e of this.edges) {
        const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        const len = 40 + (1 - (e.strength || 0.5)) * 80, f = (d - len) * 0.02 * (0.5 + (e.strength || .5)) * a;
        const fx = dx / d * f, fy = dy / d * f; e.a.vx += fx; e.a.vy += fy; e.b.vx -= fx; e.b.vy -= fy;
      }
      const cent = new Map();
      for (const n of N) { const c = cent.get(n.cluster) || cent.set(n.cluster, { x: 0, y: 0, n: 0 }).get(n.cluster); c.x += n.x; c.y += n.y; c.n++; }
      for (const n of N) {
        const c = cent.get(n.cluster); if (c && c.n > 1) { n.vx += (c.x / c.n - n.x) * 0.004 * a; n.vy += (c.y / c.n - n.y) * 0.004 * a; }
        n.vx -= n.x * 0.0016 * a; n.vy -= n.y * 0.0016 * a;
        if (n === this.dragNode) { n.vx = n.vy = 0; continue; }
        n.vx *= 0.82; n.vy *= 0.82; n.x += Math.max(-30, Math.min(30, n.vx)); n.y += Math.max(-30, Math.min(30, n.vy));
      }
    }
    fit() {
      if (!this.nodes.length) return; let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      for (const n of this.nodes) { x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y); }
      const pad = 60, kx = (this.w - pad * 2) / Math.max(40, x1 - x0), ky = (this.h - pad * 2) / Math.max(40, y1 - y0);
      this.k = Math.max(0.05, Math.min(2.2, Math.min(kx, ky))); this.tx = this.w / 2 - ((x0 + x1) / 2) * this.k; this.ty = this.h / 2 - ((y0 + y1) / 2) * this.k; this.draw();
    }
    centerOn(n) { this.tx = this.w / 2 - n.x * this.k; this.ty = this.h / 2 - n.y * this.k; }
    zoomAt(f, px, py) { this.userMoved = true; const k2 = Math.max(0.05, Math.min(6, this.k * f)); this.tx = px - (px - this.tx) * (k2 / this.k); this.ty = py - (py - this.ty) * (k2 / this.k); this.k = k2; this.draw(); }
    toWorld(px, py) { return [(px - this.tx) / this.k, (py - this.ty) / this.k]; }
    hit(px, py) {
      const [wx, wy] = this.toWorld(px, py); let best = null, bd = 1e9;
      for (const n of this.nodes) { if (!this.visible(n)) continue; const d = Math.hypot(n.x - wx, n.y - wy); if (d < Math.max(n.r, 8 / this.k) && d < bd) { bd = d; best = n; } }
      return best;
    }
    visible(n) { const f = this.filter; if (f.types && !f.types.has(n.type)) return false; return true; }
    matches(n) { const q = this.filter.q; return q && n.title.toLowerCase().includes(q); }
    draw() {
      const ctx = this.ctx, k = this.k; ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0); ctx.clearRect(0, 0, this.w, this.h);
      ctx.save(); ctx.translate(this.tx, this.ty); ctx.scale(k, k);
      const focus = this.sel || this.hover; const nb = new Set(); if (focus) { nb.add(focus); for (const e of (this.adj.get(focus) || [])) { nb.add(e.src); nb.add(e.dst); } }
      const q = this.filter.q; const anyQ = !!q;
      // edges (LOD: weak edges dropped when zoomed out)
      for (const e of this.edges) {
        if (!this.visible(e.a) || !this.visible(e.b)) continue;
        if (k < 0.45 && (e.strength || 0) < 0.55 && !(focus && (e.src === focus || e.dst === focus))) continue;
        const hot = focus && (e.src === focus || e.dst === focus);
        ctx.globalAlpha = focus ? (hot ? 0.95 : 0.07) : Math.min(0.55, 0.12 + (e.strength || 0) * 0.5);
        ctx.strokeStyle = EDGE_COLORS[e.type] || '#34475a'; ctx.lineWidth = (hot ? 2 : 0.6 + (e.strength || 0) * 1.4) / Math.max(0.6, k);
        if (e.verification === 'suggested' && e.origin === 'ai') ctx.setLineDash([4 / k, 3 / k]); else ctx.setLineDash([]);
        ctx.beginPath(); ctx.moveTo(e.a.x, e.a.y); ctx.lineTo(e.b.x, e.b.y); ctx.stroke();
      }
      ctx.setLineDash([]);
      // nodes
      const labels = [];
      for (const n of this.nodes) {
        if (!this.visible(n)) continue;
        const dim = (focus && !nb.has(n.id)) || (anyQ && !this.matches(n));
        ctx.globalAlpha = dim ? 0.12 : 1; ctx.fillStyle = GraphView.color(n.type);
        const r = n.r * (n.id === this.sel ? 1.4 : 1);
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 6.2832); ctx.fill();
        if (n.id === this.sel || (anyQ && this.matches(n))) { ctx.globalAlpha = 1; ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.6 / k; ctx.stroke(); }
        if (!dim) labels.push(n);
      }
      // labels with LOD: important nodes when far, everything when close
      ctx.globalAlpha = 1; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      const thr = k < 0.35 ? 0.85 : k < 0.7 ? 0.55 : k < 1.2 ? 0.25 : -1;
      const fs = 11 / Math.max(0.6, Math.min(2, k)); ctx.font = `${fs}px ui-sans-serif,system-ui,sans-serif`;
      const maxRank = this.nodes.reduce((m, n) => Math.max(m, n.rank || 0), 0.0001);
      const boxes = []; let drawn = 0;
      const isForced = n => n.id === this.sel || n.id === this.hover || (anyQ && this.matches(n)) || (focus && nb.has(n.id));
      const order = labels.sort((a, b) => (isForced(b) - isForced(a)) || ((b.rank || 0) - (a.rank || 0)));
      for (const n of order) {
        const forced = isForced(n);
        if (!forced && (n.rank || 0) / maxRank < thr) continue; if (drawn > 140 && !forced) continue;
        const t = n.title.length > 34 ? n.title.slice(0, 33) + '…' : n.title; const x = n.x, y = n.y + n.r + 2;
        const sw = t.length * 5.8, sx = x * k + this.tx - sw / 2, sy = y * k + this.ty, sh = 13;   // screen-space box: skip labels that would overlap
        if (!forced && boxes.some(b => sx < b[0] + b[2] && sx + sw > b[0] && sy < b[1] + b[3] && sy + sh > b[1])) continue;
        boxes.push([sx, sy, sw, sh]); drawn++;
        ctx.lineWidth = 3 / k; ctx.strokeStyle = 'rgba(10,14,19,.85)'; ctx.strokeText(t, x, y); ctx.fillStyle = forced ? '#fff' : '#b9c5d0'; ctx.fillText(t, x, y);
      }
      ctx.restore();
      // cluster captions when zoomed far out
      if (k < 0.5 && this.clusters.length) {
        const cent = new Map(); for (const n of this.nodes) { if (!this.visible(n)) continue; const c = cent.get(n.cluster) || cent.set(n.cluster, { x: 0, y: 0, n: 0 }).get(n.cluster); c.x += n.x; c.y += n.y; c.n++; }
        ctx.font = '12px ui-sans-serif,system-ui,sans-serif'; ctx.textAlign = 'center'; ctx.fillStyle = 'rgba(94,200,229,.75)';
        const placed = [];
        for (const cl of this.clusters.slice(0, 8)) {
          const c = cent.get(cl.id); if (!c || cl.size < 3) continue; const txt = `${cl.label.slice(0, 24)} · ${cl.size}`; const w = ctx.measureText(txt).width;
          const x = this.tx + (c.x / c.n) * k, y = this.ty + (c.y / c.n) * k - 22;
          if (placed.some(p => Math.abs(p.x - x) < (p.w + w) / 2 + 6 && Math.abs(p.y - y) < 16)) continue;     // no overlapping captions
          placed.push({ x, y, w }); ctx.fillText(txt, x, y);
        }
      }
    }
    _bind() {
      const c = this.c; const pos = e => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
      c.addEventListener('wheel', e => { e.preventDefault(); const [x, y] = pos(e); this.zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, x, y); }, { passive: false });
      c.addEventListener('pointerdown', e => {
        c.setPointerCapture(e.pointerId); const [x, y] = pos(e); this.pointers.set(e.pointerId, [x, y]);
        if (this.pointers.size === 2) { const [a, b] = [...this.pointers.values()]; this.pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); this.drag = null; return; }
        this.userMoved = true; const n = this.hit(x, y); this.drag = { x, y, moved: false, node: n, tx: this.tx, ty: this.ty }; if (n) { this.dragNode = n; this.alpha = Math.max(this.alpha, .3); this.kick(); }
        c.style.cursor = 'grabbing';
      });
      c.addEventListener('pointermove', e => {
        const [x, y] = pos(e); if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, [x, y]);
        if (this.pointers.size === 2) { const [a, b] = [...this.pointers.values()]; const d = Math.hypot(a[0] - b[0], a[1] - b[1]); if (this.pinch) this.zoomAt(d / this.pinch, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2); this.pinch = d; return; }
        if (this.drag) {
          const dx = x - this.drag.x, dy = y - this.drag.y; if (Math.abs(dx) + Math.abs(dy) > 4) this.drag.moved = true;
          if (this.drag.node && this.drag.moved) { const [wx, wy] = this.toWorld(x, y); this.drag.node.x = wx; this.drag.node.y = wy; } else if (!this.drag.node) { this.tx = this.drag.tx + dx; this.ty = this.drag.ty + dy; this.draw(); }
        } else { const n = this.hit(x, y); if ((n && n.id) !== this.hover) { this.hover = n ? n.id : null; c.style.cursor = n ? 'pointer' : 'grab'; if (this.opts.onHover) this.opts.onHover(n); this.draw(); } }
      });
      const up = e => {
        const [x, y] = pos(e); this.pointers.delete(e.pointerId); this.pinch = 0; c.style.cursor = 'grab';
        if (this.drag && !this.drag.moved) { const n = this.hit(x, y); this.sel = n ? n.id : null; if (this.opts.onSelect) this.opts.onSelect(n); this.draw(); }
        this.dragNode = null; this.drag = null;
      };
      c.addEventListener('pointerup', up); c.addEventListener('pointercancel', up);
      c.addEventListener('dblclick', e => { const [x, y] = pos(e); const n = this.hit(x, y); if (n && this.opts.onExpand) this.opts.onExpand(n); });
      c.addEventListener('keydown', e => { if (e.key === '+' || e.key === '=') this.zoomAt(1.2, this.w / 2, this.h / 2); if (e.key === '-') this.zoomAt(1 / 1.2, this.w / 2, this.h / 2); if (e.key === '0') this.fit(); });
    }
  }
  window.GraphView = GraphView;
})();
