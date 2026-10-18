import test from 'node:test';
import assert from 'node:assert/strict';
import {dashboardCharts} from '../src/lib/dashboard-charts.js';

function fixture() {
  return {
    usage:{dry_runs:1},
    production:{runs:7, completed:6, unfinished:1, successful:4, failed:1,
      cost_usd:{total:1.2,recorded:5,missing:2},
      successful_usage:{cost_usd:{total:.6,recorded:3}},
      failed_usage:{cost_usd:{total:.35,recorded:1}}},
    findings:{total:4,verified:2,refuted:1},
    audit_evidence:{available:true,production_runs:7,policy_recorded:1,policy_missing:6,
      provenance_known:2,provenance_unknown:5},
  };
}
const sum = rows => rows.reduce((total,row) => total + row.value,0);

test('chart partitions preserve denominators, unfinished and other finished', () => {
  const c=dashboardCharts(fixture());
  assert.deepEqual(c.delivery.map(r=>r.value),[4,1,1,1]);
  assert.equal(sum(c.delivery),7);
  assert.equal(sum(c.evaluation),4);
  assert.ok(Math.abs(sum(c.spend)-1.2)<1e-12);
  assert.equal(c.spend.reduce((n,r)=>n+r.recorded,0),5);
  assert.equal(c.costMissing,2);
  assert.equal(c.dryRuns,1); // never added to production denominator
});

test('zero recorded cost remains distinct from missing cost and empty cohorts', () => {
  const m=fixture();
  m.production.cost_usd.total=0;
  m.production.successful_usage.cost_usd.total=0;
  m.production.failed_usage.cost_usd.total=0;
  const c=dashboardCharts(m);
  assert.equal(sum(c.spend),0);
  assert.equal(c.costRecorded,5);
  assert.equal(c.costMissing,2);
  for (const key of ['runs','completed','unfinished','successful','failed']) m.production[key]=0;
  for (const cost of [m.production.cost_usd,m.production.successful_usage.cost_usd,m.production.failed_usage.cost_usd]) {
    cost.recorded=0; if ('missing' in cost) cost.missing=0;
  }
  m.findings={total:0,verified:0,refuted:0};
  m.audit_evidence={available:true,production_runs:0,policy_recorded:0,policy_missing:0,provenance_known:0,provenance_unknown:0};
  assert.equal(dashboardCharts(m).productionRuns,0);
});

test('roundoff residual is zero but genuinely inconsistent spend is unavailable', () => {
  const m=fixture();
  m.production.cost_usd.total=.3;
  m.production.successful_usage.cost_usd.total=.1;
  m.production.failed_usage.cost_usd.total=.2;
  assert.equal(dashboardCharts(m).spend[2].value,0);
  m.production.cost_usd.total=.1;
  assert.equal(dashboardCharts(m),null);
});

test('missing policy is unavailable, not zero coverage; invalid inputs do not invent zero', () => {
  const m=fixture();
  m.audit_evidence.available=false;
  m.audit_evidence.policy_recorded=null;
  m.audit_evidence.policy_missing=null;
  assert.equal(dashboardCharts(m).audit.policyRecorded,null);
  assert.equal(dashboardCharts(m).audit.provenanceKnown,2);
  assert.equal(dashboardCharts(null),null);
  assert.equal(dashboardCharts({}),null);
  m.production.runs=Infinity;
  assert.equal(dashboardCharts(m),null);
  const inconsistent=fixture();
  inconsistent.findings.verified=5;
  assert.equal(dashboardCharts(inconsistent),null);
});

test('dry-run-only data is visible by default without becoming production data', () => {
  const m=fixture();
  m.validation=structuredClone(m.production);
  m.usage.dry_runs=m.validation.runs;
  m.production={...m.production,runs:0,completed:0,unfinished:0,successful:0,failed:0,
    cost_usd:{total:0,recorded:0,missing:0},successful_usage:{cost_usd:{total:0,recorded:0}},failed_usage:{cost_usd:{total:0,recorded:0}}};
  m.audit_evidence={available:true,production_runs:0,policy_recorded:0,policy_missing:0,provenance_known:0,provenance_unknown:0};
  const c=dashboardCharts(m);
  assert.equal(c.scope,'dry-run');assert.equal(c.runCount,7);assert.equal(c.productionRuns,0);
  assert.equal(sum(c.delivery),7);assert.equal(c.audit.denominator,0);
  assert.equal(dashboardCharts(m,'production').runCount,0);
});

test('mixed data respects explicit selection and does not add cohorts together', () => {
  const m=fixture();
  m.validation={...structuredClone(m.production),runs:1,completed:1,unfinished:0,successful:1,failed:0,
    cost_usd:{total:0,recorded:0,missing:1},successful_usage:{cost_usd:{total:0,recorded:0}},failed_usage:{cost_usd:{total:0,recorded:0}}};
  assert.equal(dashboardCharts(m).scope,'production');
  assert.equal(dashboardCharts(m).runCount,7);
  const dry=dashboardCharts(m,'dry-run');
  assert.equal(dry.runCount,1);assert.equal(dry.costMissing,1);assert.equal(dry.productionRuns,7);
  assert.equal(dry.audit.denominator,7);
  assert.equal(dashboardCharts(fixture(),'dry-run'),null);
});
