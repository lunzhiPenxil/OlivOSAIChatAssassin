"""Bootstrap for the OlivOS plugin tests."""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
ORIGINAL_CWD = Path.cwd()
BOOTSTRAP_DIRECTORY = None


def _resolve_olivos_source():
    candidates = []
    configured = os.environ.get('OLIVOS_SOURCE')
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        REPO_ROOT / '.ci' / 'OlivOS',
        REPO_ROOT.parent / 'OlivOS-dev',
        REPO_ROOT.parent / 'OlivOS',
    ])
    for candidate in candidates:
        if (candidate / 'OlivOS' / '__init__.py').is_file():
            return candidate.resolve()
    spec = importlib.util.find_spec('OlivOS')
    if spec and spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations))).resolve().parent
    return None


OLIVOS_SOURCE = _resolve_olivos_source()
for entry in (str(TESTS_ROOT), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
if OLIVOS_SOURCE is not None and str(OLIVOS_SOURCE) not in sys.path:
    sys.path.insert(0, str(OLIVOS_SOURCE))


def pytest_configure(config):
    if OLIVOS_SOURCE is None:
        raise pytest.UsageError(
            '未找到 OlivOS 源码。请设置 OLIVOS_SOURCE，或把主机仓库 checkout 到 .ci/OlivOS。'
        )


def pytest_sessionstart(session):
    global BOOTSTRAP_DIRECTORY
    BOOTSTRAP_DIRECTORY = tempfile.TemporaryDirectory(prefix='olivos-assassin-tests-')
    os.chdir(BOOTSTRAP_DIRECTORY.name)


def pytest_collection_finish(session):
    os.chdir(ORIGINAL_CWD)


def pytest_sessionfinish(session, exitstatus):
    os.chdir(ORIGINAL_CWD)
    if BOOTSTRAP_DIRECTORY is not None:
        BOOTSTRAP_DIRECTORY.cleanup()


@pytest.fixture(autouse=True)
def isolated_installation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ('OLIVOS_WEBUI_HOST', 'OLIVOS_WEBUI_PORT'):
        monkeypatch.delenv(name, raising=False)
