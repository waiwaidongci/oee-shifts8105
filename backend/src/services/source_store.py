import uuid,time
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.models.source import Source,Policy,Period
from src.models.shift import SourceRevision,SourceTombstone,SourceChange,MigrationControl,ShiftRecord,DowntimeInterval
from src.services.validation import validate_source,validate_policy
from src.services.audit import log


def control(db):
 return db.query(MigrationControl).filter_by(id=1).populate_existing().first()


def ensure_control(db):
 obj=control(db)
 if not obj:
  try:
   with db.begin_nested():
    obj=MigrationControl(id=1,active_store='legacy',phase='idle',legacy_cursor=0,change_cursor=0,total_legacy=0,copied_count=0)
    db.add(obj);db.flush()
  except IntegrityError:
   # 并发的首个请求已插入控制行
   obj=control(db)
 return obj


def lock_control(db):
 """写路径先取控制行锁再判断阶段，避免与核对/切换交错时漏镜像。"""
 ensure_control(db)
 return db.query(MigrationControl).filter_by(id=1).populate_existing().with_for_update().first()


def _mirror(db,source,locked=False):
 """迁移开始后（或已切换后）写旧底稿时同步拆分存储；迁移前靠变更日志在迁移时追平。"""
 ctrl=lock_control(db) if not locked else control(db)
 if ctrl.phase=='idle' and ctrl.active_store!='split': return
 sync_normalized(db,source)


def _revision(db,natural_key):
 row=db.query(SourceRevision).filter_by(natural_key=natural_key).populate_existing().first()
 return row.revision if row else 0


def _set_revision(db,natural_key,revision):
 row=db.query(SourceRevision).filter_by(natural_key=natural_key).populate_existing().first()
 if row: row.revision=revision;row.updated=time.time()
 else: db.add(SourceRevision(natural_key=natural_key,revision=revision,updated=time.time()))


def _journal(db,natural_key,operation,revision):
 db.add(SourceChange(natural_key=natural_key,operation=operation,revision=revision,at=time.time()))


def _num(value):
 """还原旧底稿的整数/小数表现，保证切换前后快照与CSV完全一致。"""
 return int(value) if isinstance(value,float) and value.is_integer() else value


def normalize_data(data):
 """把底稿中的整数值浮点数统一为整数；两套存储共用，避免切换后快照仅因类型不同而变。"""
 return {**data,'start':_num(data['start']),'end':_num(data['end']),
  'ideal_seconds':_num(data['ideal_seconds']),'total':_num(data['total']),'good':_num(data['good']),
  'downtime':[[_num(a),_num(b)] for a,b in data['downtime']]}


def _delete_normalized(db,natural_key):
 row=db.query(ShiftRecord).filter_by(natural_key=natural_key).populate_existing().first()
 if row:
  db.query(DowntimeInterval).filter_by(shift_id=row.id).delete(synchronize_session=False)
  db.delete(row)


def sync_normalized(db,source,revision=None):
 """把一条旧底稿幂等写入拆分表；旧修订号不得覆盖新修订号。

 复制批次与业务写入可能同时到达同一自然键；插入冲突时回到保存点重读并更新，
 因此重试、并发与断点续跑都不会产生重复班次。
 """
 key=source.natural_key
 if db.query(SourceTombstone).filter_by(natural_key=key).first(): return False
 if revision is None: revision=_revision(db,key)
 data=source.data
 row=db.query(ShiftRecord).filter_by(natural_key=key).populate_existing().first()
 if row and row.revision>revision: return False
 if not row:
  try:
   with db.begin_nested():
    row=ShiftRecord(natural_key=key,source_id=source.id,revision=revision,
     department=data['department'],work_date=data['period'],start_minute=data['start'],
     end_minute=data['end'],ideal_seconds=data['ideal_seconds'],total_count=data['total'],
     good_count=data['good'],updated=time.time())
    db.add(row)
    db.flush()
  except IntegrityError:
   row=db.query(ShiftRecord).filter_by(natural_key=key).populate_existing().first()
   if not row: raise
   if row.revision>revision: return False
 row.source_id=source.id;row.department=data['department'];row.work_date=data['period']
 row.start_minute=data['start'];row.end_minute=data['end'];row.ideal_seconds=data['ideal_seconds']
 row.total_count=data['total'];row.good_count=data['good'];row.revision=revision;row.updated=time.time()
 db.flush()
 db.query(DowntimeInterval).filter_by(shift_id=row.id).delete(synchronize_session=False)
 for ordinal,(start,end) in enumerate(data['downtime']):
  db.add(DowntimeInterval(shift_id=row.id,ordinal=ordinal,start_minute=start,end_minute=end))
 return True


def sync_natural_key(db,natural_key):
 """按源数据当前状态追平一个自然键；重复执行结果相同，过期数据不会被写入。

 修订号在读取数据前后各读一次；若期间有并发写入就放弃本轮同步（返回 None），
 由下一轮重新读取，避免把旧数据配上新修订号写入拆分表。
 """
 for _ in range(5):
  before=_revision(db,natural_key)
  source=db.query(Source).filter_by(natural_key=natural_key).populate_existing().first()
  after=_revision(db,natural_key)
  if before!=after: continue
  if source: return sync_normalized(db,source,revision=after)
  _delete_normalized(db,natural_key)
  return False
 return None


def _shape(row,intervals):
 return {'natural_key':row.natural_key,'id':row.source_id if row.source_id is not None else row.id,'revision':row.revision,
  'data':{'period':row.work_date,'department':row.department,'start':_num(float(row.start_minute)),
   'end':_num(float(row.end_minute)),'ideal_seconds':_num(row.ideal_seconds),'total':int(row.total_count),
   'good':int(row.good_count),'downtime':[[_num(float(a)),_num(float(b))] for a,b in intervals]}}


def read_split(db,period=None,department=None,legacy_shape=True):
 query=(select(ShiftRecord,DowntimeInterval).outerjoin(DowntimeInterval,DowntimeInterval.shift_id==ShiftRecord.id)
  .execution_options(populate_existing=True))
 if period and period!='全部': query=query.where(ShiftRecord.work_date==period)
 if department: query=query.where(ShiftRecord.department==department)
 query=query.order_by(ShiftRecord.source_id,ShiftRecord.id,DowntimeInterval.ordinal)
 records={};intervals={}
 for row,down in db.execute(query):
  if row.id not in records: records[row.id]=row;intervals[row.id]=[]
  if down is not None: intervals[row.id].append([down.start_minute,down.end_minute])
 out=[]
 for id,row in records.items():
  item=_shape(row,intervals[id])
  out.append(item if legacy_shape else item['data'])
 return out


def read_legacy(db,period=None,department=None,legacy_shape=True):
 revisions={r.natural_key:r.revision for r in db.query(SourceRevision).all()}
 query=db.query(Source).populate_existing()
 if period and period!='全部': query=query.filter(Source.data['period'].as_string()==period)
 if department: query=query.filter(Source.data['department'].as_string()==department)
 out=[]
 for row in query.order_by(Source.id).all():
  item={'natural_key':row.natural_key,'id':row.id,'revision':revisions.get(row.natural_key,0),'data':normalize_data(row.data)}
  out.append(item if legacy_shape else item['data'])
 return out


def _active_store(db):
 # 直接读列，绕过身份映射缓存；切换提交后一定能读到新标记
 value=db.execute(select(MigrationControl.active_store).where(MigrationControl.id==1)).first()
 return value[0] if value else 'legacy'


def read_sources(db,period=None,department=None,legacy_shape=True):
 """按当前生效底稿读取。先记存储标记，读完复核标记未变，避免切换瞬间读写混用两套存储。"""
 for _ in range(3):
  store=_active_store(db)
  items=(read_split if store=='split' else read_legacy)(db,period,department,legacy_shape)
  if _active_store(db)==store: return items
 return items


def freeze_sources(db,period):
 """核算提交时冻结一份完整一致的底稿；期间过滤下推到数据库，避免全量载入。"""
 return read_sources(db,period=period,legacy_shape=False)


def source_periods(db):
 if _active_store(db)=='split':
  return sorted({name for (name,) in db.query(ShiftRecord.work_date).distinct().all()})
 return sorted({row.data.get('period') for row in db.query(Source).all() if row.data.get('period')})


def create_source(db,user,data,natural_key=None,commit=True):
 validate_source(data)
 key=natural_key or 'user-'+uuid.uuid4().hex
 if db.query(Source).filter_by(natural_key=key).first(): raise HTTPException(409,'底稿自然键已存在')
 lock_control(db)
 obj=Source(natural_key=key,data=data);db.add(obj);db.flush()
 db.query(SourceTombstone).filter_by(natural_key=key).delete(synchronize_session=False)
 _set_revision(db,key,1);_journal(db,key,'create',1)
 _mirror(db,obj,locked=True)
 if not db.query(Period).filter_by(name=data['period']).first(): db.add(Period(name=data['period'],description='业务来源自动登记期间'))
 log(db,getattr(user,'email',str(user)),'source.create',key)
 if commit: db.commit()
 return obj


def update_source(db,user,data,id):
 validate_source(data)
 lock_control(db)
 obj=db.get(Source,id)
 if not obj: raise HTTPException(404,'底稿不存在')
 revision=_revision(db,obj.natural_key)+1
 obj.data=data;db.add(obj)
 _set_revision(db,obj.natural_key,revision);_journal(db,obj.natural_key,'update',revision)
 _mirror(db,obj,locked=True)
 if not db.query(Period).filter_by(name=data['period']).first(): db.add(Period(name=data['period'],description='业务来源自动登记期间'))
 log(db,getattr(user,'email',str(user)),'source.update',obj.natural_key)
 db.commit();return obj


def delete_source(db,user,id):
 ctrl=lock_control(db)
 obj=db.get(Source,id)
 if not obj: raise HTTPException(404,'底稿不存在')
 revision=_revision(db,obj.natural_key)+1
 key=obj.natural_key
 tomb=db.get(SourceTombstone,key)
 if tomb: tomb.revision=revision;tomb.deleted_at=time.time()
 else: db.add(SourceTombstone(natural_key=key,revision=revision,deleted_at=time.time()))
 if ctrl.phase!='idle' or ctrl.active_store=='split':
  _delete_normalized(db,key)
 _journal(db,key,'delete',revision)
 db.delete(obj)
 log(db,getattr(user,'email',str(user)),'source.delete',key)
 db.commit();return {'deleted':id}


def save_policy(db,user,data):
 validate_policy(data)
 if db.query(Policy).filter_by(version=data['version']).first(): raise HTTPException(409,'版本名已存在，请另建版本保留历史')
 obj=Policy(version=data['version'],data=data);db.add(obj)
 log(db,getattr(user,'email',str(user)),'policy.version',data['version']);db.commit();return obj
