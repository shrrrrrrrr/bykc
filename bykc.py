"""Local, single-account scheduled enrollment. No credential input or persistence."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
HOME = 'https://bykc.buaa.edu.cn/system/home'
BRIDGE = Path(__file__).with_name('site_bridge.js').read_text(encoding='utf-8')
REASONS = {
    'auth': '登录已失效，请重新运行并登录。',
    'profile': '请先在官网完善个人信息。',
    'limited': '网站要求验证或限制访问，已停止自动提交。',
    'denied': '学校规则不允许报名（资格、学期额度、时间或课程冲突等）。',
    'changed': '网站结构已变化，需要更新程序。',
    'rejected': '学校拒绝报名，请在官方页面查看原因。',
    'uncertain': '请求结果暂不明确，已停止重复提交；请核对官网已选课程。',
    'busy': '网站繁忙或网络异常。',
    'full': '课程已满，等待本次重试窗口结束。',
    'not_open': '学校暂未开放该课程。',
    'already': '学校提示重复报名，正在核验。',
}


class UserError(Exception):
    pass


def now() -> datetime:
    return datetime.now(SHANGHAI)


def parse_time(value, reference=None) -> datetime:
    """Website timestamps or explicit user input, always interpreted as Beijing time."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, SHANGHAI)
    text = str(value).strip()
    if len(text) in (5, 8) and text[2] == ':':
        text = (reference or now()).strftime('%Y-%m-%d') + ' ' + text
    try:
        result = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise UserError('时间格式应为 YYYY-MM-DD HH:MM:SS 或 HH:MM:SS。') from None
    return result.replace(tzinfo=SHANGHAI) if result.tzinfo is None else result.astimezone(SHANGHAI)


def fmt(value) -> str:
    return parse_time(value).strftime('%Y-%m-%d %H:%M:%S')


@dataclass(frozen=True)
class Plan:
    start: datetime
    end: datetime
    interval: float = 0.5
    max_attempts: int = 120


def make_plan(course, start_text='', duration=60.0, interval=0.5, current=None) -> Plan:
    current = current or now()
    opening = parse_time(course['courseSelectStartDate'])
    closing = parse_time(course['courseSelectEndDate'])
    start = parse_time(start_text, current) if start_text else max(opening, current)
    if not all(math.isfinite(v) for v in (duration, interval)):
        raise UserError('时长和间隔必须是有限数字。')
    if not 1 <= duration <= 600 or not 0.5 <= interval <= 30:
        raise UserError('重试时长须为 1–600 秒，间隔须为 0.5–30 秒。')
    if start < opening:
        raise UserError('设定时间早于官网选课开放时间，不会提前提交。')
    if start_text and start < current - timedelta(seconds=2):
        raise UserError('设定时间已经过去；请检查日期（不会自动改为明天）。')
    end = min(start + timedelta(seconds=duration), closing)
    if end <= current or end <= start:
        raise UserError('该课程选课时间已经结束。')
    return Plan(start, end, interval, 120)


class School:
    def __init__(self, page):
        self.page = page

    def call(self, method, arg=None):
        if urlsplit(self.page.url).scheme != 'https' or urlsplit(self.page.url).hostname != 'bykc.buaa.edu.cn':
            raise UserError(REASONS['auth'])
        return self.page.evaluate('([method, arg]) => window.__bykcLocal[method](arg)', [method, arg])

    def courses(self):
        page, pages = 1, 1
        while page <= pages:
            result = self.call('list', page)
            if result.get('kind') != 'ok':
                raise UserError(REASONS.get(result.get('kind'), REASONS['changed']))
            pages = result['pages']
            if pages < 0 or pages > 1000:
                raise UserError(REASONS['changed'])
            yield from result['courses']
            page += 1

    def find(self, course_id):
        return next((c for c in self.courses() if str(c['id']) == str(course_id)), None)

    def verify(self, course_id):
        course = self.find(course_id)
        return course is not None and course.get('selected') is True

    def submit(self, course_id):
        return self.call('submit', course_id)['kind']

    def eligible(self, course):
        result = self.call('eligibility', course)
        if result.get('kind') != 'ok':
            raise UserError(REASONS.get(result.get('kind'), REASONS['changed']))


def wait_until(target, *, clock=time.time, sleep=time.sleep, report=print):
    """Recheck wall clock so ordinary clock corrections are honored before launch."""
    last = None
    while (remaining := target.timestamp() - clock()) > 0:
        seconds = math.ceil(remaining)
        if seconds != last and (seconds % 30 == 0 or seconds <= 10):
            report(f'距离提交还有 {seconds} 秒……')
            last = seconds
        sleep(min(1.0 if remaining > 2 else 0.01, remaining))


def enroll(school, course_id, plan, *, wall=time.time, monotonic=time.monotonic,
           sleep=time.sleep, report=print):
    """One outstanding write at a time. Unknown writes are reconciled, never replayed."""
    # Bound by monotonic time too, even if the user changes the system clock.
    deadline = monotonic() + max(0, plan.end.timestamp() - wall())
    uncertain = False
    for attempt in range(1, plan.max_attempts + 1):
        if wall() >= plan.end.timestamp() or monotonic() >= deadline:
            break
        if wall() < plan.start.timestamp():
            sleep(min(0.05, plan.start.timestamp() - wall()))
            continue
        started = monotonic()
        if uncertain:
            if school.verify(course_id):
                return True, '官网已确认该课程在已选列表中。'
        else:
            kind = school.submit(course_id)
            if kind == 'ok':
                return True, '官网报名接口已明确返回成功。'
            if kind in ('already', 'uncertain'):
                uncertain = True
                if school.verify(course_id):
                    return True, '官网已确认该课程在已选列表中。'
            elif kind not in ('busy', 'full', 'not_open'):
                return False, REASONS.get(kind, REASONS['rejected'])
            if attempt == 1 or attempt % 10 == 0:
                report(f'第 {attempt} 次提交：{REASONS.get(kind, REASONS["rejected"])}')
        delay = max(0, (2 if uncertain else plan.interval) - (monotonic() - started))
        remaining = min(plan.end.timestamp() - wall(), deadline - monotonic())
        if remaining > 0:
            sleep(min(delay, remaining))
    # A successful write may have raced the last response; do one read-only check.
    try:
        if school.verify(course_id):
            return True, '最终核验确认已选中该课程。'
    except UserError:
        return False, REASONS['uncertain']
    return False, REASONS['uncertain'] if uncertain else '重试窗口结束或达到 120 次上限，未选中课程。'


def describe(course):
    print(f'\n课程 ID：{course["id"]}  名称：{course["courseName"]}')
    print(f'教师：{course.get("courseTeacher") or "（官网未提供）"}')
    print(f'校区：{"、".join(course.get("courseCampusList") or [])}  地点：{course.get("coursePosition", "")}')
    print(f'上课时间：{fmt(course["courseStartDate"])}')
    print(f'选课时间：{fmt(course["courseSelectStartDate"])} 至 {fmt(course["courseSelectEndDate"])}')
    print(f'人数：{course.get("courseCurrentCount", "?")}/{course.get("courseMaxCount", "?")}')


def choose_course(school, name='', course_id=''):
    if not name and not course_id:
        text = input('完整课程名称（或输入 ID:课程编号）：').strip()
        if text.upper().startswith('ID:'):
            course_id = text[3:].strip()
        else:
            name = text
    if not name and not course_id:
        raise UserError('课程名称或 ID 不能为空。')
    matches = [c for c in school.courses()
               if (str(c['id']) == course_id if course_id else str(c['courseName']).strip() == name)]
    if not matches:
        raise UserError('未找到课程：名称必须完整一致，且账号须有权限看到该课程。')
    for course in matches:
        describe(course)
    if len(matches) > 1:
        course_id = input('存在同名课程，请输入要选的课程 ID：').strip()
        matches = [c for c in matches if str(c['id']) == course_id]
    if len(matches) != 1:
        raise UserError('课程 ID 无效，未启动选课。')
    course = matches[0]
    if str(course.get('courseSelectType')) != '1':
        raise UserError('该课程并非直接选课（可能是预报名抽签），本程序不将抽签报名报告为选课成功。')
    return course


@contextmanager
def keep_awake():
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
    try:
        yield
    finally:
        if os.name == 'nt':
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def run(args):
    if any(os.environ.get(key) for key in ('DEBUG', 'PWDEBUG', 'PW_LOG')):
        raise UserError('请先移除 DEBUG/PWDEBUG/PW_LOG 环境变量，防止调试日志泄露登录信息。')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise UserError('尚未安装依赖，请先按 README 运行安装命令。') from None
    with sync_playwright() as p:
        options = {'headless': False, 'args': ['--disable-background-timer-throttling',
                   '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows']}
        if args.browser != 'chromium':
            options['channel'] = args.browser
        browser = p.chromium.launch(**options)
        context = browser.new_context(accept_downloads=False, timezone_id='Asia/Shanghai',
                                      locale='zh-CN', service_workers='block')
        try:
            def allowed(route):
                parsed = urlsplit(route.request.url)
                host = parsed.hostname or ''
                if parsed.scheme == 'https' and (host == 'buaa.edu.cn' or host.endswith('.buaa.edu.cn')):
                    route.continue_()
                else:
                    route.abort()
            context.route('**/*', allowed)
            page = context.new_page()
            page.goto(HOME, wait_until='domcontentloaded', timeout=45000)
            print('\n请在弹出的官方浏览器页面登录，并完成可能出现的验证码。')
            print('账号密码只填在学校 HTTPS 页面，勿填入 PowerShell；不要关闭浏览器。')
            input('看到选课系统首页后，回到此处按回车继续：')
            if not page.evaluate(BRIDGE) or not page.evaluate('() => window.__bykcLocal.ready()'):
                raise UserError('未检测到官网已登录会话，或网站版本已变化。')
            school = School(page)
            course = choose_course(school, args.course, args.course_id)
            if course.get('selected') is True:
                return True, '官网显示你已选中该课程，本次没有再次提交。'
            school.eligible(course)
            start = args.start
            if start is None:
                start = input('提交时间（北京时间；回车采用官网开放时间，已开放则立即）：').strip()
            plan = make_plan(course, start, args.duration, args.interval)
            print(f'\n当前北京时间：{now():%Y-%m-%d %H:%M:%S}')
            print(f'首次提交时间：{plan.start:%Y-%m-%d %H:%M:%S}；停止提交：{plan.end:%Y-%m-%d %H:%M:%S}')
            print(f'单账号、单课程，间隔至少 {plan.interval} 秒，最多 {plan.max_attempts} 次。')
            if input('确认以上课程及时间，输入“确认”开始等待：').strip() != '确认':
                raise UserError('用户未确认，未启动选课。')
            if now() >= plan.end:
                raise UserError('确认时重试窗口已结束，请重新运行。')
            if args.dry_run:
                return None, '预演完成：登录、课程和时间检查通过，没有提交报名请求。'
            with keep_awake():
                # Read-only refresh shortly before launch; never on the critical instant.
                warmup = plan.start - timedelta(seconds=15)
                wait_until(warmup)
                refreshed = school.find(course['id'])
                identity_fields = ('courseName', 'courseSelectType', 'courseStartDate',
                                   'courseTeacher', 'courseCampusList', 'coursePosition')
                if refreshed is None or any(refreshed.get(k) != course.get(k) for k in identity_fields):
                    raise UserError('等待期间课程信息已变化，请重新确认。')
                if refreshed.get('selected') is True:
                    return True, '官网已确认选中课程。'
                if (refreshed.get('courseSelectStartDate') != course.get('courseSelectStartDate') or
                        refreshed.get('courseSelectEndDate') != course.get('courseSelectEndDate')):
                    raise UserError('官网调整了选课时间，请重新运行并确认。')
                school.eligible(refreshed)
                if now() >= plan.end:
                    raise UserError('预检结束时已超过重试窗口。')
                print('已准备就绪，请保持电脑联网、浏览器和 PowerShell 运行。Ctrl+C 可取消。')
                wait_until(plan.start)
                print('开始提交选课请求……')
                return enroll(school, course['id'], plan)
        finally:
            # A cleanup failure must not erase an authoritative enrollment result.
            with suppress(Exception):
                context.close()
            with suppress(Exception):
                browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='北航博雅课程本地定时选课助手（北京时间）')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--course', default='', help='完整课程名称')
    group.add_argument('--course-id', default='', help='官网课程 ID')
    parser.add_argument('--start', help='YYYY-MM-DD HH:MM:SS 或 HH:MM:SS；默认交互输入')
    parser.add_argument('--duration', type=float, default=60, help='重试窗口秒数，1–600，默认60')
    parser.add_argument('--interval', type=float, default=0.5, help='提交最小间隔秒数，0.5–30，默认0.5')
    parser.add_argument('--browser', choices=('msedge', 'chrome', 'chromium'), default='msedge')
    parser.add_argument('--dry-run', action='store_true', help='只登录、查课和检查时间，不提交')
    parser.add_argument('--no-pause', action='store_true', help='结束后不等待回车')
    args = parser.parse_args(argv)
    code = 1
    try:
        success, detail = run(args)
        print('\n' + ('预演完成' if success is None else '选课成功' if success else '抢课失败'))
        print(detail)
        code = 0 if success is not False else 1
    except (KeyboardInterrupt, EOFError):
        print('\n抢课失败\n操作已取消。如提交期间取消，请在官网核对已选课程。')
        code = 130
    except UserError as e:
        print(f'\n抢课失败\n{e}')
    except Exception:
        # Raw browser errors may contain SSO ticket URLs or request contents.
        print('\n抢课失败\n浏览器或网络发生异常。请检查依赖、网络及官网已选课程后重新运行。')
    finally:
        if not args.no_pause and sys.stdin.isatty():
            try:
                input('\n程序已停止。按回车结束，或自行关闭此窗口……')
            except (KeyboardInterrupt, EOFError):
                pass
    return code


if __name__ == '__main__':
    raise SystemExit(main())
