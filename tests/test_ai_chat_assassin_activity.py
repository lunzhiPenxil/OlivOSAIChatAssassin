"""Runtime activity registry tests for the OlivOSAIChatAssassin plugin."""

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_APP = REPO_ROOT
PLUGIN_ROOT = PLUGIN_APP / 'OlivOSAIChatAssassin'
if not (PLUGIN_ROOT / 'app.json').is_file():
    pytest.skip('OlivOSAIChatAssassin 未安装，跳过插件专项测试', allow_module_level=True)
if str(PLUGIN_APP) not in sys.path:
    sys.path.insert(0, str(PLUGIN_APP))

from OlivOSAIChatAssassin import activity  # noqa: E402


@pytest.fixture(autouse=True)
def clear_activity():
    activity.clear_all()
    yield
    activity.clear_all()


# 预览会移除消息桥标记、控制字符与换行，并限制长度。
def test_activity_preview_removes_bridge_markup_and_control_characters():
    preview = activity.sanitize_preview('[OP:at,id=10001]\n你好\x00  世界 ' + '长' * 200)
    assert preview.startswith('你好 世界')
    assert 'OP:' not in preview
    assert '\n' not in preview
    assert len(preview) <= activity.ACTIVITY_PREVIEW_LIMIT
    assert preview.endswith('…')


# 新会话先进入排队状态，并暴露队列深度与消息预览。
def test_activity_begin_exposes_a_queued_session():
    activity.begin('bot-a', '123', '群友', '今晚开黑吗')
    snapshot = activity.snapshot('bot-a')
    assert snapshot['session_count'] == 1
    assert snapshot['running_count'] == 0
    assert snapshot['pending_messages'] == 1
    session = snapshot['sessions'][0]
    assert session['group_label'] == '群 123'
    assert session['state'] == activity.STATE_QUEUED
    assert session['stage'] == activity.STAGE_QUEUED
    assert session['queue_depth'] == 1
    assert session['message_preview'] == '今晚开黑吗'


# 会话激活后，阶段和尝试次数更新会出现在快照中。
def test_activity_activation_and_stage_updates_are_visible():
    token = activity.begin('bot-a', '123', '群友', '查一下资料')
    activity.activate(token)
    activity.update(token, stage=activity.STAGE_MODEL, attempt=1, retry_count=0)
    session = activity.snapshot('bot-a')['sessions'][0]
    assert session['state'] == activity.STATE_RUNNING
    assert session['stage'] == activity.STAGE_MODEL
    assert session['attempt'] == 1
    assert session['elapsed_ms'] >= 0
    assert session['stage_elapsed_ms'] >= 0


# 轨迹保留不同步骤的结果，并受固定条数上限约束。
def test_activity_trace_keeps_distinct_steps_and_applies_a_hard_limit():
    token = activity.begin('bot-a', '123', '群友', '轨迹')
    activity.activate(token)
    activity.note(token, activity.STAGE_MODEL, '第一次结果')
    activity.note(token, activity.STAGE_MODEL, '修复后结果')
    trace = activity.snapshot('bot-a')['sessions'][0]['trace']
    model_details = [
        entry['detail'] for entry in trace if entry['stage'] == activity.STAGE_MODEL
    ]
    assert model_details == ['第一次结果', '修复后结果']

    for index in range(20):
        activity.note(token, activity.STAGE_MODEL, f'结果 {index}')
    trace = activity.snapshot('bot-a')['sessions'][0]['trace']
    assert len(trace) == activity.ACTIVITY_TRACE_LIMIT
    assert trace[-1]['detail'] == '结果 19'
    activity.finish(token)


# 同一步骤复用一条记录，发送进度不会无限增长。
def test_activity_trace_updates_same_step_and_outgoing_progress_is_bounded():
    token = activity.begin('bot-a', '123', '群友', '发送')
    activity.activate(token)
    activity.note(token, activity.STAGE_SENDING, '第 1/10 条', activity.TRACE_ACTIVE)
    activity.note(token, activity.STAGE_SENDING, '第 1/10 条', activity.TRACE_DONE)
    sending_steps = [
        entry for entry in activity.snapshot('bot-a')['sessions'][0]['trace']
        if entry['stage'] == activity.STAGE_SENDING
    ]
    assert len(sending_steps) == 1
    assert sending_steps[0]['status'] == activity.TRACE_DONE

    messages = ['[OP:at,id=1] 第一条'] + [f'第 {index} 条' for index in range(2, 10)]
    messages.append('   ')
    activity.set_outgoing(token, messages)
    activity.mark_sending(token, 0)
    activity.mark_sent(token, 0)
    activity.mark_sending(token, 1)
    snapshot = activity.snapshot('bot-a')
    session = snapshot['sessions'][0]
    assert session['outgoing_total'] == 9
    assert len(session['outgoing_messages']) == activity.ACTIVITY_OUTGOING_LIMIT
    assert session['outgoing_messages'][0] == '第一条'
    assert session['sending_index'] == 1
    assert session['sent_count'] == 1
    assert session['trace'][-1]['detail'] == '第 2/9 条'
    assert session['trace'][-1]['status'] == activity.TRACE_ACTIVE

    session['trace'].clear()
    session['outgoing_messages'].clear()
    detached = activity.snapshot('bot-a')['sessions'][0]
    assert detached['trace']
    assert detached['outgoing_messages']
    activity.finish(token)
    terminal = activity.snapshot('bot-a')['sessions'][0]
    assert terminal['state'] == activity.STATE_FINISHED
    assert terminal['outgoing_messages'] == detached['outgoing_messages']
    assert terminal['trace']


# 记忆任务可与发送并行，并在终态交接时保留完成状态。
def test_activity_memory_task_runs_alongside_sending_and_survives_terminal_handoff():
    token = activity.begin('bot-a', '123', '群友', '并行处理')
    activity.activate(token)
    activity.update(token, stage=activity.STAGE_SENDING)
    activity.memory_started(token)

    running = activity.snapshot('bot-a')['sessions'][0]
    assert running['stage'] == activity.STAGE_SENDING
    assert running['memory_task']['state'] == activity.MEMORY_STATE_RUNNING
    assert running['memory_task']['detail'] == activity.MEMORY_DETAIL_RUNNING
    assert running['memory_task']['elapsed_ms'] >= 0

    activity.finish(token)
    terminal = activity.snapshot('bot-a')['sessions'][0]
    assert terminal['state'] == activity.STATE_FINISHED
    assert terminal['stage'] == activity.STAGE_SENDING
    assert terminal['memory_task']['state'] == activity.MEMORY_STATE_RUNNING

    activity.memory_finished(token, activity.MEMORY_STATE_DONE)
    completed = activity.snapshot('bot-a')['sessions'][0]
    assert completed['state'] == activity.STATE_FINISHED
    assert completed['memory_task']['state'] == activity.MEMORY_STATE_DONE
    assert completed['memory_task']['detail'] == activity.MEMORY_DETAIL_DONE
    assert completed['memory_task']['elapsed_ms'] >= 0


# 旧记忆任务的迟到完成不能修改同群的新会话。
def test_activity_late_memory_completion_cannot_modify_a_new_session():
    old = activity.begin('bot-a', '123', '甲', '旧消息')
    activity.activate(old)
    activity.update(old, stage=activity.STAGE_SENDING)
    activity.memory_started(old)
    activity.finish(old)

    current = activity.begin('bot-a', '123', '乙', '新消息')
    activity.activate(current)
    activity.memory_finished(old, activity.MEMORY_STATE_DONE)

    session = activity.snapshot('bot-a')['sessions'][0]
    assert session['message_preview'] == '新消息'
    assert session['memory_task'] is None
    activity.finish(current)


# 新消息接管同群时，会清理上一条记忆任务状态。
def test_activity_memory_task_is_cleared_when_the_next_message_takes_over():
    old = activity.begin('bot-a', '123', '甲', '当前消息')
    activity.activate(old)
    activity.update(old, stage=activity.STAGE_SENDING)
    activity.memory_started(old)
    queued = activity.begin('bot-a', '123', '乙', '下一条消息')

    activity.finish(old)
    session = activity.snapshot('bot-a')['sessions'][0]
    assert session['state'] == activity.STATE_QUEUED
    assert session['message_preview'] == '下一条消息'
    assert session['memory_task'] is None
    activity.finish(queued)


# 正常结束后保留短期终态快照供前端展示。
@pytest.mark.parametrize(
    ('terminal_state', 'count_key', 'error_code'),
    (
        (activity.STATE_FINISHED, 'completed_count', None),
        (activity.STATE_SKIPPED, 'skipped_count', 'not_selected'),
        (activity.STATE_FAILED, 'failed_count', 'model_failure'),
    ),
)
def test_activity_terminal_finish_keeps_a_short_lived_snapshot(
    terminal_state,
    count_key,
    error_code,
):
    assert terminal_state in activity.STATES
    token = activity.begin('bot-a', '123', '群友', '正常完成')
    activity.activate(token)
    activity.finish(token, terminal_state, error_code)
    snapshot = activity.snapshot('bot-a')
    assert snapshot['session_count'] == 1
    assert snapshot['running_count'] == 0
    assert snapshot['terminal_count'] == 1
    assert snapshot[count_key] == 1
    session = snapshot['sessions'][0]
    assert session['state'] == terminal_state
    assert session['error_code'] == (error_code or '')
    assert session['expires_in_ms'] > 0


# 终态快照会在固定 TTL 后过期。
@pytest.mark.parametrize(
    ('terminal_state', 'count_key', 'error_code'),
    (
        (activity.STATE_FINISHED, 'completed_count', None),
        (activity.STATE_SKIPPED, 'skipped_count', 'not_selected'),
        (activity.STATE_FAILED, 'failed_count', 'model_failure'),
    ),
)
def test_activity_terminal_snapshot_expires_after_fixed_ttl(
    monkeypatch,
    terminal_state,
    count_key,
    error_code,
):
    now = [100.0]
    monkeypatch.setattr(activity.time, 'monotonic', lambda: now[0])
    assert activity.ACTIVITY_TERMINAL_TTL_MS == 120000
    token = activity.begin('bot-a', '123', '群友', '完成倒计时')
    activity.activate(token)
    activity.finish(token, terminal_state, error_code)

    first = activity.snapshot('bot-a')['sessions'][0]
    assert first['expires_in_ms'] == activity.ACTIVITY_TERMINAL_TTL_MS
    frozen_elapsed_ms = first['elapsed_ms']
    frozen_stage_elapsed_ms = first['stage_elapsed_ms']

    now[0] += 119.9
    visible = activity.snapshot('bot-a')
    assert visible['terminal_count'] == 1
    assert visible[count_key] == 1
    assert 0 < visible['sessions'][0]['expires_in_ms'] <= 100
    assert visible['sessions'][0]['elapsed_ms'] == frozen_elapsed_ms
    assert visible['sessions'][0]['stage_elapsed_ms'] == frozen_stage_elapsed_ms

    now[0] += 0.2
    expired = activity.snapshot('bot-a')
    assert expired['sessions'] == []
    assert expired['terminal_count'] == 0
    assert expired[count_key] == 0


# 同群消息会聚合队列深度并正确交接预览。
def test_activity_same_group_aggregates_queue_depth_and_hands_off_preview():
    first = activity.begin('bot-a', '123', '甲', '第一条')
    activity.activate(first)
    activity.begin('bot-a', '123', '乙', '第二条')
    running = activity.snapshot('bot-a')['sessions'][0]
    assert running['message_preview'] == '第一条'
    assert running['queue_depth'] == 1

    activity.finish(first)
    snapshot = activity.snapshot('bot-a')
    assert snapshot['session_count'] == 1
    assert snapshot['completed_count'] == 0
    queued = snapshot['sessions'][0]
    assert queued['state'] == activity.STATE_QUEUED
    assert queued['message_preview'] == '第二条'
    assert queued['sender_name'] == '乙'
    assert queued['queue_depth'] == 1


# 被新消息取代的完成不会保留终态快照。
def test_activity_superseded_finish_does_not_keep_terminal_snapshot():
    token = activity.begin('bot-a', '123', '群友', '退避中的消息')
    activity.activate(token)
    activity.finish(token, activity.STATE_SUPERSEDED, 'new_message')

    snapshot = activity.snapshot('bot-a')
    assert snapshot['sessions'] == []
    assert snapshot['terminal_count'] == 0
    assert snapshot['superseded_count'] == 0


# 被取代的会话完成时，下一条消息仍保持排队。
def test_activity_superseded_finish_keeps_next_message_queued():
    old = activity.begin('bot-a', '123', '甲', '退避中的消息')
    activity.activate(old)
    new = activity.begin('bot-a', '123', '乙', '已经到达的新消息')
    activity.finish(old, activity.STATE_SUPERSEDED, 'new_message')

    snapshot = activity.snapshot('bot-a')
    assert snapshot['session_count'] == 1
    assert snapshot['terminal_count'] == 0
    assert snapshot['superseded_count'] == 0
    session = snapshot['sessions'][0]
    assert session['state'] == activity.STATE_QUEUED
    assert session['message_preview'] == '已经到达的新消息'
    assert session['sender_name'] == '乙'
    activity.finish(new)


# 新消息会立即替换同群的终态卡片。
@pytest.mark.parametrize(
    ('terminal_state', 'count_key'),
    (
        (activity.STATE_FINISHED, 'completed_count'),
        (activity.STATE_SKIPPED, 'skipped_count'),
        (activity.STATE_FAILED, 'failed_count'),
    ),
)
def test_activity_new_message_replaces_terminal_card_immediately(terminal_state, count_key):
    completed = activity.begin('bot-a', '123', '甲', '已完成消息')
    activity.activate(completed)
    activity.finish(completed, terminal_state, 'not_selected')
    assert activity.snapshot('bot-a')[count_key] == 1

    queued = activity.begin('bot-a', '123', '乙', '新消息')
    snapshot = activity.snapshot('bot-a')
    assert snapshot['session_count'] == 1
    assert snapshot['terminal_count'] == 0
    for terminal_count_key in (
        'completed_count', 'skipped_count', 'failed_count', 'superseded_count',
    ):
        assert snapshot[terminal_count_key] == 0
    session = snapshot['sessions'][0]
    assert session['state'] == activity.STATE_QUEUED
    assert session['message_preview'] == '新消息'
    assert session['sender_name'] == '乙'
    activity.finish(queued)


# 完成结果会保留，且多个 bot 的活动数据互相隔离。
def test_activity_finish_keeps_completed_result_and_keeps_multi_bot_isolated():
    first = activity.begin('bot-a', '123', '甲', '同一群')
    second = activity.begin('bot-b', '123', '乙', '同一群')
    activity.activate(first)
    activity.activate(second)
    assert activity.snapshot('bot-a')['session_count'] == 1
    assert activity.snapshot('bot-b')['session_count'] == 1
    activity.finish(first)
    first_snapshot = activity.snapshot('bot-a')
    assert first_snapshot['session_count'] == 1
    assert first_snapshot['completed_count'] == 1
    assert first_snapshot['sessions'][0]['state'] == activity.STATE_FINISHED
    second_snapshot = activity.snapshot('bot-b')
    assert second_snapshot['session_count'] == 1
    assert second_snapshot['completed_count'] == 0


# 失败结束会保留短期失败快照。
def test_activity_failed_finish_keeps_a_short_lived_failure_snapshot():
    token = activity.begin('bot-a', '123', '群友', '失败消息')
    activity.activate(token)
    activity.finish(token, activity.STATE_FAILED, 'model_failure')
    snapshot = activity.snapshot('bot-a')
    assert snapshot['session_count'] == 1
    assert snapshot['terminal_count'] == 1
    assert snapshot['completed_count'] == 0
    assert snapshot['skipped_count'] == 0
    assert snapshot['failed_count'] == 1
    assert snapshot['superseded_count'] == 0
    session = snapshot['sessions'][0]
    assert session['state'] == activity.STATE_FAILED
    assert session['error_code'] == 'model_failure'
    assert 0 < session['expires_in_ms'] <= activity.ACTIVITY_TERMINAL_TTL_MS


# 旧 token 不能更新或删除新会话。
def test_activity_old_token_cannot_update_or_remove_new_session():
    old = activity.begin('bot-a', '123', '甲', '旧消息')
    activity.activate(old)
    activity.finish(old)
    current = activity.begin('bot-a', '123', '乙', '新消息')
    activity.activate(current)

    activity.update(old, stage=activity.STAGE_SENDING)
    activity.finish(old, activity.STATE_FAILED, 'stale_failure')
    session = activity.snapshot('bot-a')['sessions'][0]
    assert session['stage'] == activity.STAGE_PREPARING
    assert session['message_preview'] == '新消息'
    activity.finish(current)


# 快照返回脱离对象，并按运行中会话优先排序。
def test_activity_snapshot_returns_detached_data_and_orders_running_first():
    queued = activity.begin('bot-a', 'queued-group', '甲', '排队')
    running = activity.begin('bot-a', 'running-group', '乙', '运行')
    activity.activate(running)
    snapshot = activity.snapshot('bot-a')
    assert [item['group_id'] for item in snapshot['sessions']] == [
        'running-group', 'queued-group',
    ]
    snapshot['sessions'][0]['stage'] = 'tampered'
    assert activity.snapshot('bot-a')['sessions'][0]['stage'] == activity.STAGE_PREPARING
    activity.finish(running)
    activity.finish(queued)


# 快照数量受限时会报告被省略的会话数。
def test_activity_snapshot_limit_reports_omitted_sessions():
    tokens = [
        activity.begin('bot-a', str(index), '群友', '消息')
        for index in range(3)
    ]
    snapshot = activity.snapshot('bot-a', limit=2)
    assert snapshot['session_count'] == 3
    assert len(snapshot['sessions']) == 2
    assert snapshot['omitted_count'] == 1
    for token in tokens:
        activity.finish(token)


# 活动注册表异常按 best-effort 处理，不影响主流程。
def test_activity_registry_failure_is_best_effort(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('activity unavailable')

    monkeypatch.setattr(activity, '_begin', fail)
    token = activity.begin('bot-a', '123', '群友', '消息')
    activity.update(token, stage=activity.STAGE_MODEL)
    activity.finish(token)
    assert activity.snapshot('bot-a')['sessions'] == []
