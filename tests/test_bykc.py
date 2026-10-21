import unittest
import argparse
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import bykc


BASE = datetime(2026, 10, 21, 18, 0, tzinfo=bykc.SHANGHAI)


def course(**overrides):
    result = dict(id=42, courseName='航空与艺术', courseTeacher='示例教师',
                  courseCampusList=['沙河'], coursePosition='教室',
                  courseStartDate='2026-10-22 14:00:00',
                  courseSelectStartDate='2026-10-21 18:00:00',
                  courseSelectEndDate='2026-10-21 19:00:00',
                  courseSelectType='1', selected=False)
    return result | overrides


class Clock:
    def __init__(self):
        self.value = BASE.timestamp()
        self.sleeps = []

    def read(self):
        return self.value

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.value += seconds


class FakeSchool:
    def __init__(self, outcomes, verified=False, clock=None):
        self.outcomes = iter(outcomes)
        self.verified = verified
        self.calls = []
        self.reads = 0
        self.clock = clock

    def submit(self, course_id):
        self.calls.append((course_id, self.clock.read() if self.clock else 0))
        return next(self.outcomes, 'full')

    def verify(self, course_id):
        assert course_id == 42
        self.reads += 1
        if isinstance(self.verified, Exception):
            raise self.verified
        return self.verified

    def courses(self):
        yield course()
        yield course(id=43, courseStartDate='2026-10-23 14:00:00')


class TimeTests(unittest.TestCase):
    def test_today_uses_beijing_date(self):
        self.assertEqual(bykc.parse_time('18:00:00', BASE), BASE)

    def test_iso_offset_and_milliseconds(self):
        self.assertEqual(bykc.parse_time('2026-10-21T10:00:00Z'), BASE)
        self.assertEqual(bykc.parse_time(BASE.timestamp() * 1000), BASE)

    def test_invalid_time(self):
        for text in ('25:99:12', 'not-a-date'):
            with self.subTest(text=text), self.assertRaises(bykc.UserError):
                bykc.parse_time(text, BASE)

    def test_no_early_submission(self):
        with self.assertRaises(bykc.UserError):
            bykc.make_plan(course(), '17:59:59', current=BASE-timedelta(minutes=10))

    def test_explicit_past_is_not_tomorrow(self):
        with self.assertRaises(bykc.UserError):
            bykc.make_plan(course(), '18:00:00', current=BASE+timedelta(minutes=10))

    def test_default_open_course_starts_now(self):
        current = BASE + timedelta(minutes=2)
        self.assertEqual(bykc.make_plan(course(), current=current).start, current)

    def test_end_never_exceeds_school_closing(self):
        c = course(courseSelectEndDate='2026-10-21 18:00:02')
        self.assertEqual(bykc.make_plan(c, current=BASE).end, BASE+timedelta(seconds=2))

    def test_invalid_rate_or_window(self):
        for interval, duration in ((0.1, 60), (0.5, 601), (float('nan'), 30), (0.5, float('inf'))):
            with self.subTest(interval=interval, duration=duration), self.assertRaises(bykc.UserError):
                bykc.make_plan(course(), current=BASE, interval=interval, duration=duration)

    def test_wait_does_not_fire_early(self):
        clock = Clock()
        target = BASE+timedelta(seconds=1)
        bykc.wait_until(target, clock=clock.read, sleep=clock.sleep, report=lambda _: None)
        self.assertGreaterEqual(clock.value, target.timestamp())
        self.assertLess(clock.value-target.timestamp(), .001)


class EnrollmentTests(unittest.TestCase):
    def run_enrollment(self, outcomes, verified=False, seconds=3):
        clock = Clock()
        school = FakeSchool(outcomes, verified, clock)
        plan = bykc.Plan(BASE, BASE+timedelta(seconds=seconds))
        result = bykc.enroll(school, 42, plan, wall=clock.read, monotonic=clock.read,
                             sleep=clock.sleep, report=lambda _: None)
        return result, school, clock

    def test_first_attempt_immediate_and_stop_on_success(self):
        result, school, _ = self.run_enrollment(['ok', 'ok'])
        self.assertTrue(result[0])
        self.assertEqual(school.calls, [(42, BASE.timestamp())])

    def test_retry_full_until_success_with_spacing(self):
        result, school, _ = self.run_enrollment(['full', 'not_open', 'ok'])
        self.assertTrue(result[0])
        self.assertEqual([t-BASE.timestamp() for _, t in school.calls], [0, .5, 1])

    def test_full_eventually_fails_and_never_submits_after_end(self):
        result, school, clock = self.run_enrollment(['full']*20, seconds=1)
        self.assertFalse(result[0])
        self.assertEqual(len(school.calls), 2)
        self.assertEqual(clock.value, (BASE+timedelta(seconds=1)).timestamp())

    def test_terminal_errors_do_not_retry(self):
        for outcome in ('auth', 'profile', 'denied', 'limited', 'rejected'):
            with self.subTest(outcome=outcome):
                result, school, _ = self.run_enrollment([outcome, 'ok'])
                self.assertFalse(result[0])
                self.assertEqual(len(school.calls), 1)

    def test_unknown_write_is_never_replayed(self):
        result, school, _ = self.run_enrollment(['uncertain', 'ok'], seconds=5)
        self.assertFalse(result[0])
        self.assertEqual(len(school.calls), 1)
        self.assertGreater(school.reads, 1)

    def test_unknown_write_can_be_verified(self):
        result, school, _ = self.run_enrollment(['uncertain'], verified=True)
        self.assertTrue(result[0])
        self.assertEqual(len(school.calls), 1)

    def test_duplicate_without_verified_selection_is_not_success(self):
        result, school, _ = self.run_enrollment(['already'])
        self.assertFalse(result[0])
        self.assertEqual(len(school.calls), 1)

    def test_final_read_can_confirm_success(self):
        result, _, _ = self.run_enrollment(['full']*10, verified=True, seconds=1)
        self.assertTrue(result[0])

    def test_exhaustion_caps_writes_at_120(self):
        result, school, _ = self.run_enrollment(['busy']*200, seconds=600)
        self.assertFalse(result[0])
        self.assertEqual(len(school.calls), 120)

    def test_expired_plan_makes_no_writes(self):
        result, school, _ = self.run_enrollment(['ok'], seconds=0)
        self.assertFalse(result[0])
        self.assertEqual(school.calls, [])


class CourseTests(unittest.TestCase):
    def test_same_name_requires_exact_id(self):
        with patch('builtins.input', return_value='43'), patch('builtins.print'):
            selected = bykc.choose_course(FakeSchool([]), name='航空与艺术')
        self.assertEqual(selected['id'], 43)

    def test_partial_name_does_not_match(self):
        with self.assertRaises(bykc.UserError):
            bykc.choose_course(FakeSchool([]), name='航空')

    def test_id_does_not_match_substrings(self):
        with self.assertRaises(bykc.UserError):
            bykc.choose_course(FakeSchool([]), course_id='4')

    def test_lottery_is_not_treated_as_direct_enrollment(self):
        school = FakeSchool([])
        school.courses = lambda: iter([course(courseSelectType='2')])
        with patch('builtins.print'), self.assertRaises(bykc.UserError):
            bykc.choose_course(school, course_id='42')

    def test_school_paginates_and_checks_selected_by_id(self):
        page = type('Page', (), {'url': bykc.HOME})()
        page.evaluate = lambda _script, arg: {
            1: {'kind': 'ok', 'pages': 2, 'courses': [course(id=1, selected=True)]},
            2: {'kind': 'ok', 'pages': 2, 'courses': [course(selected=True)]},
        }[arg[1]]
        school = bykc.School(page)
        self.assertTrue(school.verify(42))
        self.assertFalse(school.verify(99))

    def test_non_school_page_is_rejected(self):
        page = type('Page', (), {'url': 'https://evil.example/system/home'})()
        with self.assertRaises(bykc.UserError):
            bykc.School(page).call('submit', 42)


class WorkflowTests(unittest.TestCase):
    def workflow(self, dry_run=False, mutation=None):
        clock = Clock()
        clock.value -= 20
        submissions = []
        closed = []
        page = SimpleNamespace(url=bykc.HOME)
        page.goto = lambda *_a, **_k: None

        def evaluate(script, args=None):
            if args is None:
                return True
            method, arg = args
            if method == 'list':
                c = course()
                if mutation and clock.value >= BASE.timestamp()-15:
                    c.update(mutation)
                return {'kind': 'ok', 'pages': 1, 'courses': [c]}
            if method == 'eligibility':
                return {'kind': 'ok'}
            if method == 'submit':
                submissions.append((arg, clock.value))
                return {'kind': 'ok'}
            raise AssertionError(method)

        page.evaluate = evaluate
        context = SimpleNamespace(route=lambda *_a: None, new_page=lambda: page,
                                  close=lambda: closed.append('context'))
        browser = SimpleNamespace(new_context=lambda **_k: context,
                                  close=lambda: closed.append('browser'))
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_k: browser))
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value = playwright
        module = SimpleNamespace(sync_playwright=lambda: manager)
        args = argparse.Namespace(browser='msedge', course='航空与艺术', course_id='',
                                  start='', duration=60, interval=.5, dry_run=dry_run)
        real_wait, real_enroll = bykc.wait_until, bykc.enroll
        replacements = {
            'now': lambda: datetime.fromtimestamp(clock.value, bykc.SHANGHAI),
            'wait_until': lambda t: real_wait(t, clock=clock.read, sleep=clock.sleep, report=lambda _: None),
            'enroll': lambda s, cid, p: real_enroll(s, cid, p, wall=clock.read,
                                                  monotonic=clock.read, sleep=clock.sleep, report=lambda _: None),
        }
        with patch.dict('sys.modules', {'playwright.sync_api': module}), \
                patch.multiple(bykc, **replacements), patch.dict('os.environ', {}, clear=True), \
                patch('builtins.input', side_effect=['', '确认']), patch('builtins.print'):
            try:
                result = bykc.run(args)
            except bykc.UserError as exc:
                result = (False, str(exc))
        return result, submissions, closed

    def test_full_interactive_flow_submits_at_opening_and_closes_session(self):
        result, submissions, closed = self.workflow()
        self.assertTrue(result[0])
        self.assertEqual(submissions, [(42, BASE.timestamp())])
        self.assertEqual(closed, ['context', 'browser'])

    def test_dry_run_never_submits(self):
        result, submissions, closed = self.workflow(dry_run=True)
        self.assertIsNone(result[0])
        self.assertEqual(submissions, [])
        self.assertEqual(closed, ['context', 'browser'])

    def test_changed_course_mode_stops_in_preflight(self):
        result, submissions, closed = self.workflow(mutation={'courseSelectType': '2'})
        self.assertFalse(result[0])
        self.assertEqual(submissions, [])
        self.assertEqual(closed, ['context', 'browser'])

    def test_raw_exception_never_leaks_to_console(self):
        with patch.object(bykc, 'run', side_effect=RuntimeError('ticket=SECRET')), \
                patch('builtins.print') as output:
            self.assertEqual(bykc.main(['--no-pause']), 1)
        text = '\n'.join(str(call) for call in output.call_args_list)
        self.assertIn('抢课失败', text)
        self.assertNotIn('SECRET', text)


if __name__ == '__main__':
    unittest.main()
