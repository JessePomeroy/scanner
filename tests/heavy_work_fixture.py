"""Test-owned admission state; never inspect live Scanner units from unit tests."""
from contextlib import contextmanager
import importlib
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


@contextmanager
def isolated_heavy_work():
    # Desktop-only discovery does not import the API tests that add this path.
    backend = str(Path(__file__).resolve().parents[1] / 'backend')
    if backend not in sys.path:
        sys.path.insert(0, backend)
    with tempfile.TemporaryDirectory(prefix='scanner-test-admission-') as temporary:
        with patch.dict(os.environ, {'SCANNER_HEAVY_LOCK': str(Path(temporary) / 'heavy.lock')}):
            # API/CLI use app.*; desktop uses backend.app.*. These are separate
            # module identities in an in-process suite, but share the real inode.
            from contextlib import ExitStack
            with ExitStack() as stack:
                for name in ('app.heavy_work', 'backend.app.heavy_work'):
                    module = importlib.import_module(name)
                    stack.enter_context(patch.object(module, 'check_legacy_workers'))
                yield
