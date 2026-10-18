import assert from 'node:assert/strict';
import test from 'node:test';
import { outcomeLabel, outcomeTone } from '../src/lib/format.js';

test('missing observation remains unknown and deleted discussions are not open', () => {
  assert.equal(outcomeLabel(null), 'no outcome yet');
  assert.equal(outcomeLabel({ deleted: true }), 'deleted');
  assert.equal(outcomeTone({ deleted: true }), 'muted');
  assert.equal(outcomeLabel({}), 'open');
});
