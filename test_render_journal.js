/* Run the dashboard's REAL renderJournal() against the REAL /notes payload
   pulled from the server, with a stub DOM. Checks the HTML it produces rather
   than trusting that it looks right.

   The functions are sliced out of bot_server.py by name, so this breaks if
   they are renamed — which is the point. */
const fs = require('fs');

const SRC = 'C:\\Users\\jhanp\\New folder\\CryptoTrader\\bot_server.py';
const src = fs.readFileSync(SRC, 'utf8');
const js = src.match(/<script>([\s\S]*?)<\/script>/)[1];

// Pull whole top-level function bodies by brace matching from `function NAME(`.
function grab(name) {
  const i = js.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('not found in dashboard: ' + name);
  let d = 0, started = false;
  for (let j = i; j < js.length; j++) {
    if (js[j] === '{') { d++; started = true; }
    else if (js[j] === '}') { d--; if (started && d === 0) return js.slice(i, j + 1); }
  }
  throw new Error('unbalanced braces in ' + name);
}

const NAMES = ['renderJournal', '_keyToMeta', '_autoNote', '_noteGetEntry',
               '_noteKeys', '_renderJournalCalendar', '_tradeNoteKey'];
const code = NAMES.map(grab).join('\n');

// Save a live payload first:  curl http://10.0.0.88:8081/notes -o notes.json
const NOTES = process.env.NOTES_JSON || 'notes.json';
if (!fs.existsSync(NOTES)) {
  console.log(`no ${NOTES} — fetch one first:\n  curl http://10.0.0.88:8081/notes -o notes.json`);
  process.exit(2);
}
const notes = JSON.parse(fs.readFileSync(NOTES, 'utf8')).notes;

let fails = [];
const el = { innerHTML: '' };
const sandbox = {
  _autoNotes: notes,
  _journalFilter: 'all',
  _localNotes: {},
  $: id => (id === 'jnl_notes_list' ? el : null),
  localStorage: {
    getItem: () => JSON.stringify(sandbox._localNotes),
    setItem: () => {},
  },
  document: { querySelectorAll: () => [] },
};

const vm = require('vm');
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

function render(filter, local) {
  sandbox._journalFilter = filter;
  sandbox._localNotes = local || {};
  el.innerHTML = '';
  vm.runInContext('renderJournal()', sandbox);
  return el.innerHTML;
}

console.log('='.repeat(72));
console.log('  RENDER — real /notes payload through the real renderJournal()');
console.log('='.repeat(72));

const html = render('all');
const cards = (html.match(/jnl-note-card/g) || []).length;
const badges = (html.match(/jnl-bot-badge/g) || []).length;
const autoBlocks = (html.match(/jnl-note-auto/g) || []).length;
console.log(`  ${Object.keys(notes).length} notes -> ${cards} cards, ${badges} bot badges, ${autoBlocks} bot bodies`);
if (cards !== Object.keys(notes).length) fails.push(`rendered ${cards} cards for ${Object.keys(notes).length} notes`);
if (badges !== cards) fails.push(`${badges} badges for ${cards} cards — every card here is a bot note`);

// Cards must be newest first.
const dates = [...html.matchAll(/data-key="(\d+)\|/g)].map(m => +m[1]);
const sorted = [...dates].sort((a, b) => b - a);
if (JSON.stringify(dates) !== JSON.stringify(sorted)) fails.push('cards are not newest-first');
else console.log('  order: newest first (ok)');

// No literal "undefined"/"NaN" leaking into the page.
for (const bad of ['undefined', 'NaN', '[object Object]']) {
  if (html.includes(bad)) fails.push(`rendered HTML contains "${bad}"`);
}
if (!fails.some(f => f.includes('contains'))) console.log('  no undefined/NaN/[object Object] in output (ok)');

// Dates must be this era, not year 57000 — the ms-vs-seconds bug.
const years = [...html.matchAll(/jnl-note-date">([^<]+)</g)].map(m => m[1]);
console.log(`  sample dates: ${[...new Set(years)].slice(0, 5).join(', ')}`);
if (years.some(y => /\d{5,}/.test(y))) fails.push('a rendered date has a 5+ digit year (ms treated as seconds)');
else console.log('  dates are plausible (ok)');

// Tag filter must actually narrow.
const fomo = render('FOMO');
const nF = (fomo.match(/jnl-note-card/g) || []).length;
console.log(`  filter 'FOMO' -> ${nF} cards (of ${cards})`);
if (nF >= cards) fails.push('FOMO filter did not narrow the list');
if (nF === 0) fails.push('FOMO filter returned nothing — the bot does tag FOMO');

// A user note on the same key must show alongside the bot's, not replace it.
const k = Object.keys(notes)[0];
const both = render('all', { [k]: { text: 'MY OWN TAKE', tags: [] } });
const at = both.indexOf(`data-key="${k}"`);
// The next card may not exist — k can be the last one — so clamp to end.
const nxt = both.indexOf('jnl-note-card', at + 10);
const seg = both.slice(at, nxt === -1 ? both.length : nxt);
if (process.env.DEBUG_CARD) console.log('\n--- card ---\n' + seg + '\n--- end ---\n');
const hasMine = seg.includes('MY OWN TAKE');
const hasBot = seg.includes('jnl-note-auto');
console.log(`  user note + bot note on one card: mine=${hasMine} bot=${hasBot}`);
if (!hasMine) fails.push('user note did not render');
if (!hasBot) fails.push('user note REPLACED the bot note instead of joining it');

// A user note on a key the bot never wrote must still appear.
const only = render('all', { '9999999999999|FOOUSD': { text: 'orphan note', tags: [] } });
if (!only.includes('orphan note')) fails.push('a user-only note vanished from the journal');
else console.log('  user-only note (no bot note) still renders (ok)');

// Empty state.
sandbox._autoNotes = {};
const empty = render('all', {});
if (!empty.includes('No notes yet')) fails.push('empty state missing');
else console.log('  empty state renders (ok)');
sandbox._autoNotes = notes;

console.log();
console.log('='.repeat(72));
if (fails.length) {
  console.log(`  ${fails.length} FAILURE(S)`);
  fails.forEach(f => console.log('   x ' + f));
  process.exit(1);
}
console.log('  all checks passed');
