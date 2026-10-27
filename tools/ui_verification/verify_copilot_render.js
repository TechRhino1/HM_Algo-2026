/* ===========================================================================
   Copilot answer renderer, for both front ends (tools/verify_copilot_render.js)

   WHY THIS EXISTS
   ---------------
   There are two copilot chat surfaces — the dashboard (`dashboard.js`) and the
   classic terminal (`terminal.js`) — and each carries its own copy of the
   trimmed-down markdown renderer. A copy that drifts is how this went wrong
   twice:

     * `dashboard.js` skipped its inline pass on bullet lines, and nearly every
       bullet the copilot writes wraps its label in **bold** ("- **Current Bias**:
       …"), so most of a typical answer reached the reader as literal asterisks.
     * `terminal.js` interpolated the answer straight into `innerHTML` with no
       escaping at all, while the dashboard escaped first — so a broker or
       journal string containing markup was injected into the page.

   Neither was visible to any other tool: the HTTP layer returns the same body
   either way, and the render harness only loads `dashboard.js`.

   WHAT IT CHECKS
   --------------
   It extracts each file's `escapeHtml` / `copilotInline` / `copilotHtml` by
   name, EVALUATES them, and asserts:
     1. hostile input is escaped in both — no raw tag survives;
     2. bullets and bold render in both, and no `**` reaches the reader;
     3. the two implementations produce byte-identical output, so a future edit
        to one of them fails here rather than shipping a difference;
     4. the terminal's bubble call site passes the answer through `copilotHtml`
        rather than interpolating it raw (a function-level test cannot see that).

   Run:  node tools/verify_copilot_render.js
   Exits non-zero if any check fails. No server, no browser.
   =========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const DASHBOARD = path.join(ROOT, 'jarvis/ui/static/js/dashboard.js');
const TERMINAL = path.join(ROOT, 'jarvis/ui/static/js/terminal.js');

const HELPERS = ['escapeHtml', 'copilotInline', 'copilotHtml'];

let checks = 0;
let failures = 0;
function ok(label, condition, detail) {
  checks++;
  if (condition) {
    console.log('  PASS  ' + label);
  } else {
    failures++;
    console.log('  FAIL  ' + label + (detail ? '  -> ' + detail : ''));
  }
}

/* Pull `function <name>(…) { … }` out of a file by matching braces. Safe here
   because none of these three bodies contains a brace inside a string, template
   literal or regex. */
function extractFunction(src, name) {
  const at = src.indexOf('function ' + name + '(');
  if (at < 0) return null;
  const open = src.indexOf('{', at);
  if (open < 0) return null;
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(at, i + 1);
    }
  }
  return null;
}

function loadHelpers(file, label, escName) {
  const src = fs.readFileSync(file, 'utf8');
  const names = [escName || 'escapeHtml', 'copilotInline', 'copilotHtml'];
  const bodies = [];
  const missing = [];
  names.forEach((n) => {
    const body = extractFunction(src, n);
    if (body) bodies.push(body); else missing.push(n);
  });
  if (missing.length) {
    throw new Error(label + ' is missing: ' + missing.join(', '));
  }
  /* The dashboard's escape helper is called `esc`, the terminal's `escapeHtml`.
     Bridge the name rather than forcing both files to agree on it. */
  const bridge = (escName && escName !== 'escapeHtml')
    ? 'function escapeHtml(v) { return ' + escName + '(v); }'
    : '';
  const fn = new Function(
    bodies.join('\n') + '\n' + bridge +
    '\nreturn { escapeHtml: escapeHtml, copilotInline: copilotInline, copilotHtml: copilotHtml };'
  );
  return { api: fn(), src };
}

console.log('copilot answer renderer\n');

const dash = loadHelpers(DASHBOARD, 'dashboard.js', 'esc');
const term = loadHelpers(TERMINAL, 'terminal.js', 'escapeHtml');

/* The dashboard escapes with `esc`; make the terminal's helper resolve too. */
function render(which, text) {
  const api = which === 'dash' ? dash.api : term.api;
  return api.copilotHtml(text);
}

const HOSTILE = '<img src=x onerror=alert(1)> & "quoted"';
const ANSWER = [
  '**Open positions — 1**',
  '- **Current Bias**: BULL (TREND_FOLLOW)',
  '- Balance **1,234.56** · Equity **1,240.00**',
  'Ask me about a symbol with *emphasis*.'
].join('\n');

console.log('escaping');
['dash', 'term'].forEach((which) => {
  const name = which === 'dash' ? 'dashboard.js' : 'terminal.js';
  const out = render(which, HOSTILE);
  ok(name + ': a tag in the text is escaped, not injected',
    out.indexOf('<img') < 0 && out.indexOf('&lt;img') >= 0, out.slice(0, 160));
  ok(name + ': quotes and ampersands are escaped',
    out.indexOf('&quot;') >= 0 && out.indexOf('&amp;') >= 0, out.slice(0, 160));
  ok(name + ': the escaped text keeps its words',
    out.indexOf('onerror=alert(1)') >= 0, out.slice(0, 160));
});

console.log('\nmarkdown');
['dash', 'term'].forEach((which) => {
  const name = which === 'dash' ? 'dashboard.js' : 'terminal.js';
  const out = render(which, ANSWER);
  ok(name + ': bold inside a bullet is rendered',
    out.indexOf('<b>Current Bias</b>') >= 0 && out.indexOf('<b>1,234.56</b>') >= 0,
    out.slice(0, 200));
  ok(name + ': no raw asterisks reach the reader',
    out.indexOf('**') < 0, out.slice(0, 200));
  ok(name + ': the bullets form one list of two items',
    (out.match(/<ul>/g) || []).length === 1 && (out.match(/<li>/g) || []).length === 2,
    out.slice(0, 200));
  ok(name + ': a bold line outside a list is still bold',
    out.indexOf('<b>Open positions') >= 0, out.slice(0, 200));
  ok(name + ': single asterisks are emphasised',
    out.indexOf('<i>emphasis</i>') >= 0, out.slice(0, 200));
});

console.log('\nthe two renderers agree');
/* The point of the tool: two copies that must not diverge. */
const CASES = [HOSTILE, ANSWER, '', 'plain', 'a\n\nb', '- one\n- two',
               '**b** and *i*', '<b>already bold</b>', '- <i>tag in a bullet</i>'];
const diffs = CASES.filter((c) => render('dash', c) !== render('term', c));
ok('every case renders identically in both front ends',
  diffs.length === 0,
  diffs.length ? JSON.stringify(diffs.map((c) => ({
    input: c, dashboard: render('dash', c), terminal: render('term', c)
  }))) : '');

console.log('\ncall sites');
/* A function-level test cannot see whether the bubble actually calls it. Scope
   this to the copilot's own `bubble(...)` calls: `${data.error}` also appears in
   `alert(...)` and `textContent` elsewhere in the file, and neither of those
   parses HTML, so a whole-file search reports a fault that is not there. */
const bubbleCalls = term.src.match(/bubble\(`[^`]*`/g) || [];
ok('the terminal has copilot bubbles to check', bubbleCalls.length >= 3,
  'found ' + bubbleCalls.length);
ok('every copilot bubble escapes its dynamic parts',
  bubbleCalls.every((c) => c.indexOf('${data.') < 0 && c.indexOf('${err.') < 0) &&
  bubbleCalls.every((c) => c.indexOf('escapeHtml(') >= 0 || c.indexOf('copilotHtml(') >= 0),
  JSON.stringify(bubbleCalls.filter((c) =>
    (c.indexOf('${data.') >= 0 || c.indexOf('${err.') >= 0) ||
    (c.indexOf('escapeHtml(') < 0 && c.indexOf('copilotHtml(') < 0))));
ok('the terminal renders the answer through copilotHtml',
  term.src.indexOf('copilotHtml(data.response') >= 0 &&
  term.src.indexOf('${data.response') < 0,
  'raw interpolation of data.response still present');
ok('the terminal escapes the refusal reason',
  term.src.indexOf('escapeHtml(data.error') >= 0, 'data.error is not escaped');
ok('the terminal escapes a transport error',
  term.src.indexOf('escapeHtml(err.message') >= 0,
  'err.message is interpolated raw');
ok('the dashboard renders the answer through copilotHtml',
  dash.src.indexOf('copilotHtml(text ||') >= 0, '');

console.log('\n' + (failures === 0
  ? 'all ' + checks + ' checks passed'
  : failures + ' of ' + checks + ' checks FAILED'));
process.exit(failures === 0 ? 0 : 1);
