import copy
import json
import threading
import time

import pytest
from fastapi import HTTPException

from src.config.database import Session
from src.constants.enums import ROLES
from src.models.identity import User
from src.models.run import Run
from src.models.source import Policy, Source
from src.models.shift import (DowntimeInterval, MigrationControl, ShiftRecord, SourceChange,
                              SourceTombstone)
from src.services import migration, source_store
from src.services.calculation import calculate, downtime_minutes
from src.services.report import submit
from src.schemas.requests import RunInput

BASE = {'period': '2026-11-01', 'department': '试制机A', 'start': 480, 'end': 1020,
        'ideal_seconds': 25, 'total': 900, 'good': 872,
        'downtime': [[540, 590], [570, 620], [990, 1060]]}


def user(email='admin@demo.local', role='admin'):
    return User(email=email, role=role, password='x')


def make_source(db, key, **changes):
    data = copy.deepcopy(BASE)
    data.update(changes)
    return source_store.create_source(db, user(), data, natural_key=key, commit=False)


def seed_rows(n=12):
    with Session() as db:
        for i in range(n):
            make_source(db, f'demo-{i}', period=f'2026-11-{i % 3 + 1:02d}',
                        department=['试制机A', '试制机B', '试制机C'][i % 3],
                        total=800 + i, good=760 + i)
        db.add(Policy(version='OEE-v1', data={'version': 'OEE-v1', 'definition': '定义'}))
        db.commit()


def run_tick(times=20):
    for _ in range(times):
        with Session() as db:
            migration.tick(db)
        with Session() as db:
            if migration.status(db)['phase'] in ('verified', 'diff', 'error'):
                return


def test_split_reads_filter_by_device_and_date():
    seed_rows(9)
    with Session() as db:
        migration.start(db, user())
    run_tick()
    with Session() as db:
        migration.verify(db, user())
        migration.switch(db, user())
        by_device = source_store.read_sources(db, department='试制机B', legacy_shape=False)
        by_date = source_store.read_sources(db, period='2026-11-02', legacy_shape=False)
        both = source_store.read_sources(db, period='2026-11-02', department='试制机B', legacy_shape=False)
    assert by_device and all(x['department'] == '试制机B' for x in by_device)
    assert by_date and all(x['period'] == '2026-11-02' for x in by_date)
    assert all(x['department'] == '试制机B' and x['period'] == '2026-11-02' for x in both)
    assert len(by_device) == 3


def test_migration_catches_up_writes_and_deletes_during_copy():
    seed_rows(30)
    with Session() as db:
        migration.start(db, user())
        # 只复制一小批，模拟迁移进行中
        migration._copy_batch(db, migration._lock(db))
        db.commit()
        # 迁移期间的增改删
        obj = db.query(Source).filter_by(natural_key='demo-1').first()
        source_store.update_source(db, user(), dict(obj.data, good=1), obj.id)
        make_source(db, 'brand-new', period='2026-11-03', total=500, good=500)
        old = db.query(Source).filter_by(natural_key='demo-2').first()
        source_store.delete_source(db, user(), old.id)
        db.commit()
    run_tick()
    with Session() as db:
        ctrl = migration.status(db)
        keys = {x['natural_key'] for x in source_store.read_split(db)}
        assert ctrl['phase'] == 'verified'
        assert 'brand-new' in keys
        assert 'demo-2' not in keys  # 期间删除的不复活
        updated = source_store.read_split(db, department='试制机B')
        assert any(x['data']['good'] == 1 for x in updated if x['natural_key'] == 'demo-1')
        assert db.get(SourceTombstone, 'demo-2') is not None


def test_retry_is_idempotent_and_resume_does_not_miss():
    seed_rows(20)
    with Session() as db:
        migration.start(db, user())
    # 模拟中断：只复制2小批后暂停
    with Session() as db:
        ctrl = migration._lock(db)
        for _ in range(2):
            migration._copy_batch(db, ctrl)
        db.commit()
        before = db.query(ShiftRecord).count()
        migration.pause(db, user())
    assert 0 < before < 20
    with Session() as db:
        migration.start(db, user())
    run_tick()
    with Session() as db:
        ctrl = migration.status(db)
        assert ctrl['copied_count'] == 20
        assert ctrl['total_legacy'] == 20
        assert ctrl['phase'] == 'verified'
        assert ctrl['pending_changes'] == 0
    # 幂等重放：再次同步同一自然键不会产生重复班次
    with Session() as db:
        for _ in range(3):
            source_store.sync_natural_key(db, 'demo-5')
        db.commit()
        assert db.query(ShiftRecord).filter_by(natural_key='demo-5').count() == 1
        assert db.query(DowntimeInterval).join(ShiftRecord).filter(ShiftRecord.natural_key == 'demo-5').count() == 3


def test_stale_revision_does_not_overwrite_newer_and_tombstone_blocks_revive():
    seed_rows(1)
    with Session() as db:
        migration.start(db, user())
        migration._copy_batch(db, migration._lock(db))
        source = db.query(Source).filter_by(natural_key='demo-0').first()
        stale = Source(**{'natural_key': 'demo-0', 'data': source.data})
        source_store.update_source(db, user(), dict(source.data, good=10), source.id)
        revision = db.get(__import__('src.models.shift', fromlist=['SourceRevision']).SourceRevision, 'demo-0').revision
        db.commit()
        # 落后批次携带旧修订号与旧数据，不得覆盖新写入
        source_store.sync_normalized(db, stale, revision=revision-1)
        db.commit()
        row = db.query(ShiftRecord).filter_by(natural_key='demo-0').first()
        assert row.good_count == 10
        source_store.delete_source(db, user(), source.id)
        db.commit()
        source_store.sync_natural_key(db, 'demo-0')
        db.commit()
        assert db.query(ShiftRecord).filter_by(natural_key='demo-0').first() is None


def test_diff_blocks_switch_and_rollback_restores_legacy():
    seed_rows(6)
    with Session() as db:
        migration.start(db, user())
    run_tick()
    # 人为制造拆分存储差异（模拟坏批次），变更日志已消费完，重放无法掩盖
    with Session() as db:
        ctrl = db.get(MigrationControl, 1)
        ctrl.change_cursor = db.query(SourceChange.id).order_by(SourceChange.id.desc()).first()[0]
        row = db.query(ShiftRecord).filter_by(natural_key='demo-0').first()
        row.total_count += 1
        db.commit()
    with Session() as db, pytest.raises(HTTPException) as exc:
        migration.switch(db, user())
    assert exc.value.status_code == 409 and '差异' in exc.value.detail
    with Session() as db:
        ctrl = migration.status(db)
        assert ctrl['active_store'] == 'legacy'
        assert ctrl['phase'] == 'diff'
        assert ctrl['diffs']['count'] == 1
        device = ctrl['diffs']['devices'][0]
        assert device['一致'] is False
        assert device['总产量旧'] != device['总产量新']
        # 修复后可以切换
        bad = db.query(ShiftRecord).filter_by(natural_key='demo-0').first()
        bad.total_count -= 1
        db.commit()
    with Session() as db:
        migration.switch(db, user())
        assert migration.status(db)['active_store'] == 'split'
    with Session() as db:
        migration.rollback(db, user())
        assert migration.status(db)['active_store'] == 'legacy'
        assert migration.status(db)['phase'] == 'rolled_back'


def test_write_during_switch_window_is_not_lost_after_switch():
    seed_rows(6)
    with Session() as db:
        migration.start(db, user())
    run_tick()
    with Session() as db:
        migration.switch(db, user())
    with Session() as db:
        obj = db.query(Source).filter_by(natural_key='demo-0').first()
        source_store.update_source(db, user(), dict(obj.data, good=5), obj.id)
        make_source(db, 'post-switch', period='2026-11-01', total=1, good=1)
        db.commit()
        rows = {x['natural_key']: x['data'] for x in source_store.read_sources(db)}
        assert rows['demo-0']['good'] == 5
        assert rows['post-switch']['total'] == 1
    # 回退后切换后的更正仍在（拆分表与旧底稿同步写入）
    with Session() as db:
        migration.rollback(db, user())
        rows = {x['natural_key']: x['data'] for x in source_store.read_sources(db)}
        assert rows['demo-0']['good'] == 5
        assert rows['post-switch']['total'] == 1


def test_frozen_snapshot_matches_full_read_and_isolated_from_later_edits():
    seed_rows(9)
    with Session() as db:
        migration.start(db, user())
    run_tick()
    with Session() as db:
        migration.switch(db, user())
    with Session() as db:
        policy = db.query(Policy).first()
        run = submit(db, user(), RunInput(name='十一月核算', policy_id=policy.id, period='全部'))
        obj = db.query(Source).filter_by(natural_key='demo-0').first()
        source_store.update_source(db, user(), dict(obj.data, good=1), obj.id)
        db.commit()
    with Session() as db:
        stored = db.get(Run, run.id)
        expected = [x['data'] for x in source_store.read_split(db)]
        # 快照是提交时的完整一致底稿，不是改写后的数据
        assert len(stored.snapshot['sources']) == 9
        assert stored.snapshot['sources'] != expected
        result = calculate(stored.snapshot['sources'], stored.snapshot['policy'])
        assert result['summary']['计划分钟'] == 9 * (1020 - 480)


def test_concurrent_reads_and_writes_during_migration_do_not_corrupt_verification():
    seed_rows(40)
    with Session() as db:
        migration.start(db, user())
    errors = []

    def writer():
        try:
            for i in range(10):
                with Session() as db:
                    key = f'live-{i}'
                    make_source(db, key, period=f'2026-11-{i % 3 + 1:02d}', total=100, good=90)
                    db.commit()
                with Session() as db:
                    obj = db.query(Source).filter_by(natural_key=f'demo-{i}').first()
                    source_store.update_source(db, user(), dict(obj.data, good=50), obj.id)
                    db.commit()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    while thread.is_alive():
        with Session() as db:
            migration.tick(db)
    thread.join()
    assert not errors
    run_tick(60)
    with Session() as db:
        ctrl = migration.status(db)
        assert ctrl['phase'] == 'verified', ctrl
        assert ctrl['pending_changes'] == 0
        migration.switch(db, user())
        assert migration.status(db)['active_store'] == 'split'


def test_down_time_union_and_oee_per_device_match_legacy():
    seed_rows(6)
    with Session() as db:
        data = source_store.read_legacy(db, legacy_shape=False)
    assert downtime_minutes(480, 1020, BASE['downtime']) == 110
    result = calculate(data, {'version': 'OEE-v1', 'definition': 'd'})
    assert result['summary']['运行分钟'] == 6 * (540 - 110)


def test_switch_failure_rolls_back_to_legacy(monkeypatch):
    seed_rows(3)
    with Session() as db:
        migration.start(db, user())
    run_tick()
    original = source_store.read_split

    def boom(*args, **kwargs):
        raise RuntimeError('模拟存储读失败')

    with Session() as db:
        monkeypatch.setattr(source_store, 'read_split', boom)
        with pytest.raises(HTTPException) as exc:
            migration.switch(db, user())
        assert exc.value.status_code == 409
        monkeypatch.setattr(source_store, 'read_split', original)
        ctrl = migration.status(db)
        assert ctrl['active_store'] == 'legacy'
        assert ctrl['phase'] == 'switch_failed'
    # 失败后仍可读取旧底稿并重新切换
    with Session() as db:
        assert len(source_store.read_sources(db)) == 3
    run_tick()
    with Session() as db:
        migration.switch(db, user())
        assert migration.status(db)['active_store'] == 'split'


def test_snapshot_checksum_identical_before_and_after_switch(monkeypatch):
    import redis
    class Fake:
        def rpush(self, *a): pass
    monkeypatch.setattr(redis.Redis, 'from_url', classmethod(lambda cls, *a, **k: Fake()))
    seed_rows(9)
    with Session() as db:
        policy = db.query(Policy).first()
        before = submit(db, user(), RunInput(name='迁移前', policy_id=policy.id, period='全部'))
        migration.start(db, user())
    run_tick()
    with Session() as db:
        migration.switch(db, user())
        assert migration.status(db)['active_store'] == 'split'
        after = submit(db, user(), RunInput(name='迁移后', policy_id=policy.id, period='全部'))
        assert after.checksum == before.checksum
        assert after.snapshot == before.snapshot


def test_tick_picks_up_changes_after_auto_verify(monkeypatch):
    import redis
    class Fake:
        def rpush(self, *a): pass
    monkeypatch.setattr(redis.Redis, 'from_url', classmethod(lambda cls, *a, **k: Fake()))
    seed_rows(6)
    with Session() as db:
        migration.start(db, user())
    run_tick()
    with Session() as db:
        assert migration.status(db)['phase'] == 'verified'
        obj = db.query(Source).filter_by(natural_key='demo-0').first()
        source_store.update_source(db, user(), dict(obj.data, good=1), obj.id)
    # 自动核对之后发生的写入，worker 仍需追平并更新核对结果
    for _ in range(3):
        with Session() as db:
            migration.tick(db)
    with Session() as db:
        ctrl = migration.status(db)
        assert ctrl['pending_changes'] == 0
        assert ctrl['phase'] == 'verified'
        data = {x['natural_key']: x['data'] for x in source_store.read_split(db)}
        assert data['demo-0']['good'] == 1
