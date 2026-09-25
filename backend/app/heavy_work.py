"""Fail-fast, per-user admission for local reconstruction and Blender work.

Keep the lock inode: never unlink it, and never explicitly unlock an inherited
descriptor. Native workers inherit the open file description, so even a killed
Python supervisor cannot admit another job until its native worker exits.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
from datetime import datetime, timezone
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import weakref
from typing import Callable, Iterator, ParamSpec, TypeVar


class HeavyWorkBusy(RuntimeError):
    """Admission denied; nothing has been queued or started."""


class HeavyWorkUnavailable(HeavyWorkBusy):
    """Admission cannot be checked safely; use the same fail-closed lifecycle."""


_current: ContextVar[tuple[int, tuple[int, int, int | None]] | None] = ContextVar(
    'scanner_heavy_work_fd', default=None
)
P = ParamSpec('P')
T = TypeVar('T')


def _identity() -> tuple[int, int, int | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), id(task) if task is not None else None


def lock_path() -> Path:
    """All launchers use this same inode; override only for tests/deployment."""
    return Path(os.environ.get('SCANNER_HEAVY_LOCK', str(
        Path.home() / '.local/state/scanner/heavy-work.lock'
    ))).absolute()


def _current_worker_unit() -> str | None:
    """Derive our unit from kernel membership, never from a user-supplied name."""
    try:
        groups = Path('/proc/self/cgroup').read_text().splitlines()
    except OSError:
        return None
    for group in groups:
        path = group.split(':', 2)[-1]
        for part in reversed(path.split('/')):
            if re.fullmatch(r'scanner-(?:reconstruct|blender)-[A-Za-z0-9_-]+\.service', part):
                return part
    return None


def check_legacy_workers(*, excluding: str | None = None) -> None:
    """Read-only migration check for old user units that predate this lock.

    On hosts without a user systemd manager the flock still protects all new
    launchers. An available but unreadable manager is not treated as idle.
    """
    if not Path(f'/run/user/{os.getuid()}/systemd/private').exists():
        return
    own_unit = _current_worker_unit()
    if excluding is not None and excluding != own_unit:
        raise HeavyWorkBusy('Worker service identity does not match this process cgroup.')
    if shutil.which('systemctl') is None:
        raise HeavyWorkBusy('Cannot inspect legacy Scanner workers: systemctl is unavailable.')
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'list-units', 'scanner-reconstruct-*.service',
             'scanner-blender-*.service', '--state=active,activating,reloading,deactivating',
             '--plain', '--no-legend', '--no-pager'],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HeavyWorkBusy(f'Cannot inspect legacy Scanner workers: {error}') from error
    if result.returncode:
        raise HeavyWorkBusy('Cannot inspect legacy Scanner workers; check the user service manager.')
    units = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    competing = [unit for unit in units if unit != own_unit]
    if competing:
        raise HeavyWorkBusy('Another Scanner worker is active: ' + ', '.join(competing)
                            + '. Wait for it to finish, then retry manually.')


class Reservation:
    """One-shot admission transferable from an HTTP route to its worker thread."""
    def __init__(self, fd: int):
        self._fd = fd
        self._close = weakref.finalize(self, os.close, fd)
        self._mutex = threading.Lock()
        self._started = False

    def close(self) -> None:
        # close, never LOCK_UN: native children may still own this description.
        self._close()

    @contextmanager
    def activate(self) -> Iterator[None]:
        with self._mutex:
            if self._started or not self._close.alive:
                raise HeavyWorkBusy('This Scanner admission reservation was already consumed.')
            self._started = True
        token = _current.set((self._fd, _identity()))
        try:
            yield
        finally:
            _current.reset(token)
            self.close()

    def run(self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        with self.activate():
            return function(*args, **kwargs)


def reserve_heavy_work(label: str, *, excluding_unit: str | None = None) -> Reservation:
    """Claim now, before irreversible review approval or scheduling a worker."""
    try:
        return _reserve_heavy_work(label, excluding_unit=excluding_unit)
    except OSError as error:
        raise HeavyWorkUnavailable(f'Scanner admission is unavailable: {error}') from error


def _reserve_heavy_work(label: str, *, excluding_unit: str | None) -> Reservation:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise HeavyWorkUnavailable('Shared Scanner admission file must be a regular file owned by this user.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            try:
                owner = json.loads(os.pread(fd, 8192, 0))
                detail = f"{owner['label']} (supervisor PID {owner['pid']}, started {owner['started_at']})"
            except (ValueError, KeyError, TypeError):
                detail = 'another Scanner workflow or its surviving native child'
            raise HeavyWorkBusy(f'Scanner is busy: {detail}. Wait for it to finish, then retry manually.') from error
        check_legacy_workers(excluding=excluding_unit)
        owner = json.dumps({'label': label, 'pid': os.getpid(),
                            'started_at': datetime.now(timezone.utc).isoformat(),
                            'unit': excluding_unit}).encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, owner, 0)
        return Reservation(fd)
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def heavy_work(label: str, *, excluding_unit: str | None = None) -> Iterator[None]:
    """Own a whole synchronous workflow; nested stages reuse its admission."""
    inherited = _current.get()
    if inherited is not None and inherited[1] == _identity():
        yield
        return
    reservation = reserve_heavy_work(label, excluding_unit=excluding_unit)
    with reservation.activate():
        yield


def guarded(label: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Admit a standalone pipeline, or reuse its enclosing job's admission."""
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with heavy_work(label):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def native_kwargs() -> dict[str, tuple[int, ...]]:
    """Pass these kwargs to every native child inside a heavy workflow."""
    lease = _current.get()
    if lease is None:
        return {}
    if lease[1] != _identity():
        raise HeavyWorkBusy('Native work needs its own workflow admission, not an inherited task context.')
    return {'pass_fds': (lease[0],)}
