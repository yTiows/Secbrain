/* Secbrain UI: every view is backed by the /api/v1 endpoints. DOM is built with textContent (no HTML injection from data). */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const API = '/api/v1';
  let token = null; try { token = localStorage.getItem('brk'); } catch (e) {}
  let graph = null, graphData = null, meta = null, currentId = null;

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v; else if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else if (k === 'text') el.textContent = v; else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) { if (kid == null || kid === false) continue; el.append(kid.nodeType ? kid : document.createTextNode(String(kid))); }
    return el;
  }
  const sepj = (...a) => a.filter(x => x != null && x !== '' && x !== false).map((x, i) => i ? [' · ', x] : x).flat();
  const fmtDate = t => t ? new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ') : '—';
  const dot = type => h('span', { class: 'dot', style: `background:${GraphView.color(type)}`, title: type });
  function toast(msg, bad) { const t = $('#toast'); t.textContent = msg; t.className = 'toast show' + (bad ? ' bad' : ''); clearTimeout(toast.t); toast.t = setTimeout(() => t.className = 'toast', 3200); }

  async function api(path, opt = {}) {
    const headers = { Authorization: 'Bearer ' + token }; let body;
    if (opt.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opt.json); }
    if (opt.form) body = opt.form;
    const r = await fetch(API + path, { method: opt.method || (body ? 'POST' : 'GET'), headers, body });
    if (r.status === 401) { logout(); throw new Error('unauthorized'); }
    if (opt.blob) { if (!r.ok) throw new Error(r.statusText); return r.blob(); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || j.error || r.statusText);
    return j;
  }
  const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

  // ---------------------------------------------------------------- auth
  function showLogin() { $('#login').hidden = false; $('#app').hidden = true; }
  function logout() { token = null; try { localStorage.removeItem('brk'); } catch (e) {} showLogin(); }
  $('#loginForm').addEventListener('submit', async e => {
    e.preventDefault(); const t = $('#tokenInput').value.trim(); $('#loginErr').textContent = '';
    const r = await fetch(API + '/me', { headers: { Authorization: 'Bearer ' + t } });
    if (!r.ok) { $('#loginErr').textContent = 'Invalid API key'; return; }
    const me = await r.json(); if (!me.scopes.includes('admin')) { $('#loginErr').textContent = 'The UI requires an admin key'; return; }
    token = t; try { localStorage.setItem('brk', t); } catch (e) {} boot();
  });
  $('#logout').addEventListener('click', logout);

  // ---------------------------------------------------------------- nav
  const views = ['graph', 'explore', 'search', 'intel', 'vault'];
  function show(v) {
    if (matchMedia('(max-width:820px)').matches) closeDetail();          // on phones the detail is a sheet that must not cover other views
    views.forEach(x => { $('#view-' + x).classList.toggle('active', x === v); document.querySelector(`.nav[data-view="${x}"]`).classList.toggle('active', x === v); });
    ({ graph: () => graph && graph.resize(), explore: loadExplorer, search: () => $('#sQ').focus(), intel: loadIntel, vault: loadVault })[v]();
    location.hash = v;
  }
  document.querySelectorAll('.nav[data-view]').forEach(b => b.addEventListener('click', () => show(b.dataset.view)));

  // ---------------------------------------------------------------- graph
  async function loadGraph() {
    const max = $('#gMax').value, ms = $('#gStrength').value;
    const d = await api(`/graph/overview?max_nodes=${max}&min_strength=${ms}`);
    graphData = d; graph.setData(d);
    $('#gInfo').textContent = `${d.shown} of ${d.total_nodes} nodes · ${d.edges.length} relationships · double-click a node to expand its neighborhood`;
    const types = [...new Set(d.nodes.map(n => n.type))].sort();
    const box = $('#gTypes'); box.replaceChildren(); const on = new Set(types);
    types.forEach(t => { const c = h('span', { class: 'chip on', tabindex: 0, role: 'button' }, h('i', { style: `background:${GraphView.color(t)}` }), t);
      const tog = () => { if (on.has(t)) on.delete(t); else on.add(t); c.classList.toggle('on', on.has(t)); graph.setFilter({ types: on.size === types.length ? null : new Set(on) }); };
      c.addEventListener('click', tog); c.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); tog(); } }); box.append(c); });
    $('#gLegend').replaceChildren(...[...new Set(d.nodes.map(n => n.project).filter(Boolean))].slice(0, 6).map(p => h('span', {}, '▸ ' + p)));
  }
  async function expandNode(n) {
    const d = await api(`/graph/neighborhood/${n.id}?hops=2&limit=60`);
    const have = new Set(graphData.nodes.map(x => x.id)); const have_e = new Set(graphData.edges.map(x => x.id));
    d.nodes.forEach(x => { if (!have.has(x.id)) graphData.nodes.push({ id: x.id, title: x.title, type: x.type, project: x.project, importance: x.importance, cluster: n.cluster, degree: 1, rank: 0.3 }); });
    d.edges.forEach(e => { if (!have_e.has(e.id)) graphData.edges.push({ id: e.id, src: e.src, dst: e.dst, type: e.type, strength: e.strength, verification: e.verification, origin: e.origin }); });
    graph.setData(graphData); toast(`Expanded “${n.title}”`);
  }
  function initGraph() {
    graph = new GraphView($('#graph'), { onSelect: n => { if (n) openDetail(n.id); else closeDetail(); }, onExpand: guard(expandNode) });
    $('#gSearch').addEventListener('input', e => graph.setFilter({ q: e.target.value.trim().toLowerCase() }));
    $('#gStrength').addEventListener('input', e => $('#gStrengthVal').textContent = e.target.value);
    $('#gStrength').addEventListener('change', guard(loadGraph)); $('#gMax').addEventListener('change', guard(loadGraph)); $('#gReload').addEventListener('click', guard(loadGraph));
    $('#zIn').addEventListener('click', () => graph.zoomAt(1.3, graph.w / 2, graph.h / 2)); $('#zOut').addEventListener('click', () => graph.zoomAt(1 / 1.3, graph.w / 2, graph.h / 2)); $('#zFit').addEventListener('click', () => graph.fit());
  }

  // ---------------------------------------------------------------- detail
  function closeDetail() { $('#detail').hidden = true; $('#app').classList.remove('has-detail'); currentId = null; if (graph) graph.select(null); }
  $('#dClose').addEventListener('click', closeDetail);
  const confBar = c => h('span', { class: 'conf', title: 'confidence ' + c }, h('b', { style: `width:${Math.round(c * 100)}%` }));
  async function openDetail(id, center) {
    currentId = id; const o = await api('/objects/' + id); const hist = await api(`/objects/${id}/history`);
    $('#detail').hidden = false; $('#app').classList.add('has-detail'); $('#dTitle').textContent = o.title; if (graph) graph.select(id, center);
    const body = $('#dBody'); body.replaceChildren();
    body.append(h('div', { class: 'meta' }, dot(o.type), ' ', ...sepj(o.type, 'v' + o.version, o.status, o.verification, o.project ? h('b', {}, o.project) : null)),
      h('div', { class: 'meta' }, 'importance ', confBar(o.importance), ' confidence ', confBar(o.confidence), ' · updated ' + fmtDate(o.updated_at)));
    body.append(h('div', { class: 'row' }, ...o.tags.map(t => h('span', { class: 'tag' }, '#' + t)), ...o.aliases.map(a => h('span', { class: 'tag', title: 'alias' }, '≈ ' + a))));
    if ((o.extra || {}).security_flags) body.append(h('div', { class: 'finding lvl-protected' }, '⚠ Contains instruction-like text (' + o.extra.security_flags.join(', ') + '). Treated strictly as data.'));
    if (o.summary) body.append(h('div', { class: 'sec' }, h('h3', {}, 'Summary'), h('div', { class: 'snip' }, o.summary)));
    body.append(h('div', { class: 'sec' }, h('h3', {}, 'Content'), h('div', { class: 'content' }, o.content)));
    body.append(h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: () => editObject(o) }, 'Edit'),
      h('button', { class: 'btn small', onclick: guard(async () => { await api(`/objects/${id}`, { method: 'PATCH', json: { verification: 'user_verified', reason: 'verified in UI' } }); toast('Marked verified'); openDetail(id); }) }, 'Verify'),
      h('button', { class: 'btn small', onclick: guard(async () => { await api(`/objects/${id}/${o.status === 'archived' ? 'restore' : 'archive'}`, { method: 'POST' }); toast(o.status === 'archived' ? 'Restored' : 'Archived'); closeDetail(); }) }, o.status === 'archived' ? 'Restore' : 'Archive'),
      h('button', { class: 'btn small danger', onclick: guard(async () => { if (!confirm('Move to deleted? (recoverable; purge is a separate step)')) return; await api('/objects/' + id, { method: 'DELETE' }); toast('Deleted (recoverable)'); closeDetail(); loadExplorer(); }) }, 'Delete')));
    // relationships
    const rels = o.relationships || [];
    const rbox = h('div', { class: 'sec' }, h('h3', {}, `Relationships (${rels.length})`));
    rels.forEach(e => rbox.append(h('div', { class: 'rel' },
      h('div', { class: 'row' }, h('span', { class: 'tag' }, (e.direction === 'in' ? '← ' : '→ ') + e.label.replace(/_/g, ' ')), h('a', { onclick: () => openDetail(e.other_id, true) }, e.other_title), confBar(e.confidence)),
      h('div', { class: 'meta' }, ...sepj(e.origin, e.verification, e.explanation)),
      h('div', { class: 'row' }, e.verification !== 'verified' ? h('button', { class: 'btn small', onclick: guard(async () => { await api('/relationships/' + e.id, { method: 'PATCH', json: { verification: 'verified' } }); toast('Relationship verified'); openDetail(id); }) }, 'Confirm') : null,
        h('button', { class: 'btn small danger', onclick: guard(async () => { await api('/relationships/' + e.id, { method: 'PATCH', json: { verification: 'rejected' } }); toast('Rejected — it will not be re-suggested'); openDetail(id); }) }, 'Reject')))));
    const addRel = h('div', { class: 'row' }, h('input', { id: 'relTarget', placeholder: 'target object id (k_…)' }), h('select', { id: 'relType' }, ...Object.keys(meta.edge_types).map(t => h('option', {}, t))),
      h('button', { class: 'btn small', onclick: guard(async () => { await api('/relationships', { json: { src: id, dst: $('#relTarget').value.trim(), type: $('#relType').value, explanation: 'added manually' } }); toast('Relationship created'); openDetail(id); }) }, 'Add'));
    rbox.append(addRel); body.append(rbox);
    // sources
    body.append(h('div', { class: 'sec' }, h('h3', {}, 'Sources'), ...o.sources.map(s => h('div', { class: 'row' }, h('span', { class: 'tag' }, s.role), s.name,
      h('a', { class: 'btn small', onclick: guard(async () => { const b = await api(`/sources/${s.source_id}/raw`, { blob: true }); const a = h('a', { href: URL.createObjectURL(b), download: s.name }); document.body.append(a); a.click(); a.remove(); }) }, 'original')))));
    // claims
    if (o.claims.length) body.append(h('div', { class: 'sec' }, h('h3', {}, 'Claims (AI-extracted, with evidence)'), ...o.claims.map(c => h('div', { class: 'rel' }, h('div', {}, c.text), h('div', { class: 'meta' }, c.subject ? `${c.subject} · ${c.predicate} · ${c.value}` : 'unstructured', '·', c.verification, '·', 'conf ' + c.confidence)))));
    // explanations / timeline / versions
    const tl = hist.timeline;
    if (tl.events.length > 1) body.append(h('div', { class: 'sec' }, h('h3', {}, 'Temporal evolution'), ...tl.events.map(ev => h('div', { class: 'rel' }, h('div', {}, h('a', { onclick: () => openDetail(ev.id, true) }, ev.title), ' ', h('span', { class: 'meta' }, fmtDate(ev.when), '·', ev.status)), h('div', { class: 'meta' }, ev.summary))), ...tl.relations.map(r => h('div', { class: 'meta' }, `${r.type.replace(/_/g, ' ')}: ${r.why}`))));
    body.append(h('div', { class: 'sec' }, h('h3', {}, 'Version history'), ...hist.versions.map(v => h('div', { class: 'row' }, h('span', { class: 'tag' }, 'v' + v.version), h('span', { class: 'meta' }, ...sepj(fmtDate(v.created_at), v.actor, v.reason)),
      v.version !== o.version ? h('button', { class: 'btn small', onclick: guard(async () => { await api(`/objects/${id}/restore-version/${v.version}`, { method: 'POST' }); toast('Restored v' + v.version + ' as a new version'); openDetail(id); }) }, 'restore') : null))));
    body.scrollTop = 0;
  }
  function editObject(o) {
    const body = $('#dBody'); const t = h('input', { value: o.title, 'aria-label': 'title' }), c = h('textarea', { 'aria-label': 'content' }), tg = h('input', { value: o.tags.join(', '), 'aria-label': 'tags' });
    c.value = o.content; const ty = h('select', {}, ...meta.object_types.map(x => h('option', { selected: x === o.type }, x))); const im = h('input', { type: 'range', min: 0, max: 1, step: 0.05, value: o.importance });
    body.replaceChildren(h('h3', {}, 'Edit'), t, c, h('div', { class: 'row' }, ty, 'importance', im), tg,
      h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: guard(async () => { await api('/objects/' + o.id, { method: 'PATCH', json: { title: t.value, content: c.value, type: ty.value, importance: +im.value, tags: tg.value.split(',').map(x => x.trim()).filter(Boolean), verification: 'user_verified', reason: 'edited in UI' } }); toast('Saved as new version'); openDetail(o.id); }) }, 'Save'), h('button', { class: 'btn', onclick: () => openDetail(o.id) }, 'Cancel')));
  }

  // ---------------------------------------------------------------- explorer
  function objCard(o, extra) {
    return h('div', { class: 'card', tabindex: 0, onclick: guard(() => openDetail(o.id, true)), onkeydown: e => { if (e.key === 'Enter') openDetail(o.id, true); } },
      h('h4', {}, dot(o.type), ' ', o.title), h('div', { class: 'meta' }, ...sepj(o.type, o.project, o.verification, 'v' + (o.version || 1)), ...(o.tags || []).slice(0, 5).map(t => h('span', { class: 'tag' }, '#' + t))),
      o.summary || o.snippet ? h('div', { class: 'snip' }, (o.snippet || o.summary).slice(0, 240)) : null, extra);
  }
  const loadExplorer = guard(async () => {
    const q = new URLSearchParams({ status: $('#eStatus').value, order: $('#eSort').value, limit: 200 });
    if ($('#eType').value) q.set('type', $('#eType').value); if ($('#eProject').value) q.set('project', $('#eProject').value); if ($('#eQ').value) q.set('q', $('#eQ').value);
    const r = await api('/objects?' + q); const box = $('#eList'); box.replaceChildren();
    if (!r.objects.length) box.append(h('p', { class: 'muted' }, 'Nothing here yet. Import documents in the Vault view.'));
    r.objects.forEach(o => box.append(objCard(o)));
    if (!$('#eProject').dataset.loaded) { const ps = await api('/objects?type=project&limit=200'); ps.objects.forEach(p => $('#eProject').append(h('option', {}, p.title))); $('#eProject').dataset.loaded = 1; }
  });
  ['eQ', 'eType', 'eStatus', 'eProject', 'eSort'].forEach(id => $('#' + id).addEventListener(id === 'eQ' ? 'input' : 'change', () => { clearTimeout(loadExplorer.t); loadExplorer.t = setTimeout(loadExplorer, 200); }));

  // ---------------------------------------------------------------- search
  $('#sForm').addEventListener('submit', guard(async e => {
    e.preventDefault(); const q = $('#sQ').value.trim(); const mode = document.querySelector('input[name=smode]:checked').value; const out = $('#sOut'); out.replaceChildren(h('p', { class: 'muted' }, 'Searching…'));
    const fb = (id, kind) => guard(async () => { await api('/feedback', { json: { kind, target: id, query: q } }); toast('Feedback recorded'); });
    if (mode === 'search') {
      const r = await api('/search', { json: { query: q, k: 15 } }); out.replaceChildren(h('div', { class: 'meta' }, `${r.results.length} results · ${r.stats.latency_ms} ms`));
      if (!r.results.length) out.append(h('p', { class: 'muted' }, 'No relevant knowledge found. (Weakly similar items are deliberately not returned.)'));
      r.results.forEach(x => out.append(objCard(Object.assign({ summary: '', tags: [], version: 1 }, x),
        h('div', {}, ...x.why.map(w => h('div', { class: 'why' }, w)), h('div', { class: 'row' }, ...x.sources.map(s => h('span', { class: 'tag' }, '📎 ' + s.name)), h('button', { class: 'btn small', onclick: e => { e.stopPropagation(); fb(x.id, 'useful')(); } }, '👍 useful'), h('button', { class: 'btn small', onclick: e => { e.stopPropagation(); fb(x.id, 'irrelevant')(); } }, '👎 irrelevant'))))));
    } else {
      const p = await api('/context', { json: { query: q, token_budget: +$('#sBudget').value || 1500, k: 8 } });
      out.replaceChildren(h('div', { class: 'meta' }, `knowledge package · ~${p.stats.tokens_est} / ${p.stats.budget} tokens (estimate) · ${p.items.length} items`));
      p.conflicts.forEach(c => out.append(h('div', { class: 'finding lvl-conditional' }, '⚠ Conflict: ' + c.why, h('div', { class: 'meta' }, c.basis))));
      p.uncertainty.filter(u => u.kind !== 'conflict').forEach(u => out.append(h('div', { class: 'finding' }, `${u.kind}: ${u.note}`)));
      p.items.forEach(i => out.append(h('div', { class: 'card', onclick: guard(() => openDetail(i.id, true)) }, h('h4', {}, dot(i.type), ' ', i.title), h('div', { class: 'meta' }, ...sepj(i.type, i.verification, i.date), ...i.sources.map(s => h('span', { class: 'tag' }, '📎 ' + s.name))), h('div', { class: 'snip' }, i.text), h('div', { class: 'why' }, i.why))));
      if (p.relationships.length) out.append(h('div', { class: 'panel' }, h('h3', {}, 'Relationships among results'), ...p.relationships.map(r => h('div', { class: 'meta' }, `${r.type.replace(/_/g, ' ')} (${r.by}): ${r.why}`))));
      if (p.project_context) out.append(h('div', { class: 'panel' }, h('h3', {}, 'Project context'), h('div', {}, `${p.project_context.name} · ${p.project_context.objects} objects`), h('div', { class: 'meta' }, 'Key: ' + p.project_context.key.join(' · '))));
      out.append(h('details', {}, h('summary', { class: 'muted' }, 'Raw package JSON (what an external agent receives)'), h('pre', { class: 'content mono' }, JSON.stringify(p, null, 1))));
    }
  }));

  // ---------------------------------------------------------------- intelligence
  const loadIntel = guard(async () => {
    const s = await api('/intelligence/summary'); const f = await api('/maintenance/findings?status=open'); const ch = await api('/changes?limit=15'); const jobs = await api('/jobs?limit=8');
    const out = $('#intelOut'); out.replaceChildren(); const n = f.findings.length; const bd = $('#intelBadge'); bd.hidden = !n; bd.textContent = n;
    const st = s.stats, g = s.graph;
    out.append(h('div', { class: 'panel wide' }, h('h3', {}, 'Vault'), h('div', { class: 'stat' }, ...[['objects', st.objects], ['sources', st.sources], ['claims', st.claims], ['relationships', g.edges], ['components', g.components], ['isolated', g.isolated], ['open findings', n]].map(([k, v]) => h('div', {}, h('b', {}, v), h('span', {}, k)))),
      h('div', { class: 'meta' }, `embedding: ${s.embedding_model} · LLM: ${s.llm} · last maintenance: ${s.last_maintenance ? fmtDate(s.last_maintenance.at) : 'never'}`)));
    const groups = {}; f.findings.forEach(x => (groups[x.kind] = groups[x.kind] || []).push(x));
    const names = { contradiction: 'Potential contradictions', duplicate: 'Duplicate candidates', outdated: 'Outdated knowledge', concept_candidate: 'Newly discovered concepts', consolidation: 'Consolidation proposals', classification: 'Classification review', isolated: 'Isolated knowledge', unsupported_claim: 'Unsupported claims', security: 'Security flags', broken_source: 'Source integrity', incomplete: 'Incomplete objects', stale: 'Stale projects', source_conflict: 'Source vs. user edits', source_section_removed: 'Sections removed from sources', isolated_cluster: 'Disconnected clusters' };
    Object.entries(groups).forEach(([k, arr]) => out.append(h('div', { class: 'panel' }, h('h3', {}, `${names[k] || k} (${arr.length})`), ...arr.slice(0, 12).map(x => h('div', { class: 'finding' },
      h('div', {}, h('b', {}, x.title)), h('div', { class: 'meta' }, h('span', { class: 'lvl-' + x.level }, x.level.toUpperCase()), ' · confidence ' + x.confidence), h('div', { class: 'meta' }, x.explanation),
      h('div', { class: 'row' }, ...x.targets.slice(0, 3).map(t => h('a', { class: 'btn small', onclick: guard(() => { show('graph'); openDetail(t, true); }) }, 'inspect ' + t.slice(2, 8))),
        x.level !== 'protected' && ['duplicate', 'outdated', 'classification', 'contradiction', 'concept_candidate', 'consolidation'].includes(x.kind) ? h('button', { class: 'btn small primary', onclick: guard(async () => { await api(`/maintenance/findings/${x.id}/apply`, { method: 'POST' }); toast('Applied (reversible from Recent changes)'); loadIntel(); }) }, x.kind === 'duplicate' ? 'Merge' : x.kind === 'contradiction' ? 'Keep newer' : 'Apply') : null,
        h('button', { class: 'btn small', onclick: guard(async () => { await api(`/maintenance/findings/${x.id}/dismiss`, { method: 'POST' }); loadIntel(); }) }, 'Dismiss')))))));
    if (!n) out.append(h('div', { class: 'panel' }, h('h3', {}, 'Findings'), h('p', { class: 'muted' }, 'No open findings. Run maintenance to inspect the vault.')));
    out.append(h('div', { class: 'panel' }, h('h3', {}, `Knowledge gaps (${s.gaps.length})`), ...s.gaps.slice(0, 12).map(x => h('div', { class: 'finding' }, h('div', { class: 'meta' }, x.kind.replace(/_/g, ' ')), x.detail))));
    out.append(h('div', { class: 'panel' }, h('h3', {}, 'Recent changes'), ...ch.changes.map(c => h('div', { class: 'row' }, h('span', { class: 'meta' }, fmtDate(c.ts), c.actor), h('span', {}, c.summary), c.undone ? h('span', { class: 'tag' }, 'undone') : (c.undo && c.undo.op && c.undo.op !== 'none' ? h('button', { class: 'btn small', onclick: guard(async () => { await api(`/changes/${c.id}/undo`, { method: 'POST' }); toast('Change undone'); loadIntel(); }) }, 'undo') : null)))));
    out.append(h('div', { class: 'panel' }, h('h3', {}, 'Recent ingestion jobs'), ...jobs.jobs.map(j => h('div', { class: 'meta' }, fmtDate(j.created_at), h('b', { class: j.status === 'failed' ? 'err' : j.status === 'done' ? 'ok' : 'warn' }, j.status), (j.result && j.result.summary) || j.error || ''))));
  });
  $('#runMaint').addEventListener('click', guard(async () => { $('#maintStatus').textContent = 'running…'; const r = await api('/maintenance/run', { method: 'POST' }); $('#maintStatus').textContent = `done in ${r.elapsed_s}s`; loadIntel(); }));

  // ---------------------------------------------------------------- vault
  const loadVault = guard(async () => {
    const out = $('#vaultOut'); out.replaceChildren();
    const jobsBox = h('div', { class: 'panel wide' }, h('h3', {}, 'Ingestion jobs'));
    const refreshJobs = guard(async () => { const jobs = await api('/jobs?limit=15'); jobsBox.replaceChildren(h('h3', {}, 'Ingestion jobs'), ...jobs.jobs.map(j => h('div', { class: 'finding' }, h('div', { class: 'row' }, h('b', { class: j.status === 'failed' ? 'err' : j.status === 'done' || j.status === 'unchanged' ? 'ok' : 'warn' }, j.status), h('span', { class: 'meta' }, fmtDate(j.created_at), 'attempts', j.attempts)), h('div', { class: 'meta' }, (j.result && j.result.summary) || j.error || ''),
      h('div', { class: 'row' }, j.status === 'awaiting_review' ? h('button', { class: 'btn small primary', onclick: guard(async () => { await api(`/jobs/${j.id}/approve`, { method: 'POST' }); toast('Approved'); refreshJobs(); }) }, 'Approve & commit') : null, j.status === 'failed' ? h('button', { class: 'btn small', onclick: guard(async () => { await api(`/jobs/${j.id}/retry`, { method: 'POST' }); refreshJobs(); }) }, 'Retry') : null)))); });
    // upload
    const review = h('input', { type: 'checkbox', id: 'upReview' }); const proj = h('input', { placeholder: 'project (optional)' });
    const fileIn = h('input', { type: 'file', multiple: true, accept: '.txt,.md,.markdown,.json,.pdf,.docx,.csv,.log', hidden: true });
    const drop = h('div', { class: 'drop', tabindex: 0, onclick: () => fileIn.click() }, 'Drop files here or click to choose — txt · md · json · pdf · docx');
    const upload = guard(async files => { for (const f of files) { const fd = new FormData(); fd.append('file', f); fd.append('uri', f.webkitRelativePath || f.name); if (proj.value) fd.append('project', proj.value); if (review.checked) fd.append('review', 'true'); const j = await api('/ingest/file', { form: fd }); toast(`${f.name}: ${j.status}${j.error ? ' — ' + j.error : ''}`, j.status === 'failed'); } refreshJobs(); });
    fileIn.addEventListener('change', () => upload([...fileIn.files])); ['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); })); ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => upload([...e.dataTransfer.files]));
    out.append(h('div', { class: 'panel' }, h('h3', {}, 'Import documents'), drop, fileIn, h('div', { class: 'row' }, proj, h('label', { class: 'inline' }, review, 'review extraction before committing')), h('p', { class: 'muted' }, 'Originals are preserved untouched. Re-importing identical content is a no-op; changed content creates new versions.')));
    // note
    const nt = h('input', { placeholder: 'title (optional)' }), nc = h('textarea', { placeholder: 'Write a note, fact, decision, idea…' }), ntype = h('select', {}, h('option', { value: '' }, 'auto-classify'), ...meta.object_types.map(x => h('option', {}, x))), ntags = h('input', { placeholder: 'tags, comma-separated' }), nproj = h('input', { placeholder: 'project' });
    out.append(h('div', { class: 'panel' }, h('h3', {}, 'New note'), nt, nc, h('div', { class: 'row' }, ntype, ntags, nproj), h('button', { class: 'btn primary', onclick: guard(async () => { const j = await api('/ingest/note', { json: { content: nc.value, title: nt.value || null, type: ntype.value || null, tags: ntags.value.split(',').map(x => x.trim()).filter(Boolean), project: nproj.value || null } }); toast(j.result.summary || j.status, j.status === 'failed'); nc.value = ''; nt.value = ''; refreshJobs(); }) }, 'Ingest note')));
    out.append(jobsBox); refreshJobs();
    // export/import/backup
    const zipIn = h('input', { type: 'file', accept: '.zip,.md', hidden: true });
    zipIn.addEventListener('change', guard(async () => { const fd = new FormData(); fd.append('file', zipIn.files[0]); const r = await api('/vault/import', { form: fd }); toast(`Imported: ${r.created_or_updated ?? r.action} (${(r.failed || []).length} failed)`, (r.failed || []).length > 0); }));
    out.append(h('div', { class: 'panel' }, h('h3', {}, 'Portability & backup'), h('p', { class: 'muted' }, 'Markdown vault export is Obsidian-compatible (frontmatter ids, [[wiki-links]], backlinks, original attachments). The database remains the source of truth.'),
      h('div', { class: 'row' }, h('button', { class: 'btn', onclick: guard(async () => { const b = await api('/vault/export', { blob: true }); const a = h('a', { href: URL.createObjectURL(b), download: 'secbrain-vault.zip' }); document.body.append(a); a.click(); a.remove(); }) }, 'Export Markdown vault (.zip)'),
        h('button', { class: 'btn', onclick: () => zipIn.click() }, 'Import vault / .md'), zipIn,
        h('button', { class: 'btn', onclick: guard(async () => { const r = await api('/vault/backup', { method: 'POST' }); toast('Backup written: ' + r.backup); }) }, 'Backup database'),
        h('button', { class: 'btn', onclick: guard(async () => { const r = await api('/vault/reindex', { method: 'POST' }); toast(`Reindexed ${r.objects} objects`); }) }, 'Rebuild indexes'))));
    // keys
    const keysBox = h('div', { class: 'panel' }, h('h3', {}, 'API keys (external agents)'));
    const kn = h('input', { placeholder: 'name, e.g. claude' }), kp = h('input', { placeholder: 'restrict to projects (comma)' }), ks = h('select', {}, h('option', {}, 'read'), h('option', {}, 'read,write'));
    const refreshKeys = guard(async () => { const k = await api('/keys'); keysBox.replaceChildren(h('h3', {}, 'API keys (external agents)'), ...k.keys.map(x => h('div', { class: 'row' }, h('b', {}, x.name), h('span', { class: 'tag' }, x.scopes.join('+')), x.projects.length ? h('span', { class: 'tag' }, 'projects: ' + x.projects.join(',')) : null, x.revoked_at ? h('span', { class: 'tag' }, 'revoked') : h('button', { class: 'btn small danger', onclick: guard(async () => { await api('/keys/' + x.id, { method: 'DELETE' }); refreshKeys(); }) }, 'revoke'))),
      h('div', { class: 'row' }, kn, ks, kp, h('button', { class: 'btn small primary', onclick: guard(async () => { const r = await api('/keys', { json: { name: kn.value || 'agent', scopes: ks.value.split(','), projects: kp.value.split(',').map(x => x.trim()).filter(Boolean) } }); prompt('Copy this key now — it is shown only once:', r.token); refreshKeys(); }) }, 'Create key'))); });
    out.append(keysBox); refreshKeys();
  });

  // ---------------------------------------------------------------- boot
  async function boot() {
    $('#login').hidden = true; $('#app').hidden = false;
    try { meta = await api('/meta'); } catch (e) { return; }
    const sel = $('#eType'); if (sel.options.length === 1) meta.object_types.forEach(t => sel.append(h('option', {}, t)));
    if (matchMedia('(min-width:821px)').matches) $('#gFilters').open = true;
    if (!graph) initGraph(); await guard(loadGraph)();
    const v = (location.hash || '#graph').slice(1); show(views.includes(v) ? v : 'graph');
    guard(async () => { const f = await api('/maintenance/findings?status=open'); const b = $('#intelBadge'); b.hidden = !f.findings.length; b.textContent = f.findings.length; })();
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDetail(); if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') { e.preventDefault(); show('search'); } });
  }
  // ---- phone automation: one-tap login link (#key=…), Web Share Target (share text/links into the vault), service worker
  async function autoLogin() {
    const m = /[#&]key=(brk_[\w-]+)/.exec(location.hash); if (!m) return false;
    history.replaceState(null, '', location.pathname + location.search);          // the key never stays in the address bar/history
    const r = await fetch(API + '/me', { headers: { Authorization: 'Bearer ' + m[1] } }); if (!r.ok) return false;
    token = m[1]; try { localStorage.setItem('brk', token); } catch (e) {} return true;
  }
  async function handleShare() {
    const q = new URLSearchParams(location.search); const text = [q.get('title'), q.get('text'), q.get('url')].filter(Boolean).join('\n\n');
    if (!text || !token) return; history.replaceState(null, '', location.pathname);
    try { const j = await api('/ingest/note', { json: { content: text, title: q.get('title') || null, tags: ['shared'] } }); toast(j.status === 'done' ? 'Saved to your brain ✓' : 'Share: ' + j.status, j.status === 'failed'); guard(loadGraph)(); } catch (e) { toast('Share failed: ' + e.message, true); }
  }
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) navigator.serviceWorker.register('sw.js').catch(() => {});
  (async () => { await autoLogin(); if (token) { await boot(); await handleShare(); } else showLogin(); })();
})();
