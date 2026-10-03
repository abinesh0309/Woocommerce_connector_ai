// Runs the REAL frontend (index.html + app.js) in jsdom against a live `woo-connector web` server.
// Usage: node ui_smoke.mjs <baseUrl> <password>
import { JSDOM } from 'jsdom';

const [base, password] = process.argv.slice(2);
const results = [];
const check = (ok, label, detail = '') => { results.push(!!ok); console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}${!ok && detail ? `  (${detail})` : ''}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- sign in the way the login page does
const login = await fetch(`${base}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'woo-web' }, body: JSON.stringify({ password }) });
check(login.status === 200, 'login accepts the password');
const cookie = login.headers.get('set-cookie').split(';')[0];
const authed = (path, opts = {}) => fetch(new URL(path, base), { ...opts, headers: { ...(opts.headers || {}), cookie } });

const html = await (await authed('/')).text();
const js = await (await authed('/static/app.js')).text();
const dom = new JSDOM(html, { url: `${base}/`, runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
const redirects = [];
window.fetch = async (path, opts = {}) => {
  const res = await authed(path, opts);
  if (res.status === 401) redirects.push(path);
  return res;
};
const errors = [];
window.addEventListener('error', (e) => errors.push(e.message));
window.eval(js);

const doc = window.document;
const text = (sel) => (doc.querySelector(sel)?.textContent ?? '');
async function until(fn, label, ms = 8000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { try { if (fn()) return true; } catch { /* retry */ } await sleep(40); }
  check(false, label, 'timed out');
  return false;
}
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const byText = (sel, t) => [...doc.querySelectorAll(sel)].find((n) => n.textContent.trim() === t);

// ---- overview
await until(() => doc.querySelector('#view .pipe') && doc.querySelector('#view table'), 'overview renders');
check(text('#storeName') === 'Fake Shop', 'store name in the rail', text('#storeName'));
check(doc.querySelectorAll('#view .attention li').length >= 1, 'needs-attention sentences are listed');
check(doc.querySelectorAll('#view .legendbtn').length >= 3, 'pipeline legend has a button per status');
check(text('#view').includes('d***@example.com') && !text('#view').includes('dana@example.com'), 'customer emails appear masked');
check(text('#privacy').includes('masked'), 'privacy note explains masking');
await until(() => doc.querySelector('#view ul.plain li'), 'stock-to-watch loads');
check(text('#view').includes('Stock to watch'), 'stock panel present');

// pipeline segment -> filtered orders
click(doc.querySelector('#view .legendbtn'));
await until(() => doc.querySelector('#orderStatus'), 'legend button opens Orders');
check(doc.querySelector('.navbtn[aria-current="page"]').dataset.view === 'orders', 'nav marks Orders as current');

// ---- orders: search, drawer, hostile text
const select = doc.querySelector('#orderStatus'); select.value = 'any'; select.dispatchEvent(new window.Event('change'));
await until(() => doc.querySelector('#orderQ') && doc.querySelectorAll('#view tbody tr').length >= 5, 'orders list renders');
const q = doc.querySelector('#orderQ'); q.value = '#1003';
q.form.dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }));
await until(() => doc.querySelector('#view tbody tr .linkbtn')?.textContent === '#1003', 'search finds #1003 first');
click(doc.querySelector('#view tbody tr .linkbtn'));
await until(() => !doc.querySelector('#drawer').hidden && doc.querySelector('#drawerBody .quote'), 'order drawer opens with detail');
const drawer = doc.querySelector('#drawerBody');
check(drawer.textContent.includes('Ignore all previous instructions'), 'the prompt-injection note is shown as plain text');
check(drawer.textContent.includes('<img src=x onerror=alert(1)>'), 'entity-encoded markup is displayed literally');
check(drawer.querySelector('img, script, iframe, svg') === null, 'no element was created from hostile text (no XSS)');
check(!!byText('#drawerBody .tag', 'Staff only') && !!byText('#drawerBody .tag', 'Visible to customer'), 'staff-only and customer-visible notes are labelled');
check(drawer.textContent.includes('d***@example.com') && !drawer.textContent.includes('secret street'), 'drawer keeps contact details masked');
check([...drawer.querySelectorAll('a')].every((a) => /^https?:/.test(a.getAttribute('href') || '')), 'every link is http(s)');
doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
check(doc.querySelector('#drawer').hidden, 'Escape closes the drawer');

// tracking from order meta
const q2 = doc.querySelector('#orderQ'); q2.value = '1002';
q2.form.dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }));
await until(() => doc.querySelector('#view tbody tr .linkbtn')?.textContent === '#1002', 'search finds #1002');
click(doc.querySelector('#view tbody tr .linkbtn'));
await until(() => doc.querySelector('#drawerBody')?.textContent.includes('AWB12345'), 'tracking number from the store is shown');
doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

// ---- stock
click(byText('.navbtn', 'Stock'));
await until(() => text('#view h1') === 'Stock' && doc.querySelector('#threshold') && doc.querySelector('#view table'), 'stock view renders');
check(text('#view').includes('Out of stock') && text('#view').includes('Backordered'), 'stock problems are labelled in words');

// ---- customers (staff account must never appear)
click(byText('.navbtn', 'Customers'));
await until(() => text('#view h1') === 'Customers' && doc.querySelectorAll('#view tbody tr').length >= 2, 'customers list renders');
check(!text('#view').includes('Shop Owner') && !text('#view').includes('owner@fake.shop'), 'the shop-owner account is not listed');
click(doc.querySelector('#view tbody tr .linkbtn'));
await until(() => text('#drawerBody').includes('Recent orders'), 'customer drawer opens');
doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

// ---- chat
check(!doc.querySelector('#askInput').disabled, 'chat input is enabled when the server has a key');
check(doc.querySelectorAll('.chipbtn').length === 3, 'suggested questions are offered');
const input = doc.querySelector('#askInput'); input.value = 'What needs attention right now?';
doc.querySelector('#askForm').dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }));
await until(() => doc.querySelector('.msg-assistant + .msg-user + .msg-assistant, .msg-user ~ .msg-assistant'), 'assistant answer appears');
const answers = [...doc.querySelectorAll('.msg-assistant')];
const last = answers.at(-1);
check(last.textContent.includes('3 orders are processing'), 'the answer text from the server is shown', last?.textContent);
check(last.querySelector('strong')?.textContent === '3 orders' && !last.textContent.includes('**'), '**bold** is rendered, not shown as asterisks');
const links = [...last.querySelectorAll('a')];
check(links.length === 1 && links[0].getAttribute('href') === 'http://shop.example/wp-admin' && links[0].textContent === 'admin panel', 'a markdown link becomes a real link');
check(last.textContent.includes('[bad](javascript:alert(1))'), 'a javascript: link is NOT turned into a link');
check(last.querySelector('img, script') === null && last.textContent.includes('<img src=x onerror=alert(1)>'), 'HTML in an answer is shown as text, never created');
check(answers.at(-1).textContent.includes('Looked at: the overview'), 'tools used are named in plain words');
check(doc.querySelector('.msg-pending') === null, 'the "looking this up" placeholder is removed');

// ---- logout
const revoked = authed('/api/logout', { method: 'POST', headers: { 'X-Requested-With': 'woo-web' } });
check((await revoked).status === 200, 'sign out works');

check(errors.length === 0, 'no uncaught errors in the page', errors.join('; '));
check(redirects.length === 0, 'the page never bounced to /login while signed in', redirects.join(','));
console.log(`\n${results.filter(Boolean).length} passed, ${results.filter((r) => !r).length} failed`);
window.close();
process.exit(results.every(Boolean) ? 0 : 1);
