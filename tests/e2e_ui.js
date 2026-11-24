// Real-browser journey against a running server:  node tests/e2e_ui.js <data_dir_with_admin.key> [base_url]
// Exercises the UI → API → DB path: login, import a file, write a note, search, inspect, edit (versioned), verify,
// reject a relationship, run maintenance, view findings, export the vault.
const { chromium } = require('playwright');
const fs = require('fs'); const path = require('path'); const os = require('os');
const base = process.argv[3] || 'http://127.0.0.1:8765';
const token = fs.readFileSync(path.join(process.argv[2], 'admin.key'), 'utf8').trim();
const api = async (p, o = {}) => (await fetch(base + '/api/v1' + p, { ...o, headers: { Authorization: 'Bearer ' + token, 'content-type': 'application/json', ...(o.headers || {}) } })).json();
let failures = 0; const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) failures++; };
(async () => {
  const b = await chromium.launch(); const page = await (await b.newContext({ viewport: { width: 1366, height: 820 }, acceptDownloads: true })).newPage();
  const errs = []; page.on('pageerror', e => errs.push(e.message)); page.on('console', m => m.type() === 'error' && errs.push(m.text()));
  await page.goto(base + '/');
  await page.fill('#tokenInput', 'brk_wrong'); await page.click('#loginForm button'); await page.waitForTimeout(400);
  ok((await page.textContent('#loginErr')).includes('Invalid'), 'wrong key is rejected'); errs.length = 0;   // the browser logs the intentional 401
  await page.fill('#tokenInput', token); await page.click('#loginForm button'); await page.waitForSelector('#app:not([hidden])');
  await page.waitForTimeout(1500); ok(await page.locator('#graph').count() === 1, 'graph view loads');
  // import a file through the Vault view
  await page.click('.nav[data-view=vault]'); await page.waitForSelector('.drop');
  const f = path.join(os.tmpdir(), 'e2e-ui-note.md');
  fs.writeFileSync(f, '---\nproject: E2E\n---\n# Deploy window\n\nWe decided to freeze deployments on Fridays because the on-call team is smaller. The deploy freeze starts at 14:00 UTC.\n');
  await page.setInputFiles('input[type=file][multiple]', f); await page.waitForTimeout(1500);
  let jobs = await api('/jobs?limit=3'); ok(jobs.jobs[0].status === 'done', 'uploaded file ingested (job done): ' + jobs.jobs[0].result.summary);
  // note composer
  await page.fill('.panel textarea', 'I prefer asynchronous standups because they respect focus time.'); await page.click('text=Ingest note'); await page.waitForTimeout(1200);
  const s1 = await api('/search', { method: 'POST', body: JSON.stringify({ query: 'asynchronous standups' }) });
  ok(s1.results.length > 0 && s1.results[0].type === 'preference', 'note ingested + auto-classified as preference');
  // search UI (context mode)
  await page.click('.nav[data-view=search]'); await page.fill('#sQ', 'When do deployments freeze?'); await page.check('input[value=context]'); await page.click('#sForm button[type=submit]'); await page.waitForTimeout(900);
  const cards = await page.locator('#sOut .card').count(); ok(cards > 0 && (await page.textContent('#sOut')).includes('14:00 UTC'), 'context package shows the freeze rule with sources');
  ok((await page.textContent('#sOut')).includes('e2e-ui-note.md'), 'package item cites its source file');
  // open detail from the card, edit & verify
  await page.click('#sOut .card >> nth=0'); await page.waitForSelector('#detail:not([hidden])');
  const oid = (await api('/search', { method: 'POST', body: JSON.stringify({ query: 'deployments freeze Fridays' }) })).results[0].id;
  await page.click('#dBody >> text=Edit'); await page.fill('#dBody textarea', 'We decided to freeze deployments on Fridays and Saturdays because the on-call team is smaller.'); await page.click('#dBody >> text=Save'); await page.waitForTimeout(800);
  let o = await api('/objects/' + oid); ok(o.version >= 2 && o.content.includes('Saturdays') && o.verification === 'user_verified', 'edit saved as a new version, marked user-verified');
  const h = await api(`/objects/${oid}/history`); ok(h.versions.length >= 2 && h.versions.at(-1).snapshot.content.includes('14:00'), 'previous version preserved');
  // reject a relationship
  const rel = o.relationships.find(r => r.origin === 'ai' && r.verification === 'suggested');
  if (rel) { page.once('dialog', d => d.accept()); await page.locator('#dBody .rel', { hasText: rel.other_title }).locator('text=Reject').first().click(); await page.waitForTimeout(700);
    const o2 = await api('/objects/' + oid + '/relationships'); ok(o2.relationships.find(r => r.id === rel.id).verification === 'rejected', 'rejected relationship persisted'); } else ok(true, 'no AI relationship to reject (skipped)');
  // maintenance + findings
  await page.click('.nav[data-view=intel]'); await page.click('#runMaint'); await page.waitForSelector('#maintStatus:has-text("done")', { timeout: 20000 });
  ok(await page.locator('#intelOut .panel').count() > 2, 'intelligence center renders panels');
  // export vault
  await page.click('.nav[data-view=vault]'); const dl = page.waitForEvent('download'); await page.click('text=Export Markdown vault'); const d = await dl; const p = await d.path();
  ok(fs.statSync(p).size > 2000, 'vault zip downloaded');
  ok(errs.length === 0, 'no console/page errors ' + JSON.stringify(errs.slice(0, 3)));
  await b.close(); process.exit(failures ? 1 : 0);
})();
