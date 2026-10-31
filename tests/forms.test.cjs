const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const script = html.match(/<script type="text\/x-dc"[^>]*>([\s\S]*?)<\/script>/)[1];

function component(fetch) {
  class DCLogic {
    setState(update) { this.state = { ...this.state, ...(typeof update === 'function' ? update(this.state) : update) }; }
  }
  class FileReader {
    readAsDataURL(file) { this.result = 'data:application/pdf;base64,' + Buffer.from(file.content).toString('base64'); this.onload(); }
  }
  const context = vm.createContext({ DCLogic, fetch, FileReader, window: { location: { protocol: 'https:' }, scrollTo() {} } });
  return vm.runInContext(script + '\nnew Component()', context);
}
const event = { preventDefault() {} };
const success = async () => ({ ok: true, json: async () => ({ ok: true, submission_id: 'saved' }) });

test('provider waits for API, submits every step and category, and prevents double-clicks', async () => {
  let finish, calls = [];
  const c = component((url, options) => { calls.push({ url, data: JSON.parse(options.body) }); return new Promise(resolve => { finish = resolve; }); });
  c.state.f = { practice: 'Clinic', ptype: 'Other', pphone: '8320000000', pname: 'Provider', npi: '1234567890', email: 'test@example.com', message: 'Question' };
  c.state.cats = { 0: true };
  await c.next(event); await c.next(event);
  const sending = c.next(event);
  await c.next(event);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/forms/provider');
  assert.equal(calls[0].data.practice, 'Clinic');
  assert.equal(calls[0].data.categories.length, 1);
  assert.equal(c.state.pDone, false);
  assert.equal(c.state.providerSending, true);
  finish(await success()); await sending;
  assert.equal(c.state.pDone, true);
  assert.equal(c.state.providerSending, false);
});

test('provider and patient show errors and retain entered data when saving fails', async () => {
  const c = component(async () => ({ ok: false, json: async () => ({ error: 'Temporarily unavailable' }) }));
  c.state.step = 3; c.state.f = { message: 'Keep this message' };
  await c.next(event);
  assert.equal(c.state.pDone, false);
  assert.equal(c.state.err, 'Temporarily unavailable');
  assert.equal(c.state.f.message, 'Keep this message');
  c.state.pf = { name: 'Person', email: 'person@example.com', message: 'Keep this question' };
  await c.submitPatient(event);
  assert.equal(c.state.patDone, false);
  assert.equal(c.state.patErr, 'Temporarily unavailable');
  assert.equal(c.state.pf.message, 'Keep this question');
});

test('patient success requires server acknowledgment', async () => {
  const c = component(success);
  c.state.pf = { name: 'Person', email: 'person@example.com', message: 'Question' };
  await c.submitPatient(event);
  assert.equal(c.state.patDone, true);
  const broken = component(async () => ({ ok: true, json: async () => ({}) }));
  broken.state.pf = c.state.pf;
  await broken.submitPatient(event);
  assert.equal(broken.state.patDone, false);
});

test('career captures role, contact information, note, and actual résumé', async () => {
  let captured;
  const c = component(async (url, options) => { captured = { url, data: JSON.parse(options.body) }; return success(); });
  c.state.af = { name: 'Applicant', email: 'applicant@example.com', phone: '123', note: 'Interested' };
  c._resumeFile = { name: 'resume.pdf', size: 14, content: '%PDF-1.4\nhello' };
  await c.applyRole(event, 'Pharmacist');
  assert.equal(captured.url, '/api/forms/career');
  assert.equal(captured.data.role, 'Pharmacist');
  assert.equal(captured.data.note, 'Interested');
  assert.equal(Buffer.from(captured.data.resume.data, 'base64').toString(), c._resumeFile.content);
  assert.equal(c.state.applied, true);
});

test('career requires a résumé and does not claim success for failed writes', async () => {
  const c = component(async () => { throw new Error('offline'); });
  c.state.af = { name: 'Applicant', email: 'applicant@example.com' };
  await c.applyRole(event, 'Pharmacist');
  assert.match(c.state.appErr, /résumé/);
  c._resumeFile = { name: 'resume.pdf', size: 14, content: '%PDF-1.4\nhello' };
  await c.applyRole(event, 'Pharmacist');
  assert.equal(c.state.applied, false);
  assert.equal(c.state.applicationSending, false);
  assert.match(c.state.appErr, /could not reach/);
});

test('homepage placeholder testimonials stay inside the disabled conditional', () => {
  assert.match(html, /<sc-if value="\{\{ false \}\}">\s*<div[^>]*>\s*<div[^>]*>\s*<div[^>]*>From the providers we serve/);
});
