import time
from fastapi import HTTPException
from src.config.settings import CONFIG, MIGRATION_BATCH
from src.models.source import Source
from src.models.shift import ShiftRecord, DowntimeInterval, SourceChange, MigrationControl, SourceTombstone
from src.services.calculation import calculate
from src.services.audit import log
from src.services import source_store as stores


def _lock(db):
 stores.ensure_control(db)
 return db.query(MigrationControl).filter_by(id=1).populate_existing().with_for_update().first()


def _pending(db, ctrl):
 return db.query(SourceChange.id).filter(SourceChange.id > ctrl.change_cursor).count()


def _actor(user):
 return getattr(user, 'email', None) or str(user)


def start(db, user, restart=False):
 ctrl = _lock(db)
 if ctrl.active_store == 'split':
  raise HTTPException(409, '底稿已切换到拆分存储；如需重新迁移请先回退')
 if ctrl.phase == 'running' and not restart:
  raise HTTPException(409, '迁移正在运行；中断后再次启动会从断点续跑')
 if restart:
  db.query(DowntimeInterval).delete(synchronize_session=False)
  db.query(ShiftRecord).delete(synchronize_session=False)
  ctrl.legacy_cursor = 0
  ctrl.change_cursor = 0
  ctrl.copied_count = 0
  ctrl.started_at = time.time()
  ctrl.diffs = None
  ctrl.verified_at = None
 elif not ctrl.started_at:
  ctrl.started_at = time.time()
 ctrl.total_legacy = db.query(Source).count()
 ctrl.phase = 'running'
 ctrl.error = None
 ctrl.updated_at = time.time()
 ctrl.last_actor = _actor(user)
 log(db, ctrl.last_actor, 'migration.start', '重启迁移' if restart else '开始或续跑')
 db.commit()
 return ctrl


def pause(db, user):
 ctrl = _lock(db)
 if ctrl.phase != 'running':
  raise HTTPException(409, '迁移未在运行')
 ctrl.phase = 'paused'
 ctrl.updated_at = time.time()
 ctrl.last_actor = _actor(user)
 log(db, ctrl.last_actor, 'migration.pause', '已暂停，断点保留')
 db.commit()
 return ctrl


def _copy_batch(db, ctrl):
 rows = (db.query(Source).filter(Source.id > ctrl.legacy_cursor).order_by(Source.id)
  .limit(MIGRATION_BATCH).populate_existing().all())
 moved = 0
 for source in rows:
  if db.get(SourceTombstone, source.natural_key):
   # 迁移期间已删除的班次：只推进断点，不复制，避免复活
   ctrl.legacy_cursor = max(ctrl.legacy_cursor, source.id)
   moved += 1
   continue
  result = stores.sync_natural_key(db, source.natural_key)
  if result is None:
   # 该行正被并发修改，下一轮重试，不推进断点
   break
  ctrl.legacy_cursor = max(ctrl.legacy_cursor, source.id)
  moved += 1
 ctrl.copied_count = db.query(ShiftRecord).count()
 return moved


def _replay_changes(db, ctrl, limit=500):
 rows = (db.query(SourceChange).filter(SourceChange.id > ctrl.change_cursor)
  .order_by(SourceChange.id).limit(limit).all())
 moved = 0
 for change in rows:
  if stores.sync_natural_key(db, change.natural_key) is None: break
  ctrl.change_cursor = change.id
  moved += 1
 return moved


def _verify_locked(db, ctrl):
 """在控制行锁内先追平变更，再逐设备、逐设备日期核对班次数量、停机并集与 OEE。

 只依据变更日志追平，不静默修补拆分存储：多出/缺失的自然键会作为差异上报并阻止切换。
 """
 while _pending(db, ctrl):
  if not _replay_changes(db, ctrl): break
 legacy = stores.read_legacy(db)
 split = stores.read_split(db)
 keys = sorted(set(_group(legacy, ('department',))) | set(_group(split, ('department',))))
 rows, diffs = [], []
 for (device,) in keys:
  old = _metrics([x for x in legacy if x['data']['department'] == device])
  new = _metrics([x for x in split if x['data']['department'] == device])
  bad = [k for k in ('班次','总产量','合格数','停机并集分钟','OEE','自然键') if not _same(old[k], new[k])]
  item = {'设备': device, '班次旧': old['班次'], '班次新': new['班次'],
   '总产量旧': old['总产量'], '总产量新': new['总产量'], '合格数旧': old['合格数'], '合格数新': new['合格数'],
   '停机并集旧': old['停机并集分钟'], '停机并集新': new['停机并集分钟'],
   'OEE旧': old['OEE'], 'OEE新': new['OEE'], '一致': not bad, '差异项': bad}
  rows.append(item)
  if bad: diffs.append(item)
 detail_old = _group(legacy, ('department', 'period'))
 detail_new = _group(split, ('department', 'period'))
 device_dates = []
 detail_diffs = []
 for pair in sorted(set(detail_old) | set(detail_new)):
  old = _metrics(detail_old.get(pair, []))
  new = _metrics(detail_new.get(pair, []))
  bad = [k for k in ('班次','总产量','合格数','停机并集分钟','OEE','自然键') if not _same(old[k], new[k])]
  item = {'设备': pair[0], '日期': pair[1], '班次旧': old['班次'], '班次新': new['班次'],
   '总产量旧': old['总产量'], '总产量新': new['总产量'], '合格数旧': old['合格数'], '合格数新': new['合格数'],
   '停机并集旧': old['停机并集分钟'], '停机并集新': new['停机并集分钟'],
   'OEE旧': old['OEE'], 'OEE新': new['OEE'], '一致': not bad, '差异项': bad}
  device_dates.append(item)
  if bad: detail_diffs.append(item)
 return rows, device_dates, diffs + detail_diffs


def _union_minutes(intervals):
 pieces = sorted([float(a), float(b)] for a, b in intervals)
 merged = []
 for a, b in pieces:
  if merged and a <= merged[-1][1]:
   merged[-1][1] = max(merged[-1][1], b)
  else:
   merged.append([a, b])
 return sum(b - a for a, b in merged)


def _metrics(items):
 data = [item['data'] for item in items]
 intervals = [pair for row in data for pair in row['downtime']]
 try:
  oee = calculate(data, CONFIG['policy'])['summary']['汇总OEE'] if data else 0
 except Exception as exc:
  oee = 'ERR:' + str(exc)
 return {'班次': len(items), '总产量': sum(int(row['total']) for row in data),
  '合格数': sum(int(row['good']) for row in data), '停机并集分钟': _union_minutes(intervals),
  'OEE': oee, '自然键': sorted(item['natural_key'] for item in items)}


def _same(a, b):
 if isinstance(a, (int, float)) and isinstance(b, (int, float)):
  return abs(float(a) - float(b)) <= 1e-9
 return a == b


def _group(items, keys):
 grouped = {}
 for item in items:
  grouped.setdefault(tuple(item['data'][k] for k in keys), []).append(item)
 return grouped


def _store_diffs(ctrl, devices, rows, diffs):
 detail=[row for row in rows if row.get('日期') is not None and not row['一致']]
 ctrl.diffs={'devices': devices, 'rows': rows, 'count': len(detail), 'blocked': bool(diffs)}
 ctrl.verified_at = time.time()


def _validate_and_store(db, ctrl):
 try:
  devices, rows, diffs = _verify_locked(db, ctrl)
 except Exception as exc:
  ctrl.phase = 'error'
  ctrl.error = str(exc)
  ctrl.updated_at = time.time()
  db.commit()
  raise
 _store_diffs(ctrl, devices, rows, diffs)
 ctrl.updated_at = time.time()
 ctrl.phase = 'verified' if not diffs else 'diff'
 return diffs


def tick(db):
 """worker 单步推进：复制一批、持续追平变更；追平后自动核对。

 切换到拆分存储之前，worker 始终消费变更日志，因此即便阶段已是 verified，
 迁移期间的新增/修改/删除也会被追平后重新核对。
 """
 ctrl = _lock(db)
 if ctrl.active_store == 'split' or ctrl.phase in ('idle', 'paused', 'error'):
  db.commit()
  return
 try:
  copied = 0
  if ctrl.phase == 'running':
   copied = _copy_batch(db, ctrl)
  replayed = _replay_changes(db, ctrl)
  ctrl.copied_count = db.query(ShiftRecord).count()
  ctrl.updated_at = time.time()
  drained = (db.query(Source).filter(Source.id > ctrl.legacy_cursor).count() == 0
             and _pending(db, ctrl) == 0)
  if drained and (ctrl.phase == 'running' or replayed or ctrl.verified_at is None):
   diffs = _validate_and_store(db, ctrl)
   log(db, 'worker', 'migration.verify', f'{len(diffs)}项差异')
  db.commit()
 except Exception as exc:
  db.rollback()
  ctrl = _lock(db)
  ctrl.phase = 'error'
  ctrl.error = str(exc)
  ctrl.updated_at = time.time()
  log(db, 'worker', 'migration.error', str(exc))
  db.commit()


def verify(db, user):
 ctrl = _lock(db)
 if ctrl.active_store == 'split':
  raise HTTPException(409, '底稿已切换，无需迁移核对')
 ctrl.phase = 'verifying'
 ctrl.last_actor = _actor(user)
 db.flush()
 try:
  diffs = _validate_and_store(db, ctrl)
 except Exception as exc:
  db.rollback()
  raise HTTPException(409, '核对执行失败，已记录错误且未切换：' + str(exc))
 log(db, ctrl.last_actor, 'migration.verify', f'{len(diffs)}项差异')
 db.commit()
 return ctrl


def switch(db, user):
 """核对通过才全量切换。控制行锁内复核，任何差异或异常都不改变生效存储。"""
 ctrl = _lock(db)
 if ctrl.active_store == 'split':
  raise HTTPException(409, '已经切换到拆分存储')
 try:
  devices, rows, diffs = _verify_locked(db, ctrl)
 except Exception as exc:
  db.rollback()
  ctrl = _lock(db)
  ctrl.phase = 'switch_failed'
  ctrl.error = str(exc)
  ctrl.updated_at = time.time()
  db.commit()
  raise HTTPException(409, '核对失败，切换已取消，仍读取旧底稿：' + str(exc))
 _store_diffs(ctrl, devices, rows, diffs)
 if diffs:
  ctrl.phase = 'diff'
  ctrl.updated_at = time.time()
  db.commit()
  raise HTTPException(409, f'核对存在{len(diffs)}项差异，已阻止切换')
 try:
  ctrl.active_store = 'split'
  ctrl.phase = 'switched'
  ctrl.switched_at = time.time()
  ctrl.updated_at = time.time()
  ctrl.last_actor = _actor(user)
  db.flush()
  # 切换自检：拆分存储必须与旧存储班次数一致且能完整读出，否则同一事务内回滚
  if db.query(ShiftRecord).count() != db.query(Source).count():
   raise RuntimeError('拆分存储班次数与切换前不一致')
  stores.read_split(db, legacy_shape=False)
  log(db, ctrl.last_actor, 'migration.switch', f'{ctrl.copied_count}条班次')
  db.commit()
 except Exception as exc:
  db.rollback()
  ctrl = _lock(db)
  if ctrl.active_store == 'split':
   ctrl.active_store = 'legacy'
  ctrl.phase = 'switch_failed'
  ctrl.error = str(exc)
  ctrl.updated_at = time.time()
  db.commit()
  raise HTTPException(409, '切换失败，已回退为旧底稿：' + str(exc))
 return ctrl


def rollback(db, user):
 ctrl = _lock(db)
 if ctrl.active_store != 'split':
  raise HTTPException(409, '当前未切换，无需回退')
 ctrl.active_store = 'legacy'
 ctrl.phase = 'rolled_back'
 ctrl.rolled_back_at = time.time()
 ctrl.updated_at = time.time()
 ctrl.last_actor = _actor(user)
 log(db, ctrl.last_actor, 'migration.rollback', '回退到旧底稿存储；切换期间更正已同步保留')
 db.commit()
 return ctrl


def status(db):
 ctrl = stores.control(db)
 if not ctrl:
  total = db.query(Source).count()
  return {'active_store': 'legacy', 'phase': 'idle', 'legacy_cursor': 0, 'change_cursor': 0,
   'total_legacy': total, 'copied_count': 0, 'pending_changes': 0, 'remaining_legacy': total, 'percent': 0.0,
   'started_at': None, 'updated_at': None, 'verified_at': None, 'switched_at': None,
   'rolled_back_at': None, 'error': None, 'diffs': {'devices': [], 'rows': [], 'count': 0}}
 total = max(db.query(Source).count(), ctrl.legacy_cursor)
 pending = _pending(db, ctrl)
 diffs = ctrl.diffs or {'devices': [], 'rows': [], 'count': 0}
 return {'active_store': ctrl.active_store, 'phase': ctrl.phase,
  'legacy_cursor': ctrl.legacy_cursor, 'change_cursor': ctrl.change_cursor,
  'total_legacy': total, 'copied_count': ctrl.copied_count, 'pending_changes': pending,
  'remaining_legacy': db.query(Source).filter(Source.id > ctrl.legacy_cursor).count(),
  'percent': 100.0 if ctrl.active_store == 'split' else (round(min(100.0, ctrl.legacy_cursor * 100.0 / total), 1) if total else 0.0),
  'started_at': ctrl.started_at, 'updated_at': ctrl.updated_at, 'verified_at': ctrl.verified_at,
  'switched_at': ctrl.switched_at, 'rolled_back_at': ctrl.rolled_back_at,
  'error': ctrl.error, 'diffs': diffs}
