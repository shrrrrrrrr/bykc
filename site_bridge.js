// Reuse the school's own encryption and session handling. Never read/export tokens.
() => {
  if (location.origin !== 'https://bykc.buaa.edu.cn') return false;
  if (window.__bykcLocal) return true;
  const injector = window.angular?.element(document.querySelector('main')).injector();
  if (!injector) return false;
  const api = injector.get('ApiService');
  const auth = injector.get('AuthService');
  const required = {
    GetStudentSemesterCourseList: 'queryStudentSemesterCourseByPage',
    GetStudentSemesterChosenCourseStatus: 'queryStudentSemesterChosenCourseStatus',
    SignupCourse: 'choseCourse',
  };
  if (Object.entries(required).some(([k, v]) => api.API[k] !== v)) return false;

  let pending = null;
  const failure = e => {
    const code = String(e?.status ?? '');
    const msg = String(e?.errmsg ?? '');
    if (['98005399', '98000023'].includes(code)) return {kind: 'auth'};
    if (code === '98001478') return {kind: 'profile'};
    if (/验证码|安全验证|访问频繁|频繁操作|限流|too many|429/i.test(msg)) return {kind: 'limited'};
    if (/未开始|尚未|未开放/.test(msg)) return {kind: 'not_open'};
    if (/已满|满员|无剩余|人数已达/.test(msg)) return {kind: 'full'};
    if (/已经.*[选报]|重复|已[选报]/.test(msg)) return {kind: 'already'};
    if (/资格|冲突|不符合|不允许|上限|结束|限制|不能|无法/.test(msg)) return {kind: 'denied'};
    if (['-999', '-998'].includes(code) || /繁忙|稍后|超时/.test(msg)) return {kind: 'busy'};
    return {kind: 'rejected'};
  };
  const request = async (key, params) => {
    if (!auth.isLogin()) return {kind: 'auth'};
    const p = api.getDefaultRequestParams();
    p.userParams = params;
    p.config.showSpinner = false;
    p.config.showErrorTips = false;
    p.config.requestTimeout = 8;
    let timer;
    try {
      return await Promise.race([
        Promise.resolve(api.requestAPI({apiName: api.API[key], params: p}))
          .then(r => String(r?.status) === '0' ? {kind: 'ok', data: r.data} : failure(r), failure),
        new Promise(resolve => {timer = setTimeout(() => resolve({kind: 'uncertain'}), 10000);}),
      ]);
    } finally {clearTimeout(timer);}
  };
  const courseFields = c => Object.fromEntries([
    'id', 'courseName', 'courseTeacher', 'coursePosition', 'courseCampusList',
    'courseStartDate', 'courseEndDate', 'courseSelectStartDate', 'courseSelectEndDate',
    'courseCurrentCount', 'courseMaxCount', 'courseSelectType', 'selected',
    'courseNewKind1', 'courseNewKind2',
  ].map(k => [k, c[k]]));
  window.__bykcLocal = {
    ready: () => auth.isLogin(),
    async list(page) {
      const r = await request('GetStudentSemesterCourseList', {pageNumber: page, pageSize: 20});
      if (r.kind !== 'ok') return r;
      if (!Array.isArray(r.data?.content) || !Number.isInteger(r.data.totalPages)) return {kind: 'changed'};
      return {kind: 'ok', courses: r.data.content.map(courseFields), pages: r.data.totalPages};
    },
    async eligibility(course) {
      const r = await request('GetStudentSemesterChosenCourseStatus', {});
      if (r.kind !== 'ok') return r;
      if (!r.data || typeof r.data !== 'object') return {kind: 'changed'};
      const key = `status_${course.courseNewKind1?.id}_${course.courseNewKind2?.id}`;
      if (r.data[key] === true && Number(r.data.redundancyCount) <= 0) return {kind: 'denied'};
      return {kind: 'ok'};
    },
    async submit(id) {
      if (pending) return {kind: 'uncertain'};
      // Keep a latch after an uncertain response: never issue concurrent/doubtful writes.
      pending = request('SignupCourse', {courseId: id});
      const result = await pending;
      if (result.kind !== 'uncertain') pending = null;
      return {kind: result.kind};
    },
  };
  return true;
}
