import {connectHost, HostRequestError} from '@openchamber/sdk';
import {applyHostReady, mountBadge, mountBanner, mountButton, mountEmpty, mountProgress, mountSpinner, type Tone} from '@openchamber/sdk/ui';

type Window = {id: string; label: string; remaining_percent: number | null; unit: string | null; resets_at: string | null; stale_reason: string | null; per_model: boolean};
type Reading = {remaining: number | null; unit: string | null; reason: string | null; observed_at: string | null; credits: {text?: string} | null; windows: Window[]};
type Source = {id: string; slug: string; name: string; account: string; provider: string; adapter: string; kind: string; status: string; needs_login: boolean; burn_rate: number | null; latest: Reading | null};
type Status = {generated_at: string; sources: Source[]; holds: Record<string, string>; claude_auth: {logged_in: boolean | null} | null};

const VISIBLE_POLL_MS = 45_000;
// Hidden, the panel only keeps the rail badge current, from the passive snapshot.
const HIDDEN_POLL_MS = 300_000;

const host = connectHost();
const node = (id: string) => document.getElementById(id)!;
let status: Status | null = null, visible = true, timer = 0, badge: number | null = null;
type Notice = {tone: 'info' | 'warning' | 'error'; title: string; body?: string};
// problem comes from the last load; message is a short-lived answer to a button.
let problem: Notice | null = null, message: Notice | null = null, messageTimer = 0;
const busy = new Set<string>();

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function until(iso: string | null) {
  if (!iso) return '';
  const minutes = Math.floor((new Date(iso).getTime() - Date.now()) / 60_000);
  if (minutes <= 0) return 'now';
  const d = Math.floor(minutes / 1440), h = Math.floor((minutes % 1440) / 60), m = minutes % 60;
  return 'in ' + (d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`);
}

// Tarmac writes reset times into hold reasons as UTC ISO; show them as local time.
const localTimes = (text: string) => text.replace(/\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)/g,
  iso => new Date(iso).toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'}));

function tone(percent: number): Tone {
  return percent < 10 ? 'error' : percent < 30 ? 'warning' : 'success';
}

function windowRow(w: Window) {
  const box = el('div', 'win'), row = el('div', 'row'), value = el('span', 'value');
  row.append(el('span', '', w.label), value);
  box.append(row);
  const amount = w.remaining_percent;
  if (amount == null) {
    value.textContent = w.stale_reason || 'unknown';
    if (!w.unit || w.unit === 'percent') box.append(el('div', 'unknown-bar'));
    return box;
  }
  const money = w.unit && w.unit !== 'percent';
  value.append(el('strong', '', money ? `${amount.toFixed(2)} ${w.unit}` : `${Math.round(amount)}% left`));
  if (w.resets_at) value.append(` · resets ${until(w.resets_at)}`);
  // Money is a number, never a bar: there is no ceiling to measure it against.
  if (!money) {
    // No kit label: the row above already says it, and the kit's caption repeats it.
    mountProgress(box, {value: amount, tone: tone(amount)});
    box.querySelector('[role=progressbar]')?.setAttribute('aria-label', `${w.label}: ${Math.round(amount)}% left`);
  }
  return box;
}

function sourceCard(s: Source) {
  const latest = s.latest;
  // Tarmac holds a source on its tightest window. While another window still
  // has room (Antigravity's Gemini group with Claude and GPT spent), the red
  // bar says enough, and "held" would read as the whole provider being out.
  const room = (latest?.windows || []).some(w => !w.per_model && (w.remaining_percent ?? 0) > 0);
  const held = room ? undefined : status?.holds[s.id];
  const card = el('section', 'source'), top = el('div', 'top');
  top.append(el('span', 'name', s.account ? `${s.name} · ${s.account}` : s.name));
  const pill = s.needs_login ? {label: 'sign in needed', tone: 'error' as Tone}
    : held ? {label: 'held', tone: 'warning' as Tone}
    : s.status !== 'current' ? {label: s.status, tone: (s.status === 'stale' ? 'warning' : 'neutral') as Tone} : null;
  if (pill) mountBadge(top, pill);
  card.append(top);
  if (s.kind === 'api' && latest?.remaining != null)
    card.append(el('div', 'balance', `${latest.remaining.toFixed(2)} ${latest.unit || ''}`.trim()));
  for (const w of latest?.windows || []) card.append(windowRow(w));
  if (held) card.append(el('div', 'held', localTimes(held)));
  if (latest?.reason) card.append(el('div', 'reason', latest.reason));
  const notes: string[] = [];
  if (s.burn_rate) notes.push(`burn ${s.burn_rate.toFixed(2)}%/h`);
  if (latest?.credits?.text) notes.push(latest.credits.text);
  if (latest?.observed_at) notes.push('read ' + new Date(latest.observed_at).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}));
  if (notes.length) card.append(el('div', 'dim', notes.join(' · ')));
  const actions = el('div', 'actions');
  mountButton(actions, {label: 'Refresh', variant: 'outline', size: 'xs', loading: busy.has(s.slug), onClick: () => void refresh(s.slug)});
  if (s.needs_login && ['claude', 'codex'].includes(s.adapter))
    mountButton(actions, {label: 'Sign in', size: 'xs', onClick: () => void login(s.slug)});
  card.append(actions);
  return card;
}

function paint() {
  const box = node('notice');
  box.replaceChildren();
  for (const n of [problem, message]) if (n) mountBanner(box, n);
  node('loading').replaceChildren();
  const list = node('sources');
  list.replaceChildren();
  if (!status) {
    if (!problem) mountSpinner(node('loading'), {label: 'Reading Tarmac…'});
    return;
  }
  node('stamp').textContent = 'as of ' + new Date(status.generated_at).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  if (!status.sources.length) mountEmpty(list, {title: 'No sources', body: 'Add one with `tarmac sources add <provider>`.'});
  for (const s of status.sources) list.append(sourceCard(s));
}

function say(next: Notice | null) {
  message = next;
  clearTimeout(messageTimer);
  if (next) messageTimer = window.setTimeout(() => { message = null; paint(); }, 30_000);
}

function explain(error: unknown): Notice {
  if (error instanceof HostRequestError) {
    if (error.code === 'NO_SERVICE') return {tone: 'warning' as const, title: 'Allow the Tarmac service', body: 'Approve it in Settings → Extensions. It only relays reads from Tarmac on 127.0.0.1:8767.'};
    if (error.code === 'DISABLED') return {tone: 'info' as const, title: 'Tarmac is paused', body: 'Resume it in Settings → Extensions.'};
    if (error.code === 'SERVICE_FAILED') return {tone: 'error' as const, title: 'The Tarmac service stopped', body: 'Close and reopen this panel to restart it.'};
  }
  return {tone: 'error' as const, title: 'Tarmac is unreachable', body: error instanceof Error ? error.message : String(error)};
}

async function call(method: 'GET' | 'POST', path: string, query: Record<string, string> = {}) {
  const result = await host.serviceRequest({method, path, query});
  const value = JSON.parse(result.body || '{}');
  if (result.status >= 400) throw new Error(value.error || `Tarmac answered ${result.status}`);
  return value;
}

function attention(next: Status) {
  const logins = next.sources.filter(s => s.needs_login).length;
  const claudeFlagged = next.sources.some(s => s.adapter === 'claude' && s.needs_login);
  return logins + (next.claude_auth?.logged_in === false && !claudeFlagged ? 1 : 0);
}

async function load() {
  try {
    const next: Status = await call('GET', '/status', visible ? {} : {passive: '1'});
    status = next;
    problem = next.claude_auth?.logged_in === false
      ? {tone: 'warning', title: 'Claude is not signed in', body: 'Run `claude /login`.'} : null;
    // The host clears the badge when the panel opens, so it only matters while hidden.
    const count = visible ? null : attention(next) || null;
    if (count !== badge) { badge = count; void host.setBadge(count).catch(() => {}); }
  } catch (error) {
    problem = explain(error);
  }
  paint();
}

async function refresh(slug: string) {
  busy.add(slug); paint();
  try {
    const result = await call('POST', '/refresh', {slug});
    say(result.refresh === 'pending' ? {tone: 'info', title: `${slug} is still polling`, body: 'The new reading shows on the next update.'} : null);
  } catch (error) {
    say(explain(error));
  }
  busy.delete(slug);
  await load();
}

async function login(slug: string) {
  try {
    await call('POST', '/login', {slug});
    say({tone: 'info', title: 'Sign-in opened in Terminal', body: 'Finish it on the Mac running Tarmac, then refresh this source.'});
  } catch (error) {
    say(explain(error));
  }
  paint();
}

function schedule() {
  clearInterval(timer);
  timer = window.setInterval(() => void load(), visible ? VISIBLE_POLL_MS : HIDDEN_POLL_MS);
}

function setVisible(next: boolean) {
  if (next === visible) return;
  visible = next;
  schedule();
  if (visible) void load();
}

let started = false;
host.onReady(context => {
  applyHostReady(context, document.documentElement);
  if (started) return;
  started = true;
  paint();
  void load();
  schedule();
});

// The host keeps rail pages mounted while hidden; a hidden panel is not a reader.
new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting))).observe(document.documentElement);
document.addEventListener('visibilitychange', () => setVisible(!document.hidden));
