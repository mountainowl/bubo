import test from 'node:test';
import assert from 'node:assert/strict';
import {workbench, findingTitle, auditReview, reviewSummary} from '../src/lib/workbench.js';
const finding = (changes={}) => ({status:'posted', severity:'blocking', verified:null, fingerprint:'f', file:'src/auth.py', body:'## Token may cross trust boundary\n\nEvidence...', ...changes});
const review = (findings, changes={}) => ({project:'p',iid:1,sha:'exact-revision',detail:{findings,run:null},...changes});

test('priority distinguishes missing outcome from observed closure and excludes refuted/dry-run evidence', () => {
  const rows = [finding(),finding({fingerprint:'observed',outcome:{resolved:false}}),
    ...['resolved','deleted','disputed','false_positive','duplicate'].map(flag => finding({outcome:{[flag]:true}})),
    finding({verified:0}),finding({verified:false}),finding({status:'planned'}),
    finding({severity:'medium'}),finding({run_id:'dry'})];
  const result = workbench([review(rows,{detail:{findings:rows,run:{run_id:'dry',dry_run:true}}})]);
  assert.equal(result.priority.length,2);
  assert.equal(result.displayed[0].finding.outcome,undefined);
  assert.equal(result.displayed[1].finding.outcome.resolved,false);
  assert.equal(result.displayed[0].review.sha,'exact-revision');
  assert.equal(result.displayed[1].finding.fingerprint,'observed');
});

test('only embedded details contribute and visible list is bounded without changing eligible count', () => {
  const result = workbench([review(Array.from({length:12},(_,i)=>finding({fingerprint:String(i)}))),review([],{detail:undefined})]);
  assert.equal(result.details.length,1);
  assert.equal(result.priority.length,12);
  assert.equal(result.displayed.length,8);
});

test('headline is first recorded line with minimal markdown cleanup and bounded length', () => {
  assert.equal(findingTitle(finding()),'Token may cross trust boundary');
  assert.equal(findingTitle(finding({body:'x'.repeat(500)})).length,180);
  assert.equal(findingTitle(finding({body:'',category:'security'})),'security in src/auth.py');
});


test('latest dry run does not relabel older or unlinked finding evidence', () => {
 const rows = [finding({run_id:'older'}), finding()];
 const result = workbench([review(rows,{detail:{findings:rows,run:{run_id:'latest',dry_run:true}}})]);
 assert.equal(result.priority.length,2);
});


test('bounded list keeps critical findings ahead of lower severity unknown outcomes', () => {
 const rows = [...Array.from({length:9},(_,i)=>finding({severity:'high',fingerprint:String(i)})), finding({severity:'critical',fingerprint:'critical',outcome:{resolved:false}})];
 const result = workbench([review(rows)]);
 assert.equal(result.displayed[0].finding.fingerprint,'critical');
 assert.equal(result.priority.length,10);
 assert.equal(result.displayed.length,8);
});


test('audit drilldown cannot open another retry of the same revision', () => {
  const latest=review([], { detail:{run:{run_id:'new-run'},findings:[]} });
  const row={project:latest.project,iid:latest.iid,sha:latest.sha};
  assert.equal(auditReview([latest], {...row,run_id:'old-run'}), undefined);
  assert.equal(auditReview([latest], row), undefined);
  assert.equal(auditReview([latest], {...row,run_id:'new-run'}), latest);
});


test('review summaries partition verification and observations without adding overlapping flags', () => {
 const r=review([finding({verified:true,outcome:{resolved:true,disputed:true}}),finding({verified:0}),finding({verified:null,outcome:{}})]);
 const s=reviewSummary(r);
 assert.deepEqual([s.verified,s.refuted,s.unevaluated],[1,1,1]);
 assert.equal(s.observed,2); assert.equal(s.unobserved,1);
 assert.equal(s.priority.length,1); assert.equal(s.priorityObserved,1);
});

test('missing detail differs from empty running and failed reviews', () => {
 assert.equal(reviewSummary({status:'failed'}).available,false);
 assert.equal(reviewSummary(review([],{status:'running'})).empty,'No findings recorded yet');
 assert.equal(reviewSummary(review([],{status:'failed'})).empty,'No findings recorded');
});

test('latest-run measurements preserve missing cost and incomplete duration', () => {
 const r=review([], {detail:{findings:[],run:{cost_usd:0,cost_recorded:false,started_at:'2026-10-17T01:00:00Z',finished_at:null}}});
 assert.equal(reviewSummary(r).cost,null); assert.equal(reviewSummary(r).duration,null);
 r.detail.run.cost_recorded=true;r.detail.run.finished_at='2026-10-17T01:01:00Z';
 assert.equal(reviewSummary(r).cost,0); assert.equal(reviewSummary(r).duration,60);
 r.detail.run.finished_at='invalid';assert.equal(reviewSummary(r).duration,null);
});
