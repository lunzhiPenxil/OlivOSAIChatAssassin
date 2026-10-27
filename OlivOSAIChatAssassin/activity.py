import re
import secrets
import threading
import time
from datetime import datetime


ACTIVITY_PREVIEW_LIMIT = 96
ACTIVITY_SENDER_LIMIT = 32
ACTIVITY_TRACE_LIMIT = 12
ACTIVITY_TRACE_TEXT_LIMIT = 96
ACTIVITY_OUTGOING_LIMIT = 8
ACTIVITY_OUTGOING_TEXT_LIMIT = 240
ACTIVITY_SNAPSHOT_LIMIT = 50
ACTIVITY_TERMINAL_TTL_MS = 120000
ACTIVITY_COMPLETION_TTL_MS = ACTIVITY_TERMINAL_TTL_MS
ACTIVITY_TERMINAL_LIMIT = 50
ACTIVITY_COMPLETED_LIMIT = ACTIVITY_TERMINAL_LIMIT

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

MEMORY_STATE_RUNNING = 'running'
MEMORY_STATE_DONE = 'done'
MEMORY_STATE_FAILED = 'failed'
MEMORY_STATE_SKIPPED = 'skipped'

MEMORY_STATES = frozenset((
    MEMORY_STATE_RUNNING,
    MEMORY_STATE_DONE,
    MEMORY_STATE_FAILED,
    MEMORY_STATE_SKIPPED,
))

MEMORY_DETAIL_RUNNING = '与回复发送并行'
MEMORY_DETAIL_DONE = '并行整理完成'
MEMORY_DETAIL_FAILED = '并行整理失败，不影响回复'
MEMORY_DETAIL_SKIPPED = '已跳过并行整理'

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

TERMINAL_STATES = frozenset((
    STATE_FINISHED,
    STATE_SKIPPED,
    STATE_SUPERSEDED,
    STATE_FAILED,
))

RETAINED_TERMINAL_STATES = frozenset((
    STATE_FINISHED,
    STATE_SKIPPED,
    STATE_FAILED,
))

TRACE_ACTIVE = 'active'
TRACE_DONE = 'done'
TRACE_WARNING = 'warning'
TRACE_FAILED = 'failed'

TRACE_STATUSES = frozenset((
    TRACE_ACTIVE,
    TRACE_DONE,
    TRACE_WARNING,
    TRACE_FAILED,
))

TRACE_STEPS = STAGES | STATES

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


def sanitize_trace_detail(value):
    return _clean_text(value, ACTIVITY_TRACE_TEXT_LIMIT)


def sanitize_outgoing_message(value):
    return _clean_text(value, ACTIVITY_OUTGOING_TEXT_LIMIT)


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
        self.terminal_monotonic = 0.0
        self.expires_monotonic = 0.0
        self.state = STATE_QUEUED
        self.stage = STAGE_QUEUED
        self.preview = ''
        self.sender_name = ''
        self.attempt = 0
        self.retry_count = 0
        self.error_code = ''
        self.trace = []
        self.outgoing_messages = []
        self.outgoing_total = 0
        self.sending_index = -1
        self.sent_count = 0
        self.memory_state = ''
        self.memory_detail = ''
        self.memory_started_monotonic = 0.0
        self.memory_finished_monotonic = 0.0
        self.memory_token_id = None
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
_terminals = {}
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


def _session_for_memory_token(token):
    if not isinstance(token, ActivityToken):
        return None
    key = (token.bot_hash, token.group_id)
    session = _sessions.get(key)
    if (
        session is not None
        and session.session_id == token.session_id
        and session.current_token_id == token.token_id
        and session.memory_token_id == token.token_id
    ):
        return session
    terminal = _terminals.get(key)
    if terminal is not None and terminal.memory_token_id == token.token_id:
        return terminal
    return None


def _reset_memory_task(session):
    session.memory_state = ''
    session.memory_detail = ''
    session.memory_started_monotonic = 0.0
    session.memory_finished_monotonic = 0.0
    session.memory_token_id = None


def _refresh_pending_summary(session):
    if not session.pending_tokens:
        return
    latest = list(session.pending_tokens.values())[-1]
    session.preview = latest['preview']
    session.sender_name = latest['sender_name']


def _close_active_trace(session, now, status=TRACE_DONE, detail=None):
    if not session.trace:
        return
    entry = session.trace[-1]
    if entry.get('status') != TRACE_ACTIVE:
        return
    entry['status'] = status if status in TRACE_STATUSES else TRACE_DONE
    entry['at_ms'] = int(max(0.0, now - session.started_monotonic) * 1000)
    if detail is not None:
        entry['detail'] = sanitize_trace_detail(detail)


def _append_trace(session, stage, detail, status, now):
    if stage not in TRACE_STEPS:
        return
    safe_status = status if status in TRACE_STATUSES else TRACE_DONE
    safe_detail = sanitize_trace_detail(detail)
    at_ms = int(max(0.0, now - session.started_monotonic) * 1000)
    if session.trace and session.trace[-1].get('stage') == stage:
        entry = session.trace[-1]
        if entry.get('detail', '') == safe_detail:
            entry['status'] = safe_status
            entry['detail'] = safe_detail
            entry['at_ms'] = at_ms
            return
        if not entry.get('detail') and entry.get('status') == TRACE_ACTIVE:
            entry['status'] = safe_status
            entry['detail'] = safe_detail
            entry['at_ms'] = at_ms
            return
    _close_active_trace(session, now)
    session.trace.append({
        'stage': stage,
        'status': safe_status,
        'detail': safe_detail,
        'at_ms': at_ms,
    })
    if len(session.trace) > ACTIVITY_TRACE_LIMIT:
        del session.trace[:len(session.trace) - ACTIVITY_TRACE_LIMIT]


def _prune_terminals(now):
    expired_keys = [
        key for key, session in _terminals.items()
        if session.expires_monotonic <= now
    ]
    for key in expired_keys:
        _terminals.pop(key, None)
    overflow = len(_terminals) - ACTIVITY_TERMINAL_LIMIT
    if overflow <= 0:
        return
    oldest_keys = sorted(
        _terminals,
        key=lambda key: _terminals[key].expires_monotonic,
    )[:overflow]
    for key in oldest_keys:
        _terminals.pop(key, None)


def _remember_terminal(session, token, now, state, error_code):
    terminal = _ActivitySession(
        f'{session.session_id}-done-{token.token_id}',
        session.bot_hash,
        session.group_id,
        _next_id('session'),
    )
    terminal.state = state if state in TERMINAL_STATES else STATE_FINISHED
    terminal.stage = session.stage if session.stage in STAGES else STAGE_QUEUED
    terminal.preview = session.preview
    terminal.sender_name = session.sender_name
    terminal.attempt = session.attempt
    terminal.retry_count = session.retry_count
    terminal.error_code = _clean_error_code(error_code)
    terminal.trace = [dict(entry) for entry in session.trace]
    terminal.outgoing_messages = list(session.outgoing_messages)
    terminal.outgoing_total = session.outgoing_total
    terminal.sending_index = session.sending_index
    terminal.sent_count = session.sent_count
    terminal.memory_state = session.memory_state
    terminal.memory_detail = session.memory_detail
    terminal.memory_started_monotonic = session.memory_started_monotonic
    terminal.memory_finished_monotonic = session.memory_finished_monotonic
    terminal.memory_token_id = session.memory_token_id
    terminal.started_monotonic = session.started_monotonic
    terminal.started_at = session.started_at
    terminal.stage_started_monotonic = session.stage_started_monotonic
    terminal.updated_monotonic = now
    terminal.updated_at = _now_iso()
    terminal.terminal_monotonic = now
    terminal.expires_monotonic = now + ACTIVITY_TERMINAL_TTL_MS / 1000.0
    _terminals[(session.bot_hash, session.group_id)] = terminal
    _prune_terminals(now)


def _begin(bot_hash, group_id, sender_name, preview):
    token = _new_token(bot_hash, group_id)
    with _lock:
        now = time.monotonic()
        _prune_terminals(now)
        key = (bot_hash, group_id)
        _terminals.pop(key, None)
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
            session.trace = []
            session.outgoing_messages = []
            session.outgoing_total = 0
            session.sending_index = -1
            session.sent_count = 0
            _reset_memory_task(session)
            _append_trace(
                session,
                STAGE_QUEUED,
                f'等待处理 · 队列 {len(session.pending_tokens)}',
                TRACE_ACTIVE,
                now,
            )
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
        session.trace = []
        session.outgoing_messages = []
        session.outgoing_total = 0
        session.sending_index = -1
        session.sent_count = 0
        _reset_memory_task(session)
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
        _append_trace(session, session.stage, '取得处理权', TRACE_ACTIVE, now)


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
            _append_trace(session, stage, '', TRACE_ACTIVE, now)
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


def _note(token, stage, detail=None, status=TRACE_DONE):
    with _lock:
        session = _session_for_token(token)
        if session is None or session.current_token_id != token.token_id:
            return
        now = time.monotonic()
        _append_trace(session, stage, detail, status, now)
        session.updated_monotonic = now
        session.updated_at = _now_iso()


def note(token, stage, detail=None, status=TRACE_DONE):
    try:
        _note(token, stage, detail, status)
    except Exception:
        return None
    return None


def _memory_started(token):
    with _lock:
        session = _session_for_token(token)
        if session is None or session.current_token_id != token.token_id:
            return
        if session.memory_token_id == token.token_id:
            return
        now = time.monotonic()
        session.memory_token_id = token.token_id
        session.memory_state = MEMORY_STATE_RUNNING
        session.memory_detail = MEMORY_DETAIL_RUNNING
        session.memory_started_monotonic = now
        session.memory_finished_monotonic = 0.0
        session.updated_monotonic = now
        session.updated_at = _now_iso()


def memory_started(token):
    try:
        _memory_started(token)
    except Exception:
        return None
    return None


def _memory_finished(token, state):
    normalized_state = state if state in MEMORY_STATES else MEMORY_STATE_FAILED
    if normalized_state == MEMORY_STATE_RUNNING:
        normalized_state = MEMORY_STATE_FAILED
    with _lock:
        now = time.monotonic()
        _prune_terminals(now)
        session = _session_for_memory_token(token)
        if session is None or session.memory_finished_monotonic > 0.0:
            return
        if session.memory_state != MEMORY_STATE_RUNNING:
            return
        session.memory_state = normalized_state
        session.memory_detail = {
            MEMORY_STATE_DONE: MEMORY_DETAIL_DONE,
            MEMORY_STATE_FAILED: MEMORY_DETAIL_FAILED,
            MEMORY_STATE_SKIPPED: MEMORY_DETAIL_SKIPPED,
        }.get(normalized_state, MEMORY_DETAIL_FAILED)
        session.memory_finished_monotonic = now
        session.updated_monotonic = now
        session.updated_at = _now_iso()


def memory_finished(token, state=MEMORY_STATE_DONE):
    try:
        _memory_finished(token, state)
    except Exception:
        return None
    return None


def _set_outgoing(token, messages):
    with _lock:
        session = _session_for_token(token)
        if session is None or session.current_token_id != token.token_id:
            return
        safe_messages = []
        total = 0
        if isinstance(messages, (list, tuple)):
            for message in messages:
                safe_message = sanitize_outgoing_message(message)
                if not safe_message:
                    continue
                total += 1
                if len(safe_messages) < ACTIVITY_OUTGOING_LIMIT:
                    safe_messages.append(safe_message)
        session.outgoing_messages = safe_messages
        session.outgoing_total = total
        session.sending_index = -1
        session.sent_count = 0
        session.updated_monotonic = time.monotonic()
        session.updated_at = _now_iso()


def set_outgoing(token, messages):
    try:
        _set_outgoing(token, messages)
    except Exception:
        return None
    return None


def _set_send_progress(token, index, sent):
    with _lock:
        session = _session_for_token(token)
        if session is None or session.current_token_id != token.token_id:
            return
        now = time.monotonic()
        if type(index) is not int or index < 0:
            return
        total = max(session.outgoing_total, index + 1)
        detail = f'第 {index + 1}/{total} 条'
        if sent:
            session.sent_count = max(0, min(total, index + 1))
            session.sending_index = -1
            status = TRACE_DONE
        else:
            session.sending_index = index
            session.sent_count = min(session.sent_count, index)
            status = TRACE_ACTIVE
        _append_trace(session, STAGE_SENDING, detail, status, now)
        session.updated_monotonic = now
        session.updated_at = _now_iso()


def mark_sending(token, index):
    try:
        _set_send_progress(token, index, False)
    except Exception:
        return None
    return None


def mark_sent(token, index):
    try:
        _set_send_progress(token, index, True)
    except Exception:
        return None
    return None


def _finish(token, state, error_code):
    with _lock:
        now = time.monotonic()
        _prune_terminals(now)
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

        normalized_state = state if state in STATES else STATE_FAILED
        has_pending = bool(session.pending_tokens)
        final_status = TRACE_DONE
        if normalized_state == STATE_FAILED:
            final_status = TRACE_FAILED
        elif normalized_state == STATE_SKIPPED:
            final_status = TRACE_WARNING
        if normalized_state in RETAINED_TERMINAL_STATES and not has_pending:
            _close_active_trace(session, now, final_status)
            _append_trace(
                session,
                normalized_state,
                _clean_error_code(error_code),
                final_status,
                now,
            )
            _remember_terminal(session, token, now, normalized_state, error_code)
        session.current_token_id = None
        session.state = normalized_state
        session.stage = STAGE_QUEUED
        session.stage_started_monotonic = now
        session.updated_monotonic = now
        session.updated_at = _now_iso()
        session.attempt = 0
        session.retry_count = 0
        session.error_code = _clean_error_code(error_code)
        if has_pending:
            _refresh_pending_summary(session)
            session.state = STATE_QUEUED
            session.trace = []
            session.outgoing_messages = []
            session.outgoing_total = 0
            session.sending_index = -1
            session.sent_count = 0
            _reset_memory_task(session)
            _append_trace(
                session,
                STAGE_QUEUED,
                f'等待下一条 · 队列 {len(session.pending_tokens)}',
                TRACE_ACTIVE,
                now,
            )
        else:
            _sessions.pop((token.bot_hash, token.group_id), None)


def finish(token, state=STATE_FINISHED, error_code=None):
    try:
        _finish(token, state, error_code)
    except Exception:
        return None
    return None


def _snapshot_memory_task(session, now):
    if (
        session.memory_state not in MEMORY_STATES
        or session.memory_token_id is None
        or session.memory_started_monotonic <= 0.0
    ):
        return None
    elapsed_at = (
        session.memory_finished_monotonic
        if session.memory_finished_monotonic > 0.0
        else now
    )
    return {
        'state': session.memory_state,
        'detail': sanitize_trace_detail(session.memory_detail),
        'elapsed_ms': int(
            max(0.0, elapsed_at - session.memory_started_monotonic) * 1000
        ),
    }


def _snapshot_session(session, now):
    terminal = session.terminal_monotonic > 0.0
    queue_depth = 0 if terminal else session.queue_depth()
    if terminal:
        state = session.state if session.state in TERMINAL_STATES else STATE_FINISHED
        stage = session.stage if session.stage in STAGES else STAGE_QUEUED
        elapsed_at = session.terminal_monotonic
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
        'trace': [
            {
                'stage': entry.get('stage', STAGE_QUEUED),
                'status': entry.get('status', TRACE_DONE),
                'detail': sanitize_trace_detail(entry.get('detail', '')),
                'at_ms': max(0, int(entry.get('at_ms', 0))),
            }
            for entry in session.trace
            if isinstance(entry, dict)
        ],
        'outgoing_messages': [
            sanitize_outgoing_message(message)
            for message in session.outgoing_messages
            if sanitize_outgoing_message(message)
        ],
        'outgoing_total': session.outgoing_total,
        'sending_index': session.sending_index,
        'sent_count': session.sent_count,
        'memory_task': _snapshot_memory_task(session, now),
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
            _prune_terminals(now)
            sessions = [
                session for (session_bot_hash, _group_id), session in _sessions.items()
                if session_bot_hash == bot_hash
            ]
            sessions.extend(
                session for (session_bot_hash, _group_id), session in _terminals.items()
                if session_bot_hash == bot_hash
            )
            sessions.sort(key=lambda session: (
                2 if session.terminal_monotonic > 0.0 else (
                    0 if session.current_token_id is not None else 1
                ),
                -session.updated_monotonic,
                -session.sequence,
            ))
            session_count = len(sessions)
            running_count = sum(
                1 for session in sessions if session.current_token_id is not None
            )
            terminal_count = sum(
                1 for session in sessions if session.terminal_monotonic > 0.0
            )
            completed_count = sum(
                1 for session in sessions
                if session.terminal_monotonic > 0.0 and session.state == STATE_FINISHED
            )
            skipped_count = sum(
                1 for session in sessions
                if session.terminal_monotonic > 0.0 and session.state == STATE_SKIPPED
            )
            failed_count = sum(
                1 for session in sessions
                if session.terminal_monotonic > 0.0 and session.state == STATE_FAILED
            )
            superseded_count = sum(
                1 for session in sessions
                if session.terminal_monotonic > 0.0 and session.state == STATE_SUPERSEDED
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
                'terminal_count': terminal_count,
                'completed_count': completed_count,
                'skipped_count': skipped_count,
                'failed_count': failed_count,
                'superseded_count': superseded_count,
                'pending_messages': pending_messages,
                'omitted_count': max(0, session_count - len(selected)),
            }
    except Exception:
        return {
            'generated_at': _now_iso(),
            'sessions': [],
            'session_count': 0,
            'running_count': 0,
            'terminal_count': 0,
            'completed_count': 0,
            'skipped_count': 0,
            'failed_count': 0,
            'superseded_count': 0,
            'pending_messages': 0,
            'omitted_count': 0,
        }


def clear_bot(bot_hash):
    try:
        bot_hash = str(bot_hash)
        with _lock:
            for key in [key for key in _sessions if key[0] == bot_hash]:
                _sessions.pop(key, None)
            for key in [key for key in _terminals if key[0] == bot_hash]:
                _terminals.pop(key, None)
    except Exception:
        return None
    return None


def clear_all():
    try:
        with _lock:
            _sessions.clear()
            _terminals.clear()
    except Exception:
        return None
    return None
