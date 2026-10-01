import { $, load, fail, kpis, seg, select, bars, table, xy, legend, pct, usd, num } from './kit.js';

const COLOR = { direct: '#dc2626', 'client retries': '#f59e0b', gateway: 'var(--accent)' };
const LABEL = { direct: 'Call one provider directly', 'client retries': 'Client-side retries', gateway: 'This gateway' };
const sKey = (s) => (s.strategy === 'gateway' ? 'gateway' : s.strategy === 'direct' ? 'direct' : 'retries');

try {
  const data = await load('data.json');
  const runs = Object.values(data);
  const by = Object.fromEntries(runs.map((r) => [sKey(r), r]));
  const direct = by.direct, gw = by.gateway;

  // ---- headline KPIs ----
  const costUp = (gw.cost_usd.total - direct.cost_usd.total) / direct.cost_usd.total;
  kpis($('#kpis'), [
    { label: 'Interactive answered', value: pct(gw.interactive.availability), note: `vs ${pct(direct.interactive.availability)} calling one provider` },
    { label: 'p95 latency', value: `${num(gw.interactive.p95_s)} s`, note: `vs ${num(direct.interactive.p95_s)} s direct` },
    { label: 'Batch completed', value: pct(gw.batch.completion), note: `${gw.batch.queued} requests queued through the outage` },
    { label: 'Spend', value: usd(gw.cost_usd.total, 2), note: `+${pct(costUp, 0)} for staying up` },
  ]);

  // ---- comparison: table (always) + metric bars ----
  const order = ['direct', 'client retries', 'gateway'];
  const ordered = order.map((s) => runs.find((r) => r.strategy === s)).filter(Boolean);
  table($('#cmpTable'),
    [
      { key: 'setup', label: 'Setup' },
      { key: 'ans', label: 'Interactive answered', num: true },
      { key: 'p50', label: 'p50', num: true },
      { key: 'p95', label: 'p95', num: true },
      { key: 'batch', label: 'Batch completed', num: true },
      { key: 'spend', label: 'Spend', num: true },
    ],
    ordered.map((r) => ({
      setup: LABEL[r.strategy], k: sKey(r),
      ans: pct(r.interactive.availability), p50: `${num(r.interactive.p50_s)} s`,
      p95: `${num(r.interactive.p95_s)} s`, batch: pct(r.batch.completion), spend: usd(r.cost_usd.total, 2),
    })),
    { hl: (r) => r.k === 'gateway', label: 'Setups compared' });

  const METRIC = {
    answered: { title: 'Interactive requests answered — higher is better', pick: (r) => r.interactive.availability, fmt: (v) => pct(v) },
    p95: { title: 'p95 latency in seconds — lower is better', pick: (r) => r.interactive.p95_s, fmt: (v) => `${num(v)} s` },
    batch: { title: 'Batch work completed — higher is better', pick: (r) => r.batch.completion, fmt: (v) => pct(v) },
    spend: { title: 'Total spend on the run — lower is cheaper', pick: (r) => r.cost_usd.total, fmt: (v) => usd(v, 2) },
  };
  seg($('#metricSel'), [['answered', 'Answered'], ['p95', 'p95 latency'], ['batch', 'Batch'], ['spend', 'Spend']], 'answered', (v) => {
    const m = METRIC[v] || METRIC.answered;
    bars($('#cmpBars'), ordered.map((r) => ({
      label: LABEL[r.strategy], value: m.pick(r), text: m.fmt(m.pick(r)),
      color: COLOR[r.strategy], dim: r.strategy !== 'gateway',
    })), { title: m.title });
  });

  // ---- timeline ----
  const line = (r) => Object.entries(r.success_by_5s).map(([t, v]) => ({ x: +t, y: v * 100 })).sort((a, b) => a.x - b.x);
  const SERIES = {
    direct: { name: LABEL.direct, color: COLOR.direct, points: line(direct), dash: true, width: 2 },
    retries: { name: LABEL['client retries'], color: COLOR['client retries'], points: line(by.retries), dash: true, width: 2 },
    gateway: { name: LABEL.gateway, color: 'var(--accent)', points: line(gw), width: 2.6 },
  };
  const VIEW = { all: ['direct', 'retries', 'gateway'], vs: ['direct', 'gateway'], gw: ['gateway'] };
  const drawTimeline = (v) => {
    const keys = VIEW[v] || VIEW.all;
    const series = keys.map((k) => SERIES[k]);
    xy($('#tlChart'), {
      height: 300, label: 'Share of interactive requests answered in each 5-second window',
      x: { label: 'seconds into the run', min: 0, max: 235, fmt: (t) => `${t}s` },
      y: { label: '% answered', min: 0, max: 100, fmt: (t) => `${t}%`, pad: 0 },
      vline: { x: 100, label: 'all providers down 100–115s' },
      series,
    });
    legend($('#tlKey'), series);
  };
  select($('#tlSel'), [['all', 'All three setups'], ['vs', 'Gateway vs direct'], ['gw', 'Gateway only']], 'all', drawTimeline);

  // ---- breaker events ----
  const events = gw.breaker_events || [];
  const firstAfter = (from, pred) => events.find((e) => e.t >= from && pred(e));
  const openAlpha60 = firstAfter(60, (e) => e.provider === 'alpha' && e.to === 'open');
  const closeAlpha120 = firstAfter(120, (e) => e.provider === 'alpha' && e.to === 'closed');
  const openAlphaSlow = firstAfter(150, (e) => e.provider === 'alpha' && e.to === 'open' && /p95|budget|latenc/i.test(e.reason));
  const react = [];
  if (openAlpha60) react.push(`opened <b>${num(openAlpha60.t - 60, 1)} s</b> after alpha began failing at 60 s`);
  if (closeAlpha120) react.push(`closed again <b>${num(closeAlpha120.t - 120, 1)} s</b> after alpha recovered at 120 s`);
  if (openAlphaSlow) react.push(`tripped on latency <b>${num(openAlphaSlow.t - 150, 1)} s</b> after alpha slowed at 150 s`);
  $('#brStats').innerHTML = `${events.length} automatic state changes across the run — alpha's breaker ` + react.join('; ') + '.';

  const CHANGE = { 'closed→open': 'tripped open', 'open→half_open': 'probing (half-open)', 'half_open→closed': 'recovered (closed)', 'half_open→open': 'probe failed, reopened' };
  const drawBreakers = (prov) => {
    const rows = events.filter((e) => prov === 'all' || e.provider === prov);
    table($('#brTable'),
      [
        { key: 't', label: 'at', num: true, fmt: (v) => `${num(v, 1)} s` },
        { key: 'provider', label: 'provider' },
        { key: 'change', label: 'what happened' },
        { key: 'reason', label: 'why' },
      ],
      rows.map((e) => ({ t: e.t, provider: e.provider, change: CHANGE[`${e.from}→${e.to}`] || `${e.from} → ${e.to}`, reason: e.reason })),
      { cls: (r) => (/open/.test(r.change) && !/recover/.test(r.change) ? 'bad' : /recover/.test(r.change) ? 'good' : ''), label: 'Breaker state changes' });
  };
  seg($('#brSel'), [['all', 'All'], ['alpha', 'alpha'], ['beta', 'beta'], ['gamma', 'gamma']], 'all', drawBreakers);

  // ---- cost ----
  const COST = {
    strategy: {
      rows: () => ordered.map((r) => ({ label: LABEL[r.strategy], value: r.cost_usd.total, text: usd(r.cost_usd.total, 2), color: COLOR[r.strategy], dim: r.strategy !== 'gateway' })),
      note: `The gateway spent ${usd(gw.cost_usd.total, 2)} against ${usd(direct.cost_usd.total, 2)} for the direct call — up ${pct(costUp, 0)}. Failover sends some traffic to a pricier provider, and ${gw.cost_usd.hedge_losers ? `${num(100 * gw.cost_usd.hedge_losers / gw.cost_usd.total, 0)}% of spend` : 'a slice of spend'} paid for ${gw.interactive.hedged} hedged calls that lost the race.`,
    },
    tenant: {
      rows: () => Object.entries(gw.cost_usd.by_tenant).map(([k, v]) => ({ label: k, value: v, text: usd(v, 2), color: 'var(--accent)' })),
      note: 'Every request is tagged with its tenant, so spend splits cleanly for billing or chargeback.',
    },
    feature: {
      rows: () => Object.entries(gw.cost_usd.by_feature).map(([k, v]) => ({ label: k, value: v, text: usd(v, 2), color: 'var(--fx-2)' })),
      note: 'The same tagging attributes spend to each product feature — here chat, search and summarize.',
    },
  };
  select($('#costSel'), [['strategy', 'By setup'], ['tenant', 'By tenant'], ['feature', 'By feature']], 'strategy', (v) => {
    const c = COST[v] || COST.strategy;
    bars($('#costBars'), c.rows(), { fmt: (x) => usd(x, 2) });
    $('#costNote').innerHTML = c.note;
  });
} catch (err) {
  fail(err);
}
