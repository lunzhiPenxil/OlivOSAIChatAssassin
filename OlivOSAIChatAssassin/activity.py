import re
import secrets
import threading
import time
from datetime import datetime


ACTIVITY_PREVIEW_LIMIT = 96
ACTIVITY_SENDER_LIMIT = 32
ACTIVITY_SNAPSHOT_LIMIT = 50
ACTIVITY_COMPLETION_TTL_MS = 3000
ACTIVITY_COMPLETED_LIMIT = 50

STAGE_QUEUED = 'queued'
STAGE_PREPARING = 'preparing'
STAGE_RECALLING = 'recalling'
STAGE_PACING = 'pacing'
STAGE_JUDGING = 'judging'
STAGE_MODEL = 'model'
STAGE_REPAIRING = 'repairing'
STAGE_BACKOFF = 'backoff'
STAGE_SENDING = 'sending'
STAGE_MEMORY_EXTRACT = 'memory_extract'

STAGES = frozenset((
    STAGE_QUEUED,
    STAGE_PREPARING,
    STAGE_RECALLING,
    STAGE_PACING,
    STAGE_JUDGING,
    STAGE_MODEL,
    STAGE_REPAIRING,
    STAGE_BACKOFF,
    STAGE_SENDING,
    STAGE_MEMORY_EXTRACT,
))

STATE_QUEUED = 'queued'
STATE_RUNNING = 'running'
STATE_SKIPPED = 'skipped'
STATE_SUPERSEDED = 'superseded'
STATE_FAILED = 'failed'
STATE_FINISHED = 'finished'

STATES = frozenset((
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SKIPPED,
    STATE_SUPERSEDED,
    STATE_FAILED,
    STATE_FINISHED,
))

_MESSAGE_MARKER_RE = re.compile(r'\[(?:CQ|OP):[^\]]*\]', re.IGNORECASE)
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_INVISIBLE_RE = re.compile(r'[\u200b-\u200f\u202a-\u202e\u2066-\u2069]')
_WHITESPACE_RE = re.compile(r'\s+')
_ERROR_CODE_RE = re.compile(r'^[a-z0-9_]{1,64}$')


def _now_iso():
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _clean_text(value, limit):
    if not isinstance(value, str):
        return ''
    text = _MESSAGE_MARKER_RE.sub(' ', value)
    text = _CONTROL_RE.sub(' ', text)
    text = _INVISIBLE_RE.sub('', text)
    text = _WHITESPACE_RE.sub(' ', text).strip()
    if len(text) > limit:
        return text[:max(0, limit - 1)].rstrip() + '…'
    return text


def sanitize_preview(value):
    return _clean_text(value, ACTIVITY_PREVIEW_LIMIT)


def sanitize_sender(value):
    return _clean_text(value, ACTIVITY_SENDER_LIMIT)


def _clean_error_code(value):
    if isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value):
        return value
    return ''


class ActivityToken:
    def __init__(self, token_id, bot_hash, group_id, session_id=None):
        self.token_id = token_id
        self.bot_hash = bot_hash
        self.group_id = group_id
        self.session_id = session_id
        self.started_monotonic = time.monotonic()
        self.started_at = _now_iso()


class _ActivitySession:
    def __init__(self, session_id, bot_hash, group_id, sequence):
        self.session_id = session_id
        self.bot_hash = bot_hash
        self.group_id = group_id
        self.group_label = f'群 {group_id}'
        self.sequence = sequence
        self.current_token_id = None
        self.pending_tokens = {}
        self.completed_monotonic = 0.0
        self.expires_monotonic = 0.0
        self.state = STATE_QUEUED
        self.stage = STAGE_QUEUED
        self.preview = ''
        self.sender_name = ''
        self.attempt = 0
        self.retry_count = 0
        self.error_code = ''
        now = time.monotonic()
        self.started_monotonic = now
        self.updated_monotonic = now
        self.stage_started_monotonic = now
        self.started_at = _now_iso()
        self.updated_at = self.started_at

    def queue_depth(self):
        return len(self.pending_tokens)


_lock = threading.RLock()
_sessions = {}
_completed = {}
_next_token_id = 0
_next_session_sequence = 0


def _next_id(name):
    global _next_token_id, _next_session_sequence
    if name == 'token':
        _next_token_id += 1
        return _next_token_id
    _next_session_sequence += 1
    return _next_session_sequence


def _new_token(bot_hash, group_id):
    with _lock:
        token_id = _next_id('token')
    return ActivityToken(token_id, bot_hash, group_id)


def _session_for_token(token):
    if not isinstance(token, ActivityToken) or not token.session_id:
        return None
    session = _sessions.get((token.bot_hash, token.group_id))
    if session is None or session.session_id != token.session_id:
        return None
    return session


def _refresh_pending_summary(session):
    if not session.pending_tokens:
        return
    latest = list(session.pending_tokens.values())[-1]
    session.preview = latest['preview']
    session.sender_name = latest['sender_name']


def _prune_completed(now):
    expired_keys = [
        key for key, session in _completed.items()
        if session.expires_monotonic <= now
    ]
    for key in expired_keys:
        _completed.pop(key, None)
    overflow = len(_completed) - ACTIVITY_COMPLETED_LIMIT
    if overflow <= 0:
        return
    oldest_keys = sorted(
        _completed,
        key=lambda key: _completed[key].expires_monotonic,
    )[:overflow]
    for key in oldest_keys:
        _completed.pop(key, None)


def _remember_completion(session, token, now):
    completed = _ActivitySession(
        f'{session.session_id}-done-{token.token_id}',
        session.bot_hash,
        session.group_id,
        _next_id('session'),
    )
    completed.state = STATE_FINISHED
    completed.stage = session.stage if session.stage in STAGES else STAGE_QUEUED
    completed.preview = session.preview
    completed.sender_name = session.sender_name
    completed.attempt = session.attempt
    completed.retry_count = session.retry_count
    completed.error_code = session.error_code
    completed.started_monotonic = session.started_monotonic
    completed.started_at = session.started_at
    completed.stage_started_monotonic = session.stage_started_monotonic
    completed.updated_monotonic = now
    completed.updated_at = _now_iso()
    completed.completed_monotonic = now
    completed.expires_monotonic = now + ACTIVITY_COMPLETION_TTL_MS / 1000.0
    _completed[(session.bot_hash, session.group_id)] = completed
    _prune_completed(now)


def _begin(bot_hash, group_id, sender_name, preview):
    token = _new_token(bot_hash, group_id)
    with _lock:
        now = time.monotonic()
        _prune_completed(now)
        key = (bot_hash, group_id)
        _completed.pop(key, None)
        session = _sessions.get(key)
        if session is None:
            sequence = _next_id('session')
            session_id = f'activity-{secrets.token_hex(8)}'
            session = _ActivitySession(session_id, bot_hash, group_id, sequence)
            _sessions[key] = session
        token.session_id = session.session_id
        token.started_monotonic = now
        token.started_at = _now_iso()
        safe_preview = sanitize_preview(preview)
        safe_sender = sanitize_sender(sender_name)
        session.pending_tokens[token.token_id] = {
            'preview': safe_preview,
            'sender_name': safe_sender,
        }
        session.updated_monotonic = now
        session.updated_at = token.started_at
        if session.current_token_id is None:
            session.state = STATE_QUEUED
            session.stage = STAGE_QUEUED
            session.preview = safe_preview
            session.sender_name = safe_sender
    return token


def begin(bot_hash, group_id, sender_name=None, preview=None):
    try:
        return _begin(str(bot_hash), str(group_id), sender_name, preview)
    except Exception:
        return ActivityToken(-1, str(bot_hash), str(group_id))


def _activate(token, stage, metadata):
    with _lock:
        session = _session_for_token(token)
        if session is None:
            return
        if (
            session.current_token_id is not None
            and session.current_token_id != token.token_id
        ):
            return
        queued = session.pending_tokens.pop(token.token_id, None)
        if session.current_token_id is None and queued is None:
            return
        now = time.monotonic()
        session.current_token_id = token.token_id
        session.state = STATE_RUNNING
        session.stage = stage if stage in STAGES else STAGE_PREPARING
        session.attempt = 0
        session.retry_count = 0
        session.error_code = ''
        session.started_monotonic = token.started_monotonic
        session.started_at = token.started_at
        session.stage_started_monotonic = now
        session.updated_monotonic = now
        session.updated_at = _now_iso()
        if queued is not None:
            session.preview = queued['preview']
            session.sender_name = queued['sender_name']
        if isinstance(metadata, dict):
            if 'preview' in metadata:
                session.preview = sanitize_preview(metadata.get('preview'))
            if 'sender_name' in metadata:
                session.sender_name = sanitize_sender(metadata.get('sender_name'))


def activate(token, stage=STAGE_PREPARING, metadata=None):
    try:
        _activate(token, stage, metadata)
    except Exception:
        return None
    return None


def _update(token, stage=None, attempt=None, retry_count=None, preview=None,
            sender_name=None):
    with _lock:
        session = _session_for_token(token)
        if session is None or session.current_token_id != token.token_id:
            return
        now = time.monotonic()
        if stage in STAGES and stage != session.stage:
            session.stage = stage
            session.stage_started_monotonic = now
        if type(attempt) is int and attempt >= 0:
            session.attempt = attempt
        if type(retry_count) is int and retry_count >= 0:
            session.retry_count = retry_count
        if preview is not None:
            session.preview = sanitize_preview(preview)
        if sender_name is not None:
            session.sender_name = sanitize_sender(sender_name)
        session.updated_monotonic = now
        session.updated_at = _now_iso()


def update(token, stage=None, attempt=None, retry_count=None, preview=None,
           sender_name=None):
    try:
        _update(token, stage, attempt, retry_count, preview, sender_name)
    except Exception:
        return None
    return None


def _finish(token, state, error_code):
    with _lock:
        now = time.monotonic()
        _prune_completed(now)
        session = _session_for_token(token)
        if session is None:
            return
        if token.token_id in session.pending_tokens:
            del session.pending_tokens[token.token_id]
            if session.current_token_id is None:
                _refresh_pending_summary(session)
            if not session.pending_tokens and session.current_token_id is None:
                _sessions.pop((token.bot_hash, token.group_id), None)
            return
        if session.current_token_id != token.token_id:
            return

        if state == STATE_FINISHED and not session.pending_tokens:
            _remember_completion(session, token, now)
        session.current_token_id = None
        session.state = state if state in STATES else STATE_FAILED
        session.stage = STAGE_QUEUED
        session.stage_started_monotonic = now
        session.updated_monotonic = now
        session.updated_at = _now_iso()
        session.attempt = 0
        session.retry_count = 0
        session.error_code = _clean_error_code(error_code)
        if session.pending_tokens:
            _refresh_pending_summary(session)
            session.state = STATE_QUEUED
        else:
            _sessions.pop((token.bot_hash, token.group_id), None)


def finish(token, state=STATE_FINISHED, error_code=None):
    try:
        _finish(token, state, error_code)
    except Exception:
        return None
    return None


def _snapshot_session(session, now):
    completed = session.completed_monotonic > 0.0
    queue_depth = 0 if completed else session.queue_depth()
    if completed:
        state = STATE_FINISHED
        stage = session.stage if session.stage in STAGES else STAGE_QUEUED
        elapsed_at = session.completed_monotonic
        expires_in_ms = max(0, int((session.expires_monotonic - now) * 1000))
    elif session.current_token_id is None:
        state = STATE_QUEUED
        stage = STAGE_QUEUED
        elapsed_at = now
        expires_in_ms = 0
    else:
        state = session.state if session.state in STATES else STATE_RUNNING
        stage = session.stage if session.stage in STAGES else STAGE_QUEUED
        elapsed_at = now
        expires_in_ms = 0
    return {
        'session_id': session.session_id,
        'group_id': session.group_id,
        'group_label': session.group_label,
        'state': state,
        'stage': stage,
        'message_preview': sanitize_preview(session.preview),
        'sender_name': sanitize_sender(session.sender_name),
        'queue_depth': queue_depth,
        'elapsed_ms': int(max(0.0, elapsed_at - session.started_monotonic) * 1000),
        'stage_elapsed_ms': int(max(0.0, elapsed_at - session.stage_started_monotonic) * 1000),
        'attempt': session.attempt,
        'retry_count': session.retry_count,
        'error_code': session.error_code,
        'updated_at': session.updated_at,
        'expires_in_ms': expires_in_ms,
    }


def snapshot(bot_hash, limit=ACTIVITY_SNAPSHOT_LIMIT):
    try:
        bot_hash = str(bot_hash)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = ACTIVITY_SNAPSHOT_LIMIT
        limit = max(0, min(limit, ACTIVITY_SNAPSHOT_LIMIT))
        now = time.monotonic()
        with _lock:
            _prune_completed(now)
            sessions = [
                session for (session_bot_hash, _group_id), session in _sessions.items()
                if session_bot_hash == bot_hash
            ]
            sessions.extend(
                session for (session_bot_hash, _group_id), session in _completed.items()
                if session_bot_hash == bot_hash
            )
            sessions.sort(key=lambda session: (
                2 if session.completed_monotonic > 0.0 else (
                    0 if session.current_token_id is not None else 1
                ),
                -session.updated_monotonic,
                -session.sequence,
            ))
            session_count = len(sessions)
            running_count = sum(
                1 for session in sessions if session.current_token_id is not None
            )
            completed_count = sum(
                1 for session in sessions if session.completed_monotonic > 0.0
            )
            pending_messages = sum(session.queue_depth() for session in sessions)
            selected = sessions[:limit]
            return {
                'generated_at': _now_iso(),
                'sessions': [
                    _snapshot_session(session, now) for session in selected
                ],
                'session_count': session_count,
                'running_count': running_count,
                'completed_count': completed_count,
                'pending_messages': pending_messages,
                'omitted_count': max(0, session_count - len(selected)),
            }
    except Exception:
        return {
            'generated_at': _now_iso(),
            'sessions': [],
            'session_count': 0,
            'running_count': 0,
            'completed_count': 0,
            'pending_messages': 0,
            'omitted_count': 0,
        }


def clear_bot(bot_hash):
    try:
        bot_hash = str(bot_hash)
        with _lock:
            for key in [key for key in _sessions if key[0] == bot_hash]:
                _sessions.pop(key, None)
            for key in [key for key in _completed if key[0] == bot_hash]:
                _completed.pop(key, None)
    except Exception:
        return None
    return None


def clear_all():
    try:
        with _lock:
            _sessions.clear()
            _completed.clear()
    except Exception:
        return None
    return None
