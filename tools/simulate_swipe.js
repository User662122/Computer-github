#!/usr/bin/env node
/*
 * Drive the real desktop viewer (.github/desktop_client.html) in Node with a
 * fake DOM, so a swipe can be inspected without a phone or a VM.
 *
 *   node tools/simulate_swipe.js            # summary
 *   node tools/simulate_swipe.js --json     # machine readable
 *
 * It answers /info, /quality and /frames the way the VM does -- a 960x540
 * delta picture of a 1920x1080 desktop -- then performs a one-finger swipe
 * from the middle of the screen upwards and prints every request the viewer
 * made. What matters:
 *
 *   * exactly one "pointer" request (touch-down), never one per move,
 *   * only "scroll" requests afterwards, with identical ratios to that
 *     parked pointer (i.e. the pointer is not dragged along the swipe),
 *   * the sum of dy mapped to the finger distance times screenW/frameW.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(REPO, '.github', 'desktop_client.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + '<script>'.length,
                          html.lastIndexOf('</script>'));

const VIEW_W = 390, VIEW_H = 780;      // phone viewport, CSS pixels
const SCREEN_W = 1920, SCREEN_H = 1080; // VM desktop
const FRAME_W = 960, FRAME_H = 540;    // delta picture

const requests = [];

function jsonResponse(body) {
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body),
                          text: () => Promise.resolve(JSON.stringify(body))});
}

function fakeFetch(url, options) {
  const opts = options || {};
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({url: url, body: body});
  if (url.indexOf('/input') === 0) return jsonResponse({ok: true});
  if (url.indexOf('/info') === 0) return jsonResponse({width: SCREEN_W, height: SCREEN_H});
  if (url.indexOf('/quality') === 0) return jsonResponse({ok: true});
  if (url.indexOf('/frames') === 0) {
    // One empty long-poll answer, then nothing: the simulation only cares
    // about the gesture, not about pixels.
    return jsonResponse({seq: 1, r: [], full: false, w: FRAME_W, h: FRAME_H,
                         reason: 'empty'});
  }
  return jsonResponse({});
}

function element(id) {
  const listeners = {};
  return {
    id: id, style: {}, className: '', textContent: '', value: '',
    clientWidth: VIEW_W, clientHeight: VIEW_H,
    width: FRAME_W, height: FRAME_H,
    getContext: () => ({fillRect(){}, clearRect(){}, drawImage(){},
                        fillStyle: ''}),
    getBoundingClientRect: () => ({left: 0, top: 0, width: FRAME_W, height: FRAME_H,
                                   right: FRAME_W, bottom: FRAME_H}),
    setPointerCapture(){}, releasePointerCapture(){},
    addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
    dispatch: (name, event) => (listeners[name] || []).forEach(fn => fn(event)),
  };
}

const elements = {};
['viewport', 'stage', 'screen', 'fps', 'st', 'modeBtn', 'dataBtn', 'qualityBtn',
 'typeText', 'zoomIn', 'zoomOut', 'zoomFit', 'zoomOne'].forEach(id => {
  elements[id] = element(id);
});

// ---- virtual clock ------------------------------------------------
// Timers are fired in due order, never ahead of the clock, so a 650 ms
// long press cannot fire during a 300 ms swipe and the 20 s frame poll
// stays out of the way.
let vclock = 0;
const GESTURE_BUDGET_MS = 600;      // how much virtual time a gesture may use
const timers = [];
global.setTimeout = (fn, ms) => {
  const timer = {fn: fn, ms: ms || 0, due: vclock + (ms || 0)};
  timers.push(timer);
  return timer;
};
global.clearTimeout = (timer) => {
  const index = timers.indexOf(timer);
  if (index >= 0) timers.splice(index, 1);
};
global.setInterval = () => 0;
global.Date.now = () => 1000000 + vclock;

async function runTimers() {
  // Timers and promise callbacks alternate in a browser; interleave them so
  // the viewer's "park the pointer, then send the wheel" chain can complete.
  for (let guard = 0; guard < 500; guard++) {
    for (let tick = 0; tick < 8; tick++) await Promise.resolve();
    if (!timers.length) return;
    timers.sort((a, b) => a.due - b.due);
    const next = timers[0];
    if (next.due > GESTURE_BUDGET_MS) return;   // not part of this gesture
    timers.shift();
    if (next.due > vclock) vclock = next.due;
    next.fn();
  }
}
function advance(ms) { vclock += ms; }

global.window = {
  performance: {now: () => vclock},
  devicePixelRatio: 2,
  addEventListener(){},
  AbortController: undefined,
};
global.document = {
  getElementById: (id) => elements[id] || element(id),
  addEventListener(){},
  activeElement: null,
};
global.fetch = fakeFetch;
global.Image = function () {};
global.URL = {createObjectURL: () => 'blob:x', revokeObjectURL(){}};
// navigator already exists in modern Node and its getter is read-only.

// ---- the gesture ---------------------------------------------------
const startY = VIEW_H * 0.5;
const startX = VIEW_W * 0.5;
const parkRatio = {x: (startX - 0) / FRAME_W, y: (startY - 0) / FRAME_H};

function pointer(type, x, y) {
  elements.viewport.dispatch(type, {pointerType: 'touch', pointerId: 1,
                                    clientX: x, clientY: y, preventDefault(){}});
}
function settle() { return runTimers(); }

(async () => {
  // Run the viewer.
  new Function(script)();
  await runTimers();

  pointer('pointerdown', startX, startY);
  await settle();
  const afterDown = requests.filter(r => r.body && r.body.action === 'pointer').length;

  // 12 moves of 20 px each = 240 CSS px swipe (a normal phone flick).
  for (let step = 1; step <= 12; step++) {
    advance(16);                       // one phone frame per pointermove
    pointer('pointermove', startX, startY - step * 20);
    await settle();
  }
  pointer('pointerup', startX, startY - 240);
  await settle();

  const inputs = requests.filter(r => r.url.indexOf('/input') === 0).map(r => r.body);
  const pointerMoves = inputs.filter(b => b.action === 'pointer');
  const scrolls = inputs.filter(b => b.action === 'scroll');
  const wheelDxDy = scrolls.map(b => ({dx: b.dx || 0, dy: b.dy || 0}));
  const totalDy = wheelDxDy.reduce((sum, s) => sum + s.dy, 0);
  const totalDx = wheelDxDy.reduce((sum, s) => sum + s.dx, 0);

  // The viewer fits a 960x540 picture into the 390x780 viewport: scale 0.40625,
  // so the stage is 390x219 and one CSS pixel is 1/0.40625 picture pixels.
  const fitScale = Math.min(VIEW_W / FRAME_W, VIEW_H / FRAME_H);
  const desktopUnit = SCREEN_W / FRAME_W;
  const expectedWheel = 240 / fitScale * desktopUnit;   // desktop px, negative

  const result = {
    viewport: [VIEW_W, VIEW_H],
    fitScale: Number(fitScale.toFixed(5)),
    stageSize: [Number((FRAME_W * fitScale).toFixed(1)), Number((FRAME_H * fitScale).toFixed(1))],
    pointerRequestsOnTouchDown: afterDown,
    pointerRequestsTotal: pointerMoves.length,
    pointerRequestAt: pointerMoves[0] ? [pointerMoves[0].x_ratio, pointerMoves[0].y_ratio] : null,
    parkedRatioFromFinger: [Number(parkRatio.x.toFixed(4)), Number(parkRatio.y.toFixed(4))],
    scrollRequests: scrolls.length,
    ratiosSentWhileSwiping: scrolls.map(b => [b.x_ratio, b.y_ratio]),
    totalWheelDy: Number(totalDy.toFixed(2)),
    totalWheelDx: Number(totalDx.toFixed(2)),
    expectedWheelDy: Number((-expectedWheel).toFixed(2)),
    wheelAsFractionOfFingerTravel: Number((Math.abs(totalDy) / expectedWheel).toFixed(3)),
    dragRequests: inputs.filter(b => b.action === 'drag').length,
    clickRequests: inputs.filter(b => b.action === 'click').length,
  };

  if (process.argv.indexOf('--json') >= 0) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    Object.entries(result).forEach(([key, value]) => {
      console.log(key + ': ' + JSON.stringify(value));
    });
  }
})();
