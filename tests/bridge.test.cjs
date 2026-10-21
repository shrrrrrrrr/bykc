const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'site_bridge.js'), 'utf8');

function fixture({loggedIn = true, handler, origin = 'https://bykc.buaa.edu.cn', changed = false, timeout = false} = {}) {
  const calls = [];
  const api = {
    API: {
      GetStudentSemesterCourseList: 'queryStudentSemesterCourseByPage',
      GetStudentSemesterChosenCourseStatus: 'queryStudentSemesterChosenCourseStatus',
      SignupCourse: changed ? 'unknown' : 'choseCourse',
    },
    getDefaultRequestParams: () => ({config: {}, userParams: {}}),
    requestAPI: request => {
      calls.push(request);
      return handler ? handler(request) : Promise.resolve({status: '0', data: {content: [], totalPages: 0}});
    },
  };
  const auth = {isLogin: () => loggedIn};
  const sandbox = {
    location: {origin},
    document: {querySelector: () => ({})},
    window: {angular: {element: () => ({injector: () => ({get: key => key === 'ApiService' ? api : auth})})}},
    setTimeout: timeout ? callback => setTimeout(callback, 1) : setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  const installed = vm.runInContext(`(${source})()`, sandbox);
  return {bridge: sandbox.window.__bykcLocal, calls, installed};
}

test('refuses a foreign origin and incompatible official API', () => {
  assert.equal(fixture({origin: 'https://evil.example'}).installed, false);
  assert.equal(fixture({changed: true}).installed, false);
});

test('expired login never sends API requests', async () => {
  const f = fixture({loggedIn: false});
  assert.equal((await f.bridge.submit(42)).kind, 'auth');
  assert.equal(f.calls.length, 0);
});

test('list only returns course fields; excludes tokens and private account fields', async () => {
  const f = fixture({handler: () => Promise.resolve({status: '0', token: 'DO_NOT_EXPORT', data: {
    totalPages: 1, content: [{id: 42, courseName: 'Example', selected: true, studentNo: 'PRIVATE', token: 'SECRET'}],
  }})});
  const result = await f.bridge.list(1);
  assert.equal(result.kind, 'ok');
  assert.equal(result.courses[0].id, 42);
  assert.equal(result.courses[0].selected, true);
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
  assert.ok(!JSON.stringify(result).includes('SECRET'));
  assert.ok(!JSON.stringify(result).includes('DO_NOT_EXPORT'));
  assert.deepEqual({...f.calls[0].params.userParams}, {pageNumber: 1, pageSize: 20});
});

test('submit uses exact official courseId parameter and preserves encryption service', async () => {
  const f = fixture();
  assert.equal((await f.bridge.submit(42)).kind, 'ok');
  assert.equal(f.calls[0].apiName, 'choseCourse');
  assert.deepEqual({...f.calls[0].params.userParams}, {courseId: 42});
  assert.equal(f.calls[0].params.config.showSpinner, false);
  assert.equal(f.calls[0].params.config.showErrorTips, false);
});

test('concurrent submit never creates a second write', async () => {
  let finish;
  const f = fixture({handler: () => new Promise(resolve => {finish = resolve;})});
  const first = f.bridge.submit(42);
  assert.equal((await f.bridge.submit(42)).kind, 'uncertain');
  assert.equal(f.calls.length, 1);
  finish({status: '0', data: {}});
  assert.equal((await first).kind, 'ok');
});

test('uncertain request keeps write latch even after timeout', async () => {
  const f = fixture({handler: () => new Promise(() => {}), timeout: true});
  assert.equal((await f.bridge.submit(42)).kind, 'uncertain');
  assert.equal((await f.bridge.submit(42)).kind, 'uncertain');
  assert.equal(f.calls.length, 1);
});

test('known failures mapped to fixed messages without exporting raw errors', async () => {
  for (const [status, errmsg, kind] of [
    ['98005399', 'PRIVATE', 'auth'], ['98001478', 'PRIVATE', 'profile'],
    ['x', '访问频繁 PRIVATE', 'limited'], ['x', '人数已满 PRIVATE', 'full'],
    ['x', '尚未开始 PRIVATE', 'not_open'], ['x', '已经选课 PRIVATE', 'already'],
    ['x', '不符合资格 PRIVATE', 'denied'], ['-999', 'PRIVATE', 'busy'],
    ['x', 'PRIVATE', 'rejected'],
  ]) {
    const f = fixture({handler: () => Promise.reject({status, errmsg, token: 'SECRET'})});
    const r = await f.bridge.submit(42);
    assert.equal(r.kind, kind);
    assert.ok(!JSON.stringify(r).includes('PRIVATE'));
    assert.ok(!JSON.stringify(r).includes('SECRET'));
  }
});

test('semester quota follows the official frontend rule', async () => {
  const f = fixture({handler: () => Promise.resolve({status: '0', data: {status_60_5: true, redundancyCount: 0}})});
  const result = await f.bridge.eligibility({courseNewKind1: {id: 60}, courseNewKind2: {id: 5}});
  assert.equal(result.kind, 'denied');
});

test('malformed course response fails closed', async () => {
  const f = fixture({handler: () => Promise.resolve({status: '0', data: {unfamiliar: []}})});
  assert.equal((await f.bridge.list(1)).kind, 'changed');
});
