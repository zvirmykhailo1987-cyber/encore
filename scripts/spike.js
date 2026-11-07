// Doors open. Thousands of fans keep refreshing the event page while the gates scan tickets.
//
// TARGET    base URL, for example http://edge or http://web:8000
// FANS      peak number of people refreshing at the same time
// THINK     seconds between one person's refreshes (average)
// HOLD      seconds at the peak
// SCANS     gate scans per second, 0 for none
// BROWSER   1 to ask for compressed replies, as real browsers do
// OUT       where to write the JSON summary
import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';

const TARGET = __ENV.TARGET || 'http://edge';
const FANS = Number(__ENV.FANS || 1000);
const THINK = Number(__ENV.THINK || 2);
const HOLD = Number(__ENV.HOLD || 60);
const SCANS = Number(__ENV.SCANS || 0);
const RAMP = Number(__ENV.RAMP || 20);
const TICKETS = Number(__ENV.TICKETS || 500000);
const TOKEN = __ENV.ENCORE_OPERATOR_TOKEN || '';
const HOST = { Host: 'localhost' };
const FAN_HEADERS = __ENV.BROWSER === '1' ? { ...HOST, 'Accept-Encoding': 'gzip' } : HOST;

const edgeHits = new Counter('edge_cache_hits');
const unexpected = new Counter('unexpected_responses');

const scenarios = {
  fans: {
    executor: 'ramping-vus',
    exec: 'fan',
    startVUs: 0,
    stages: [
      { duration: `${RAMP}s`, target: FANS },
      { duration: `${HOLD}s`, target: FANS },
      { duration: '5s', target: 0 },
    ],
    gracefulRampDown: '5s',
    gracefulStop: '10s',
    tags: { flow: 'fan' },
  },
};
if (SCANS > 0) {
  scenarios.gates = {
    executor: 'constant-arrival-rate',
    exec: 'gate',
    rate: SCANS,
    timeUnit: '1s',
    duration: `${RAMP + HOLD}s`,
    preAllocatedVUs: Math.min(500, SCANS * 2),
    maxVUs: 1000,
    tags: { flow: 'gate' },
  };
}

export const options = {
  scenarios,
  discardResponseBodies: true,
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max'],
  noConnectionReuse: false,
  thresholds: {
    'http_req_duration{flow:fan}': ['p(95)>=0'],
    'http_req_duration{flow:gate}': ['p(95)>=0'],
    'http_req_failed{flow:fan}': ['rate>=0'],
    'http_req_failed{flow:gate}': ['rate>=0'],
  },
};

export function fan() {
  // Most refreshes load the page. Some fetch the live numbers behind it.
  const url = Math.random() < 0.8 ? `${TARGET}/events/LOADTEST` : `${TARGET}/api/v1/events/LOADTEST/availability`;
  const reply = http.get(url, { headers: FAN_HEADERS, timeout: '10s' });
  if (['HIT', 'STALE', 'UPDATING'].includes(reply.headers['X-Edge-Cache'])) edgeHits.add(1);
  if (!check(reply, { 'fan got an answer': (r) => r.status === 200 || r.status === 304 })) unexpected.add(1);
  sleep(THINK * (0.5 + Math.random()));
}

export function gate() {
  const ticket = `G${exec.scenario.iterationInTest % TICKETS}`;
  const body = JSON.stringify({ ticket, scan_id: `${__VU}-${__ITER}-${Date.now()}` });
  const headers = { ...HOST, 'Content-Type': 'application/json', Origin: 'http://localhost' };
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  const reply = http.post(`${TARGET}/api/v1/scans`, body, { headers, timeout: '10s' });
  if (!check(reply, { 'gate admitted the ticket': (r) => r.status === 200 })) unexpected.add(1);
}

function metric(name) {
  const found = data_.metrics[name];
  return found ? found.values : {};
}

let data_;
export function handleSummary(data) {
  data_ = data;
  const fan = metric('http_req_duration{flow:fan}');
  const gate = metric('http_req_duration{flow:gate}');
  const failed = metric('http_req_failed');
  const fanFailed = metric('http_req_failed{flow:fan}');
  const gateFailed = metric('http_req_failed{flow:gate}');
  const summary = {
    target: TARGET,
    fans: FANS,
    think_seconds: THINK,
    hold_seconds: HOLD,
    scans_per_second_requested: SCANS,
    requests: metric('http_reqs').count || 0,
    requests_per_second: Math.round(metric('http_reqs').rate || 0),
    failed_rate: failed.rate || 0,
    unexpected_responses: metric('unexpected_responses').count || 0,
    edge_cache_hits: metric('edge_cache_hits').count || 0,
    peak_virtual_users: metric('vus_max').max || 0,
    fan: { p50: fan.med, p95: fan['p(95)'], p99: fan['p(99)'], max: fan.max, failed_rate: fanFailed.rate },
    gate: { p50: gate.med, p95: gate['p(95)'], p99: gate['p(99)'], max: gate.max, failed_rate: gateFailed.rate },
  };
  const result = { stdout: JSON.stringify(summary) + '\n' };
  if (__ENV.OUT) result[__ENV.OUT] = JSON.stringify(summary, null, 2);
  return result;
}
