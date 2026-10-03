(() => {
  'use strict';

  // ---------------------------------------------------------------- helpers
  // Everything that comes from the server is inserted as text (never as HTML): order notes and names are written by customers.
  const $ = (selector, root = document) => root.querySelector(selector);

  const el = (tag, props = {}, ...kids) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (key === 'href') { if (/^https?:\/\//i.test(value)) node.setAttribute('href', value); }
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return node;
  };

  // Drop null/false entries (optional sections) so they are not rendered as the text "null".
  const nodes = (content) => [].concat(content).flat(Infinity).filter((n) => n != null && n !== false);

  class ApiError extends Error {}

  async function api(path, options = {}) {
    const headers = { 'X-Requested-With': 'woo-web' };
    if (options.body) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, { credentials: 'same-origin', ...options, headers });
    if (res.status === 401) {
      window.location.assign('/login');
      throw new ApiError('Signed out.');
    }
    let body = {};
    try { body = await res.json(); } catch { /* not JSON */ }
    if (!res.ok) throw new ApiError(body.error || `The request failed (${res.status}).`);
    return body;
  }

  const qs = (params) => {
    const out = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value != null && value !== '') out.set(key, value);
    const text = out.toString();
    return text ? `?${text}` : '';
  };

  const money = (amount, currency) => {
    if (amount == null || amount === '') return '–';
    try { return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(Number(amount)); }
    catch { return `${currency || ''} ${amount}`.trim(); }
  };

  const when = (iso) => {
    if (!iso) return '';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  };

  const STATUS = {
    pending: ['Pending payment', 's-pending'], processing: ['Processing', 's-processing'], 'on-hold': ['On hold', 's-hold'],
    completed: ['Completed', 's-done'], cancelled: ['Cancelled', 's-muted'], refunded: ['Refunded', 's-muted'],
    failed: ['Failed', 's-failed'], 'checkout-draft': ['Draft', 's-muted'],
  };
  const chip = (status) => {
    const [label, cls] = STATUS[status] || [status, 's-muted'];
    return el('span', { class: `chip ${cls}` }, el('i', { class: 'dot', 'aria-hidden': 'true' }), label);
  };

  const loading = (text = 'Loading…') => el('p', { class: 'muted', role: 'status' }, text);
  const notice = (message, retry) => el('div', { class: 'notice error', role: 'alert' }, el('span', { text: message }), retry && el('button', { class: 'linkbtn', onclick: retry }, 'Try again'));
  const empty = (title, hint) => el('div', { class: 'empty' }, el('p', { class: 'emptytitle', text: title }), hint && el('p', { class: 'muted', text: hint }));

  function table(columns, rows) {
    if (!rows.length) return null;
    const head = el('tr', {}, columns.map((c) => el('th', { scope: 'col', class: c.cls }, c.label)));
    const body = rows.map((cells) => el('tr', {}, cells.map((cell, i) => el('td', { 'data-label': columns[i].label, class: columns[i].cls }, cell))));
    return el('div', { class: 'tablewrap' }, el('table', {}, el('thead', {}, head), el('tbody', {}, body)));
  }

  function pager(page, totalPages, total, go) {
    if (!totalPages || totalPages <= 1) return total ? el('p', { class: 'muted small', text: `${total} result${total === 1 ? '' : 's'}` }) : null;
    return el('div', { class: 'pager' },
      el('button', { class: 'ghost', disabled: page <= 1, onclick: () => go(page - 1) }, 'Previous'),
      el('span', { class: 'muted', text: `Page ${page} of ${totalPages} (${total} results)` }),
      el('button', { class: 'ghost', disabled: page >= totalPages, onclick: () => go(page + 1) }, 'Next'));
  }

  const adminLink = (url, label) => el('a', { class: 'ext', href: url, target: '_blank', rel: 'noopener noreferrer' }, label);

  // ---------------------------------------------------------------- state
  const ctx = { currency: '', config: { chat_enabled: false } };
  const state = { orders: { status: 'any', q: '', page: 1 }, customers: { q: '', page: 1 }, stock: { threshold: 5, q: '', page: 1 } };
  const view = $('#view');
  let renderToken = 0;

  async function guarded(container, fn) {
    const token = ++renderToken;
    container.replaceChildren(loading());
    try {
      const content = await fn();
      if (token === renderToken) container.replaceChildren(...nodes(content));
    } catch (error) {
      if (token === renderToken) container.replaceChildren(notice(error.message, () => guarded(container, fn)));
    }
  }

  // ---------------------------------------------------------------- order bits
  function orderTable(rows) {
    return table(
      [{ label: 'Order' }, { label: 'Placed', cls: 'nowrap' }, { label: 'Status' }, { label: 'Customer' }, { label: 'Items' }, { label: 'Total', cls: 'num' }],
      rows.map((o) => [
        el('button', { class: 'linkbtn strong', onclick: () => openOrder(o.id) }, `#${o.number}`),
        when(o.created_at),
        chip(o.status),
        el('span', {}, o.customer_name || 'Guest', o.customer_email && el('span', { class: 'sub', text: o.customer_email })),
        (o.items || []).join(', '),
        money(o.total, o.currency || ctx.currency),
      ]),
    );
  }

  // ---------------------------------------------------------------- views
  async function overview() {
    const [ov, recent] = await Promise.all([api('/api/overview'), api(`/api/orders${qs({ per_page: 6 })}`)]);
    const c = ov.counts;
    const segs = [['pending', 'Pending payment', c.pending, 's-pending'], ['processing', 'Processing', c.processing, 's-processing'], ['on-hold', 'On hold', c.on_hold, 's-hold'], ['failed', 'Failed', c.failed, 's-failed']];
    const total = segs.reduce((n, s) => n + s[2], 0);

    const pipeline = total
      ? el('div', {},
        el('div', { class: 'pipe', 'aria-hidden': 'true' }, segs.filter((s) => s[2] > 0).map((s) => el('span', { class: `pipeseg ${s[3]}`, style: `flex:${s[2]}` }))),
        el('ul', { class: 'legend' }, segs.map((s) => el('li', {}, el('button', { class: 'legendbtn', onclick: () => go('orders', { status: s[0] }) }, el('i', { class: `dot ${s[3]}`, 'aria-hidden': 'true' }), el('span', { text: s[1] }), el('strong', { class: 'num', text: String(s[2]) }))))))
      : empty('No open orders', 'Nothing is pending, processing, on hold or failed.');

    const attention = el('ul', { class: 'attention' }, ov.needs_attention.map((t) => el('li', { text: t })));
    const lowBox = el('div', {}, loading('Checking stock…'));
    api(`/api/low-stock${qs({ threshold: 5 })}`).then((r) => {
      const items = r.items.slice(0, 6);
      lowBox.replaceChildren(
        items.length
          ? el('ul', { class: 'plain' }, items.map((i) => el('li', {}, el('span', { text: i.name }), el('span', { class: 'sub' }, issueLabel(i.issue), i.stock_quantity != null ? ` (${i.stock_quantity})` : ''))))
          : empty('Stock looks fine', `Nothing at or below 5 units among ${r.products_scanned} products.`),
        r.truncated && el('p', { class: 'muted small', text: 'Only part of the catalogue was scanned.' }),
        items.length ? el('button', { class: 'linkbtn', onclick: () => go('stock') }, 'See all stock issues') : null);
    }).catch((e) => lowBox.replaceChildren(notice(e.message)));

    return [
      el('h1', {}, 'Overview'),
      el('section', { class: 'block' }, el('h2', {}, 'Needs attention'), attention),
      el('section', { class: 'block' }, el('h2', {}, 'Order pipeline'), pipeline, el('p', { class: 'muted small', text: `${c.last_24h} order${c.last_24h === 1 ? '' : 's'} placed in the last 24 hours.` })),
      el('div', { class: 'cols' },
        el('section', { class: 'block' }, el('h2', {}, 'Latest orders'), orderTable(recent.items) || empty('No orders yet'), el('button', { class: 'linkbtn', onclick: () => go('orders') }, 'See all orders')),
        el('section', { class: 'block' }, el('h2', {}, 'Stock to watch'), lowBox)),
      ov.admin_url ? adminLink(ov.admin_url, 'Open orders in WooCommerce') : null,
    ];
  }

  async function orders() {
    const s = state.orders;
    const data = await api(`/api/orders${qs({ q: s.q, status: s.status === 'any' ? '' : s.status, page: s.page })}`);
    const statusSelect = el('select', { id: 'orderStatus', onchange: (e) => { s.status = e.target.value; s.page = 1; render('orders'); } },
      Object.entries({ any: 'Any status', ...Object.fromEntries(Object.entries(STATUS).map(([k, v]) => [k, v[0]])) }).map(([value, label]) => el('option', { value, selected: value === s.status }, label)));
    const search = el('input', { id: 'orderQ', type: 'search', placeholder: 'Order number, email, name or product', value: s.q, 'aria-label': 'Search orders' });
    const form = el('form', { class: 'filters', onsubmit: (e) => { e.preventDefault(); s.q = search.value.trim(); s.page = 1; render('orders'); } },
      search, el('label', { class: 'sr', for: 'orderStatus' }, 'Status'), statusSelect, el('button', { class: 'primary', type: 'submit' }, 'Search'),
      (s.q || s.status !== 'any') && el('button', { class: 'ghost', type: 'button', onclick: () => { s.q = ''; s.status = 'any'; s.page = 1; render('orders'); } }, 'Clear'));
    return [
      el('h1', {}, 'Orders'), form,
      orderTable(data.items) || empty('No orders match', 'Try another status, or search by order number, customer email or product name.'),
      pager(data.page, data.total_pages, data.total, (p) => { s.page = p; render('orders'); }),
    ];
  }

  const ISSUES = { low: 'Low', out_of_stock: 'Out of stock', oversold: 'Oversold (negative stock, backorders off)', backordered: 'Backordered' };
  const issueLabel = (key) => ISSUES[key] || key;

  async function stock() {
    const s = state.stock;
    const [low, products] = await Promise.all([
      api(`/api/low-stock${qs({ threshold: s.threshold })}`),
      api(`/api/products${qs({ q: s.q, page: s.page })}`),
    ]);
    const thresholdInput = el('input', { id: 'threshold', type: 'number', min: '0', max: '100000', value: String(s.threshold), 'aria-label': 'Low-stock threshold' });
    const tform = el('form', { class: 'filters', onsubmit: (e) => { e.preventDefault(); s.threshold = Math.max(0, parseInt(thresholdInput.value, 10) || 0); render('stock'); } },
      el('label', { for: 'threshold' }, 'Flag products at or below'), thresholdInput, el('span', { class: 'muted', text: 'units' }), el('button', { class: 'primary', type: 'submit' }, 'Check'));
    const issues = table(
      [{ label: 'Product' }, { label: 'SKU' }, { label: 'Problem' }, { label: 'In stock', cls: 'num' }],
      low.items.map((i) => [i.name, i.sku || '–', issueLabel(i.issue), i.stock_quantity != null ? String(i.stock_quantity) : '–']),
    );
    const search = el('input', { type: 'search', placeholder: 'Product name or SKU', value: s.q, 'aria-label': 'Search products' });
    const pform = el('form', { class: 'filters', onsubmit: (e) => { e.preventDefault(); s.q = search.value.trim(); s.page = 1; render('stock'); } }, search, el('button', { class: 'primary', type: 'submit' }, 'Search'));
    const list = table(
      [{ label: 'Product' }, { label: 'SKU' }, { label: 'Price', cls: 'num' }, { label: 'Stock' }],
      products.items.map((p) => [
        el('button', { class: 'linkbtn strong', onclick: () => openProduct(p.id) }, p.name),
        p.sku || '–',
        p.price ? money(p.price, ctx.currency) : '–',
        stockText(p.stock),
      ]),
    );
    return [
      el('h1', {}, 'Stock'), tform,
      issues || empty('No stock problems', `Nothing at or below ${s.threshold} units among ${low.products_scanned} products.`),
      low.truncated && el('p', { class: 'muted small', text: `Scanned ${low.products_scanned} of ${low.total_products} products, so this list may be incomplete.` }),
      el('h2', { class: 'gap' }, 'Products'), pform,
      list || empty('No products found'),
      pager(products.page, products.total_pages, products.total, (p) => { s.page = p; render('stock'); }),
    ];
  }

  function stockText(st) {
    if (!st) return '–';
    if (st.managed_at_parent) return 'Shared with parent';
    if (st.quantity != null) return `${st.quantity} (${st.status === 'instock' ? 'in stock' : st.status === 'outofstock' ? 'out of stock' : st.status === 'onbackorder' ? 'on backorder' : st.status})`;
    return st.status === 'instock' ? 'In stock' : st.status === 'outofstock' ? 'Out of stock' : st.status === 'onbackorder' ? 'On backorder' : '–';
  }

  async function customers() {
    const s = state.customers;
    const data = await api(`/api/customers${qs({ q: s.q, page: s.page })}`);
    const search = el('input', { type: 'search', placeholder: 'Exact email, or a name', value: s.q, 'aria-label': 'Search customers' });
    const form = el('form', { class: 'filters', onsubmit: (e) => { e.preventDefault(); s.q = search.value.trim(); s.page = 1; render('customers'); } },
      search, el('button', { class: 'primary', type: 'submit' }, 'Search'), s.q && el('button', { class: 'ghost', type: 'button', onclick: () => { s.q = ''; s.page = 1; render('customers'); } }, 'Clear'));
    const list = table(
      [{ label: 'Customer' }, { label: 'Email' }, { label: 'Joined' }, { label: 'Orders', cls: 'num' }],
      data.items.map((c) => [el('button', { class: 'linkbtn strong', onclick: () => openCustomer(c.id) }, c.name || `Customer ${c.id}`), c.email || '–', when(c.created_at), c.orders_count != null ? String(c.orders_count) : '–']),
    );
    return [
      el('h1', {}, 'Customers'), form,
      list || empty('No customers found', 'Registered shoppers only. Guest checkouts appear under Orders.'),
      pager(data.page, data.total_pages, data.total, (p) => { s.page = p; render('customers'); }),
    ];
  }

  const VIEWS = { overview, orders, stock, customers };

  function render(name) {
    document.querySelectorAll('.navbtn').forEach((b) => b.setAttribute('aria-current', b.dataset.view === name ? 'page' : 'false'));
    guarded(view, VIEWS[name]);
  }

  function go(name, patch) {
    if (patch) Object.assign(state[name], patch, { page: 1 });
    if (window.location.hash !== `#${name}`) window.location.hash = name; else render(name);
  }

  window.addEventListener('hashchange', () => render(currentView()));
  const currentView = () => (VIEWS[window.location.hash.slice(1)] ? window.location.hash.slice(1) : 'overview');

  // ---------------------------------------------------------------- drawer
  const drawer = $('#drawer');
  const scrim = $('#scrim');
  let lastFocus = null;

  function openDrawer(title, build) {
    lastFocus = document.activeElement;
    $('#drawerTitle').textContent = title;
    drawer.hidden = false;
    scrim.hidden = false;
    document.body.classList.add('noscroll');
    const body = $('#drawerBody');
    const token = ++renderToken;
    body.replaceChildren(loading());
    build().then((content) => { if (!drawer.hidden) body.replaceChildren(...nodes(content)); })
      .catch((e) => { if (!drawer.hidden) body.replaceChildren(notice(e.message)); });
    $('#drawerClose').focus();
    return token;
  }

  function closeDrawer() {
    if (drawer.hidden) return;
    drawer.hidden = true;
    scrim.hidden = true;
    document.body.classList.remove('noscroll');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  $('#drawerClose').addEventListener('click', closeDrawer);
  scrim.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  const kv = (pairs) => el('dl', { class: 'kv' }, pairs.filter((p) => p[1] != null && p[1] !== '').map((p) => [el('dt', { text: p[0] }), el('dd', {}, p[1])]));
  const addressText = (a) => (a ? [[a.first_name, a.last_name].filter(Boolean).join(' '), a.city, a.state, a.postcode, a.country].filter(Boolean).join(', ') : '');

  function openOrder(id) {
    openDrawer(`Order #${id}`, async () => {
      const [o, tr] = await Promise.all([api(`/api/orders/${id}`), api(`/api/orders/${id}/tracking`).catch(() => null)]);
      const cur = o.currency || ctx.currency;
      const items = table(
        [{ label: 'Item' }, { label: 'Qty', cls: 'num' }, { label: 'Price', cls: 'num' }, { label: 'Total', cls: 'num' }],
        (o.line_items || []).map((li) => [el('span', {}, li.name, li.variation && el('span', { class: 'sub', text: li.variation }), li.sku && el('span', { class: 'sub', text: `SKU ${li.sku}` })), String(li.quantity ?? ''), money(li.unit_price, cur), money(li.total, cur)]),
      );
      const notes = (o.notes || []).map((n) => el('li', { class: 'note' },
        el('p', { class: 'notehead' }, el('span', { class: n.visible_to_customer ? 'tag tag-customer' : 'tag tag-staff', text: n.visible_to_customer ? 'Visible to customer' : 'Staff only' }), el('span', { class: 'muted small', text: `${n.added_by === 'system' ? 'System' : 'Staff'} · ${when(n.created_at)}` })),
        el('p', { class: 'quote', text: n.note_text || '' })));
      const shipment = tr && tr.shipments && tr.shipments.length
        ? tr.shipments.map((s) => el('p', {}, [s.carrier, s.tracking_number].filter(Boolean).join(' · '), s.tracking_url && [' ', adminLink(s.tracking_url, 'Track')], s.shipped_date && el('span', { class: 'sub', text: `Shipped ${s.shipped_date}` })))
        : el('p', { class: 'muted', text: 'No tracking recorded by the store.' });
      return [
        el('p', {}, chip(o.status), ' ', el('span', { class: 'muted', text: when(o.created_at) })),
        kv([['Customer', [addressText(o.billing)].join('')], ['Email', o.billing && o.billing.email], ['Phone', o.billing && o.billing.phone], ['Ship to', addressText(o.shipping)], ['Payment', o.payment_method]]),
        o.customer_note_text ? el('div', { class: 'block' }, el('h3', {}, 'Customer note'), el('p', { class: 'quote', text: o.customer_note_text })) : null,
        el('div', { class: 'block' }, el('h3', {}, 'Items'), items || empty('No items')),
        kv([['Discount', o.discount_total && money(o.discount_total, cur)], ['Shipping', o.shipping_total && money(o.shipping_total, cur)], ['Tax', o.tax_total && money(o.tax_total, cur)], ['Total', el('strong', { text: money(o.total, cur) })]]),
        (o.refunds || []).length ? el('div', { class: 'block' }, el('h3', {}, 'Refunds'), el('ul', { class: 'plain' }, o.refunds.map((r) => el('li', {}, money(r.amount, cur), r.reason_text && el('span', { class: 'sub', text: r.reason_text }))))) : null,
        el('div', { class: 'block' }, el('h3', {}, 'Shipping'), shipment),
        el('div', { class: 'block' }, el('h3', {}, 'Notes'), notes.length ? el('ul', { class: 'plain notes' }, notes) : el('p', { class: 'muted', text: 'No notes.' })),
        o.admin_url ? adminLink(o.admin_url, 'Open in WooCommerce to make changes') : null,
      ];
    });
  }

  function openProduct(id) {
    openDrawer('Product', async () => {
      const p = await api(`/api/products/${id}`);
      $('#drawerTitle').textContent = p.name;
      const vars = table(
        [{ label: 'Variation' }, { label: 'SKU' }, { label: 'Price', cls: 'num' }, { label: 'Stock' }],
        (p.variations || []).map((v) => [v.attributes || `#${v.id}`, v.sku || '–', v.price ? money(v.price, ctx.currency) : '–', stockText(v.stock)]),
      );
      return [
        kv([['SKU', p.sku], ['Type', p.type], ['Status', p.status], ['Price', p.price && money(p.price, ctx.currency)], ['Stock', stockText(p.stock)], ['Categories', (p.categories || []).join(', ')]]),
        p.description_text ? el('div', { class: 'block' }, el('h3', {}, 'Description'), el('p', { class: 'quote', text: p.description_text })) : null,
        vars ? el('div', { class: 'block' }, el('h3', {}, 'Variations'), vars, p.variations_truncated && el('p', { class: 'muted small', text: 'Only the first 100 variations are shown.' })) : null,
        p.admin_url ? adminLink(p.admin_url, 'Open in WooCommerce') : null,
      ];
    });
  }

  function openCustomer(id) {
    openDrawer('Customer', async () => {
      const c = await api(`/api/customers/${id}`);
      $('#drawerTitle').textContent = c.name || `Customer ${id}`;
      return [
        kv([['Email', c.email], ['Phone', c.phone], ['Joined', when(c.created_at)], ['Orders', String(c.order_count)], ['Billing', addressText(c.billing)]]),
        el('div', { class: 'block' }, el('h3', {}, 'Recent orders'), orderTable(c.recent_orders || []) || empty('No orders yet')),
      ];
    });
  }

  // ---------------------------------------------------------------- chat
  const log = $('#log');
  const form = $('#askForm');
  const input = $('#askInput');
  const send = $('#askSend');
  const TOOL_WORDS = {
    get_store_info: 'store details', get_store_overview: 'the overview', list_orders: 'orders', search_orders: 'order search', get_order: 'an order', get_order_tracking: 'tracking',
    list_products: 'products', search_products: 'product search', get_product: 'a product', list_low_stock: 'stock levels', list_product_categories: 'categories',
    list_customers: 'customers', search_customers: 'customer search', get_customer: 'a customer',
  };

  // Models answer in light markdown. Render only **bold** and [text](http(s) url) as DOM nodes; everything else stays literal text.
  const RICH = /\*\*(.+?)\*\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  function rich(text) {
    const out = [];
    let last = 0;
    for (const m of text.matchAll(RICH)) {
      if (m.index > last) out.push(text.slice(last, m.index));
      out.push(m[1] !== undefined ? el('strong', { text: m[1] }) : el('a', { class: 'ext', href: m[3], target: '_blank', rel: 'noopener noreferrer', text: m[2] }));
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function addMessage(kind, text, tools) {
    const node = el('div', { class: `msg msg-${kind}` }, el('p', { class: 'msgtext' }, kind === 'assistant' ? rich(text) : text));
    if (tools && tools.length) node.append(el('p', { class: 'muted small', text: `Looked at: ${tools.map((t) => TOOL_WORDS[t] || t).join(', ')}` }));
    log.append(node);
    log.scrollTop = log.scrollHeight;
    return node;
  }

  async function ask(text) {
    addMessage('user', text);
    const waiting = addMessage('pending', 'Looking this up…');
    send.disabled = true;
    try {
      const reply = await api('/api/chat', { method: 'POST', body: JSON.stringify({ message: text }) });
      waiting.remove();
      addMessage('assistant', reply.answer || 'No answer came back. Try rephrasing the question.', reply.tools);
    } catch (error) {
      waiting.remove();
      addMessage('error', error.message);
    } finally {
      send.disabled = !ctx.config.chat_enabled;
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || !ctx.config.chat_enabled) return;
    input.value = '';
    ask(text);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });

  function setupChat() {
    const chips = $('#chips');
    if (!ctx.config.chat_enabled) {
      input.disabled = true;
      send.disabled = true;
      log.append(el('div', { class: 'msg msg-error' }, el('p', { class: 'msgtext', text: 'The assistant is off. Set OPENROUTER_API_KEY and restart the web server to turn it on. The dashboard works without it.' })));
      return;
    }
    log.append(el('div', { class: 'msg msg-assistant' }, el('p', { class: 'msgtext', text: 'Ask about orders, tracking, stock or customers. I only read your store and cannot change anything. Customer details are masked, and I can be wrong, so check the order before acting.' })));
    for (const q of ['What needs attention right now?', 'Which products are running low?', 'Show me the latest on-hold orders']) {
      chips.append(el('button', { class: 'chipbtn', type: 'button', onclick: () => { document.body.classList.add('ask-open'); ask(q); chips.hidden = true; } }, q));
    }
  }

  const openAsk = () => { document.body.classList.add('ask-open'); input.focus(); };
  const closeAsk = () => document.body.classList.remove('ask-open');
  $('#askOpen').addEventListener('click', openAsk);
  $('#askClose').addEventListener('click', closeAsk);

  // ---------------------------------------------------------------- boot
  document.querySelectorAll('.navbtn').forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  $('#logout').addEventListener('click', async () => {
    try { await api('/api/logout', { method: 'POST' }); } finally { window.location.assign('/login'); }
  });

  async function boot() {
    try {
      const [info, config] = await Promise.all([api('/api/info'), api('/api/config')]);
      ctx.currency = info.currency || '';
      ctx.config = config;
      $('#storeName').textContent = info.store_name || 'Store desk';
      $('#storeMeta').textContent = [info.currency, info.country].filter(Boolean).join(' · ');
      $('#privacy').textContent = info.pii_mode === 'masked' ? 'Customer emails and phones are masked. Street addresses are hidden.' : 'Full customer details are shown.';
      const banner = $('#banner');
      for (const w of info.warnings || []) banner.append(el('p', { class: 'notice warn', text: w }));
    } catch (error) {
      $('#banner').append(notice(`Could not load the store: ${error.message}`, () => window.location.reload()));
    }
    setupChat();
    render(currentView());
  }
  boot();
})();
