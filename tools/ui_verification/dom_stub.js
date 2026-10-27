/* ===========================================================================
   Shared stubbed DOM for the headless render harnesses (tools/dom_stub.js)

   WHY THIS IS SHARED
   ------------------
   Two front ends are driven headlessly in Node: the dashboard
   (tools/verify_dashboard_render.js) and the classic terminal
   (tools/verify_terminal_render.js). Both need the same things — a real
   element tree, an innerHTML setter that materialises the fragment, a small
   selector engine, and a getElementById that returns null for an id the
   template does not define.

   A copy of this file in each harness would drift, and the drift would be
   invisible: a harness whose stub is subtly weaker still prints PASS. That is
   the same failure mode as a duplicated production renderer, so the stub lives
   in one place.

   It sits at tools/dom_stub.js rather than tools/lib/ on purpose: .gitignore
   carries a bare `lib/` rule, which silently ignores tools/lib/ — the file
   would work locally and be absent from every clone, and the dashboard harness
   would fail to require it.

   FIDELITY RULES (each one earned by a wrong answer)
   --------------------------------------------------
   * `getElementById` returns **null** for an id the template does not define.
     A permissive stub that always returns an element makes a controller that
     queries a non-existent id look correct.
   * A form control's `value` is always a string — an untouched input reads
     `''`, never `undefined`.
   * A `<select>` always exposes `.options`, derived from the tree so that
     appending an `<option>` is observable.
   * `innerHTML` keeps the raw string *and* materialises the fragment, so both
     `deepHtml()` (content assertions) and tree queries work.
   * `appendChild` moves a DocumentFragment's children rather than inserting
     the fragment itself.

   Usage:
     const { createDom } = require('./dom_stub');
     const dom = createDom({ templateIds, docRoots: [...], templateTree: {...} });
     dom.linkTree();
     // then run the module with dom.documentStub as `document`
   =========================================================================== */
'use strict';

/* A very small selector engine: compound selectors (tag / .class / #id /
   [attr] / [attr="v"]) optionally joined by descendant spaces. That is the
   whole subset the controllers use, and a genuine tree walk is required to
   verify in-place ticks, which update cells by querying them rather than by
   rebuilding the table. */
function matchesSimple(el, sel) {
  let s = sel;
  const idM = /#([\w-]+)/.exec(s);
  if (idM && el.id !== idM[1]) return false;
  s = s.replace(/#[\w-]+/g, '');

  const classes = (s.match(/\.[\w-]+/g) || []).map((c) => c.slice(1));
  s = s.replace(/\.[\w-]+/g, '');

  const attrs = [];
  const attrRe = /\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = attrRe.exec(s)) !== null) attrs.push([m[1], m[2]]);
  s = s.replace(/\[[^\]]*\]/g, '');

  const tag = s.trim();
  if (tag && el.tagName !== tag.toUpperCase()) return false;

  for (const c of classes) {
    if (!el.classList.contains(c) && String(el.className || '').split(/\s+/).indexOf(c) < 0) return false;
  }
  for (const a of attrs) {
    const v = el.getAttribute(a[0]);
    if (v === null) return false;
    if (a[1] !== undefined && v !== a[1]) return false;
  }
  return true;
}

function descendantsOf(root) {
  const out = [];
  (function walk(node) {
    const kids = node.children || [];
    for (let i = 0; i < kids.length; i++) { out.push(kids[i]); walk(kids[i]); }
  })(root);
  return out;
}

function selectAll(root, selector) {
  const parts = String(selector).trim().split(/\s+/).filter(Boolean);
  let scope = [root];
  for (const part of parts) {
    const next = [];
    for (const node of scope) {
      for (const el of descendantsOf(node)) {
        if (matchesSimple(el, part) && next.indexOf(el) < 0) next.push(el);
      }
    }
    scope = next;
  }
  return scope;
}

function decodeEntities(s) {
  return String(s)
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

const VOID_TAGS = new Set(['BR', 'HR', 'IMG', 'INPUT', 'META', 'LINK']);

function createDom(options) {
  const opts = options || {};
  const templateIds = opts.templateIds || new Set();
  const docRoots = opts.docRoots || [];
  const templateTree = opts.templateTree || {};

  const requestedIds = [];
  const prelinked = new Set();
  const registry = new Map();

  /* Parse a well-formed HTML fragment into real nodes.

     The controllers write table rows with innerHTML. Without this the harness
     would hold them as an opaque string, which hides every in-place update —
     including per-second countdown ticks, which find their cells by querying
     the tree rather than by rebuilding the table.

     Handles only the subset the controllers emit: quoted attributes, no
     comments, no CDATA, and void elements only from the set above. */
  function parseHtml(fragment) {
    const root = new El('', 'div');
    const stack = [root];
    const tagRe = /<\/?([a-zA-Z][\w-]*)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*\/?>/g;
    let last = 0;
    let m;

    const textInto = (txt) => {
      if (!txt) return;
      const top = stack[stack.length - 1];
      top._text += decodeEntities(txt);
    };

    while ((m = tagRe.exec(fragment)) !== null) {
      textInto(fragment.slice(last, m.index));
      last = tagRe.lastIndex;

      const raw = m[0];
      if (raw.charAt(1) === '/') {
        if (stack.length > 1) stack.pop();
        continue;
      }

      const el = new El('', m[1].toUpperCase());
      const attrRe = /([\w-]+)(?:="([^"]*)")?/g;
      let a;
      while ((a = attrRe.exec(m[2] || '')) !== null) {
        if (!a[1]) continue;
        el.setAttribute(a[1], a[2] === undefined ? '' : decodeEntities(a[2]));
      }

      stack[stack.length - 1].appendChild(el);
      if (!VOID_TAGS.has(el.tagName) && !/\/>$/.test(raw)) stack.push(el);
    }
    textInto(fragment.slice(last));
    return root;
  }

  class El {
    constructor(id, tag) {
      this.id = id || '';
      this.tagName = (tag || 'DIV').toUpperCase();
      this._text = '';
      this._html = '';
      this.className = '';
      this.hidden = false;
      /* In a real DOM every form control's `value` is a string — an untouched
         input reads `''`, never `undefined`. Leaving it undefined made a
         controller's `slEl.value !== ''` guard pass on an empty field and send
         `Number(undefined)` (NaN, serialised as null) for a level the user
         never typed. A stub default that disagrees with the DOM invents a bug. */
      this.value = '';
      this.style = {};
      this.attrs = {};
      this.children = [];
      this.parentNode = null;
      this.dataset = {};
      this.offsetWidth = 100;
      this.clientWidth = 800;
      this.clientHeight = 360;
      this.classList = {
        _s: new Set(),
        add: (...c) => c.forEach((x) => this.classList._s.add(x)),
        remove: (...c) => c.forEach((x) => this.classList._s.delete(x)),
        toggle: (c, on) => (on ? this.classList._s.add(c) : this.classList._s.delete(c)),
        contains: (c) => this.classList._s.has(c)
      };
    }
    get textContent() { return this._text; }
    set textContent(v) { this._text = v === null || v === undefined ? '' : String(v); }
    /* `innerText` is a real property of a rendered element, and the classic
       terminal writes the history count through it. Without the alias the
       assignment quietly becomes a plain property, `deepHtml()` never sees the
       text, and the count reads as blank — a stub gap that looks exactly like a
       rendering bug. Aliased to `_text` (the layout-aware difference between
       innerText and textContent is not something these assertions depend on). */
    get innerText() { return this._text; }
    set innerText(v) { this.textContent = v; }
    /* A real <select> always exposes `.options`, so a controller that reads
       `sel.options.length` is not doing anything unusual — it is the documented
       way to test whether a select has been populated. Without this the
       backtest form's guard (`options.length === 0`, i.e. "only populate once")
       threw a TypeError in the harness and took the whole run down, which would
       have looked like a crash in the dashboard rather than a gap in the stub.
       Derived from the tree so appending an <option> is actually observable. */
    get options() { return (this.children || []).filter((c) => c.tagName === 'OPTION'); }
    get innerHTML() { return this._html; }
    set innerHTML(v) {
      this._html = v === null || v === undefined ? '' : String(v);
      this.children = [];
      // Materialise the fragment so the tree can be queried. `_html` keeps the
      // raw string, so deepHtml() still works for the panels that are asserted
      // on by content.
      if (this._html && this._html.indexOf('<') >= 0) {
        const parsed = parseHtml(this._html);
        parsed.children.forEach((c) => this.appendChild(c));
      }
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
    removeAttribute(k) { delete this.attrs[k]; }
    addEventListener(type, fn) {
      this._listeners = this._listeners || {};
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    }
    removeEventListener() {}
    /* Drive a bound handler from the harness. Without this the controller's
       click and change bindings are unreachable, and the only way to exercise a
       view would be to call its renderer directly — which would prove nothing
       about whether the wiring works. */
    fire(type, ev) {
      const ls = (this._listeners || {})[type] || [];
      ls.forEach((fn) => fn(ev || {}));
      return ls.length;
    }
    appendChild(c) {
      // A real DOM moves a DocumentFragment's children into the target rather
      // than inserting the fragment itself. Emulate that, or a panel built with
      // a fragment looks like it holds one child.
      if (c && c.tagName === 'FRAGMENT') {
        const kids = c.children || [];
        for (let i = 0; i < kids.length; i++) { kids[i].parentNode = this; this.children.push(kids[i]); }
        c.children = [];
        return c;
      }
      c.parentNode = this;
      this.children.push(c);
      return c;
    }
    removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
    insertBefore(c) { c.parentNode = this; this.children.push(c); return c; }
    querySelectorAll(sel) { return selectAll(this, sel); }
    querySelector(sel) { return selectAll(this, sel)[0] || null; }
    contains() { return true; }
    remove() {}
    closest() { return null; }
    focus() {}
  }

  function elementFor(id) {
    requestedIds.push(id);
    if (!templateIds.has(id)) return null;   // mirrors a real getElementById miss
    if (!registry.has(id)) registry.set(id, new El(id));
    return registry.get(id);
  }

  /* A controller may query *within* a view section — walking a container to
     update cells in place. A flat registry has no ancestry, so the panels that
     live inside each view are declared by the caller and linked before the
     module runs. Ids created this way are recorded as pre-linked so they do not
     make the "every queried id exists" check pass by construction. */
  function linkTree() {
    Object.keys(templateTree).forEach((parentId) => {
      const parent = elementFor(parentId);
      if (!parent) return;
      prelinked.add(parentId);
      templateTree[parentId].forEach((childId) => {
        const child = elementFor(childId);
        if (!child) return;
        prelinked.add(childId);
        if (!child.parentNode) parent.appendChild(child);
      });
    });
    docRoots.forEach((id) => {
      const el = elementFor(id);
      if (!el) return;
      prelinked.add(id);
      if (!el.parentNode) documentStub.appendChild(el);
    });
  }

  const documentListeners = {};
  function fireDocument(type, ev) {
    const ls = documentListeners[type] || [];
    ls.forEach((fn) => fn(ev || {}));
    return ls.length;
  }

  /* Serialise an element and everything beneath it.

     Two traps this avoids. Panels built with createElement + appendChild leave
     innerHTML empty on the container, so reading innerHTML alone reports a
     blank panel. And panels built with textContent leave nothing in innerHTML
     at all, so the text has to be included too. */
  function deepHtml(el) {
    if (!el) return '';
    let out = (el.innerHTML || '') + (el._text || '');
    const kids = el.children || [];
    for (let i = 0; i < kids.length; i++) out += deepHtml(kids[i]);
    return out;
  }

  const documentStub = {
    readyState: 'complete',
    body: new El('body', 'body'),
    documentElement: new El('html', 'html'),
    head: new El('head', 'head'),
    children: [],
    /* The classic terminal guards input writes with
       `document.activeElement !== inputEl`, so the property has to exist or
       every one of those guards takes the "not focused" branch by accident. */
    activeElement: null,
    appendChild(c) { c.parentNode = documentStub; documentStub.children.push(c); return c; },
    getElementById: elementFor,
    createElement: (tag) => new El('', tag),
    createDocumentFragment: () => new El('', 'fragment'),
    querySelectorAll: (sel) => selectAll(documentStub, sel),
    querySelector: (sel) => selectAll(documentStub, sel)[0] || null,
    addEventListener: (type, fn) => {
      (documentListeners[type] = documentListeners[type] || []).push(fn);
    },
    hidden: false
  };

  return {
    El,
    documentStub,
    registry,
    requestedIds,
    prelinked,
    elementFor,
    linkTree,
    fireDocument,
    documentListeners,
    deepHtml,
    parseHtml,
    selectAll,
    matchesSimple,
    decodeEntities
  };
}

module.exports = { createDom };
