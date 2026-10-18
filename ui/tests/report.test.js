import test from 'node:test';
import assert from 'node:assert/strict';
import { reportSelection, auditReview, filterAudit, auditCsv } from '../src/lib/report.js';

test('report selects matching nested window, limits unique audit runs and never falls back to another period', () => {
  const metrics = {label:'7d',production:{runs:75}};
  const audit = Array.from({length:60}, (_,i) => ({run_id:String(i)}));
  const data = {dashboard:{metrics:{windows:[metrics]}},reports:[{label:'7d',report:{audit:[audit[0],...audit]}}]};
  const selected = reportSelection(data,'7d');
  assert.equal(selected.metrics,metrics);
  assert.equal(selected.audit.length,50);
  assert.equal(selected.audit[1].run_id,'1');
  assert.deepEqual(reportSelection(data,'today'),{metrics:null,audit:[]});
});

test('audit details require the exact run, project, review and revision', () => {
  const row = {run_id:'r1',project:'p',iid:7,sha:'sha'};
  const review = {project:'p',iid:7,sha:'sha',detail:{run:{run_id:'r1'}}};
  assert.equal(auditReview([review],row),review);
  for (const patch of [{run_id:'r2'},{project:'other'},{iid:8},{sha:'new'},{run_id:null}]) {
    assert.equal(auditReview([review],{...row,...patch}),undefined);
  }
});

test('audit search applies every term case-insensitively and distinguishes dry runs', () => {
  const rows = [{run_id:'1',project:'Bubo',model:'GPT-5',dry_run:true}, {run_id:'2',project:'Bubo',model:'Claude',dry_run:false}];
  assert.deepEqual(filterAudit(rows,'BUBO gpt dry'),[rows[0]]);
  assert.deepEqual(filterAudit(rows,'production'),[rows[1]]);
  assert.deepEqual(filterAudit(rows,'missing'),[]);
});

test('CSV protects formulas and quotes, preserves known zero and blanks unrecorded cost', () => {
  const csv = auditCsv([{run_id:'=HYPERLINK("bad")',project:' +SUM(1,2)',model:'line\n"quoted"',cost_usd:0,cost_recorded:false},
    {run_id:'safe',cost_usd:0,cost_recorded:true}]);
  assert.ok(csv.includes('"\'=HYPERLINK(""bad"")"'));
  assert.ok(csv.includes('"\' +SUM(1,2)"'));
  assert.ok(csv.includes('"line\n""quoted"""'));
  assert.ok(csv.includes('"","false"'));
  assert.ok(csv.includes('"0","true"'));
});
