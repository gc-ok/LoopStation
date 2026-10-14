// Run the application's actual monitor functions against a controlled clock.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const html = require('node:fs').readFileSync(0, 'utf8');
const animation = 'function animateCountdown() {' + html.split('function animateCountdown() {')[1].split('async function poll()')[0];
const polling = 'async function poll() {' + html.split('async function poll() {')[1].split('\ninitDept();')[0];
const stale = 'function markStale(' + html.split('function markStale(')[1].split('// ---- per-device')[0];
let now = 1000, failure = false, hang = false, nextTimer = 0;
const timers = new Map(), nodes = {};
let data = {state: 'playing', is_playing: true, is_looping: false, playback_rate: 0.5,
  end_countdown: 40, position: 10, song_duration: 30, updated_at: 1,
  next: {name: 'Next', countdown: 20}};
const context = {
  document: {getElementById: id => nodes[id] ||= {}},
  performance: {now: () => now}, requestAnimationFrame: () => {}, AbortController,
  setTimeout: fn => { timers.set(++nextTimer, fn); return nextTimer; },
  clearTimeout: id => timers.delete(id),
  formatTime: n => String(n), formatCountdown: n => String(n),
  renderStandby: () => {}, renderGoLog: () => {}, rerenderCards: () => {},
  fetch: async (_url, options) => {
    if (failure) throw Error('network lost');
    if (hang) return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(Error('request timed out'))));
    return {ok: true, status: 200, json: async () => data};
  }
};
vm.createContext(context);
vm.runInContext(`var pollInFlight=false, failCount=0, serverIsPlaying=false,
  serverPosition=0, serverCountdown=null, serverDuration=0, lastPollTime=0,
  serverPlaybackRate=1, serverEndCountdown=null, serverUpdatedAt=null, serverUpdateSeenAt=0,
  STALE_MS=3000, FETCH_TIMEOUT_MS=2500, lastNextName='', lastAcks={}, lastCurrent=null,
  lastNext=null, API_URL='/api/state'; ${stale} ${animation} ${polling}`, context);

(async () => {
  await context.poll(); now += 1000; context.animateCountdown();
  assert.equal(nodes.countdown.textContent, '19');
  assert.equal(nodes.positionBar.textContent, '10.5');
  failure = true; await context.poll(); now += 1000; context.animateCountdown();
  assert.equal(context.serverIsPlaying, false);
  assert.equal(nodes.countdown.textContent, '--:--');
  assert.equal(nodes.countdownLabel.textContent, 'UPDATES STOPPED');
  failure = false; data = {...data, updated_at: 2}; await context.poll();
  assert.equal(nodes.connStatus.textContent, 'Connected');
  assert.equal(nodes.countdown.textContent, '20');
  data = {...data, state: 'paused', is_playing: false, is_paused: true, is_looping: true};
  await context.poll(); now += 2000; context.animateCountdown();
  assert.equal(nodes.statusText.textContent, 'Paused');
  assert.equal(nodes.countdown.textContent, '20');
  data = {...data, state: 'playing', is_playing: true, is_paused: false, updated_at: 3};
  await context.poll(); now += 4000; context.animateCountdown();
  assert.equal(context.serverIsPlaying, false);
  assert.equal(nodes.countdown.textContent, '--:--');
  // Even responsive HTTP cannot hide a frozen desktop state timestamp.
  await context.poll();
  assert.equal(nodes.countdownLabel.textContent, 'UPDATES STOPPED');
  hang = true; const pending = context.poll();
  for (const timeout of timers.values()) timeout();
  await pending;
  assert.equal(context.pollInFlight, false);
  hang = false; data = {...data, updated_at: 4}; await context.poll();
  assert.equal(nodes.connStatus.textContent, 'Connected');
  console.log('monitor checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
