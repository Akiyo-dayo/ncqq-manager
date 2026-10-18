"""Explicit, opt-in SnowLuma login repair jobs. Never called by QR polling."""
import asyncio
from contextlib import contextmanager
import functools
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

_guard = threading.Lock()
_locks = {}
_jobs = {}
_active = {}


@contextmanager
def account_lock(container, program):
    with _guard:
        lock = _locks.setdefault((container, program), threading.RLock())
    if not lock.acquire(blocking=False):
        raise RuntimeError('该账号有登录/修复操作正在进行，请稍后重试')
    try:
        yield
    finally:
        lock.release()


@contextmanager
def display_lock(container):
    # Each shared X display and extractor scratch filenames are container-global.
    with _guard:
        lock = _locks.setdefault((container, '__display__'), threading.RLock())
    if not lock.acquire(timeout=90):
        raise RuntimeError('共享登录桌面忙，请稍后重试')
    try:
        yield
    finally:
        lock.release()


def serialized(fn):
    @functools.wraps(fn)
    def wrapped(name, *args, **kwargs):
        from services import snowluma as sl
        item = sl.accounts().get(name)
        if not item:
            return fn(name, *args, **kwargs)
        with account_lock(item['container'], item['program']):
            with display_lock(item['container']):
                return fn(name, *args, **kwargs)
    return wrapped


def get_job(name):
    with _guard:
        job = _jobs.get(_active.get(name))
        return dict(job) if job else None


def active(name):
    job = get_job(name)
    return job and job['phase'] not in ('succeeded', 'failed')


def _update(job, **values):
    with _guard:
        job.update(values, updated_at=time.time())


def _helper(target, mode, item, token=None):
    args = ['python3', '/tmp/snowluma-repair.py', mode, item['program'], item['uin']]
    if token:
        args.append(token)
    result = target.exec_run(args, demux=True)
    out = result.output[0] if isinstance(result.output, tuple) else result.output
    try:
        data = json.loads((out or b'').decode().strip().splitlines()[-1])
    except Exception:
        raise RuntimeError('修复助手未返回有效结果')
    if result.exit_code:
        raise RuntimeError(data.get('error', '修复助手失败'))
    return data


def _run(job):
    from services import snowluma as sl
    try:
        item = sl.accounts()[job['name']]
        with account_lock(item['container'], item['program']), display_lock(item['container']):
            _run_locked(job)
    except Exception as exc:
        _update(job, phase='failed', error=str(exc))


def _run_locked(job):
    from services import snowluma as sl
    from services import snowluma_state
    token = None
    try:
        item = sl.accounts()[job['name']]
        with account_lock(item['container'], item['program']), display_lock(item['container']):
            _update(job, phase='checking')
            target = sl._container_client(item['container'])
            sl._ensure_extractor(item['container'])
            sl._states_cache.pop(item['container'], None)
            sl._win_cache['ts'] = 0
            snowluma_state._cache.pop(item['container'], None)
            stage = sl._account_login_stage(item)
            if stage != 'waiting':
                raise RuntimeError('仅允许修复明确处于待登录界面的账号；当前状态：' + stage)
            # A working login surface needs no reset. A transient QR failure alone
            # never starts this job: explicit user confirmation is required first.
            qr = sl.fetch_qr(job['name'])
            if qr.get('status') in ('ok', 'logged_in') or qr.get('oneclick'):
                _update(job, phase='succeeded', result='already_available', message='登录界面已可用，未重置数据')
                return
            if qr.get('source') != 'snowluma_qr_unavailable':
                raise RuntimeError('取码失败原因不是登录环境不可用，拒绝重置')
            # Recheck immediately before stopping: login may have completed while
            # the refresh was in flight.
            sl._states_cache.pop(item['container'], None)
            sl._win_cache['ts'] = 0
            snowluma_state._cache.pop(item['container'], None)
            if sl._account_login_stage(item) != 'waiting':
                raise RuntimeError('登录状态已变化，取消修复')
            source = Path(__file__).with_name('snowluma_repair_helper.py')
            sl._put_bytes(target, 'snowluma-repair.py', source.read_bytes())
            _update(job, phase='backup')
            data = _helper(target, 'begin', item)
            token = data['transaction']
            _update(job, phase='verifying', backup=data['backup'])
            deadline = time.monotonic() + 120
            verified = None
            while time.monotonic() < deadline:
                sl._states_cache.pop(item['container'], None)
                sl._win_cache['ts'] = 0
                snowluma_state._cache.pop(item['container'], None)
                qr = sl.fetch_qr(job['name'])
                if qr.get('status') in ('ok', 'logged_in') or qr.get('oneclick'):
                    verified = qr.get('source')
                    break
                time.sleep(3)
            if not verified:
                raise RuntimeError('新登录环境验证超时')
            result = _helper(target, 'finish', item, token)
            token = None
            _update(job, result=verified, backup=result['backup'],
                    other_program_changes=result['other_program_changes'])
            # finish() phase is an on-disk transaction phase, not the job outcome.
            _update(job, phase='succeeded', message='登录环境已恢复；二维码可用不代表已登录')
    except Exception as exc:
        error = str(exc)
        if token:
            _update(job, phase='rolling_back')
            try:
                _helper(target, 'rollback', item, token)
                error += '；已恢复原配置'
            except Exception as rollback_error:
                error += '；回滚失败：' + str(rollback_error)
        _update(job, phase='failed', error=error)
    finally:
        try:
            sl._states_cache.pop(item['container'], None)
            sl._win_cache['ts'] = 0
            from services.state_engine import state_engine
            state_engine.notify_change()
        except Exception:
            pass
        from services.operation_logger import operation_logger
        operation_logger.info('snowluma_login_repair_result', {
            k: job.get(k) for k in ('name', 'operation_id', 'phase', 'backup', 'error', 'result')
        })


_tasks = set()


def start(name):
    with _guard:
        previous = _jobs.get(_active.get(name))
        if previous and previous['phase'] not in ('succeeded', 'failed'):
            return dict(previous)
        # Bounded history: per-account latest result is enough for recovery polling.
        old = _active.get(name)
        if old:
            _jobs.pop(old, None)
        job = dict(name=name, operation_id=uuid4().hex, phase='queued', started_at=time.time(), error='')
        _jobs[job['operation_id']] = job
        _active[name] = job['operation_id']
    task = asyncio.create_task(asyncio.to_thread(_run, job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return dict(job)
