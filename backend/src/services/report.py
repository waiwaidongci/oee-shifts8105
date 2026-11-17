import hashlib, json, time
from fastapi import HTTPException
from src.models.source import Policy
from src.models.run import Run
from src.services.audit import log
from src.services.source_store import freeze_sources
from src.config.settings import REDIS_URL
import redis


def submit(db, user, body):
 policy = db.get(Policy, body.policy_id)
 if not policy:
  raise HTTPException(404, '口径版本不存在')
 # 期间过滤下推到存储层，且只在存储标记稳定时读取，保证快照要么全旧要么全新
 sources = freeze_sources(db, body.period)
 if not sources:
  raise HTTPException(422, '所选期间没有底稿')
 snapshot = {'policy': policy.data, 'sources': sources, 'period': body.period}
 checksum = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
 obj = Run(name=body.name, status='queued', snapshot=snapshot, checksum=checksum, created=time.time(), approved=False)
 db.add(obj)
 log(db, user.email, 'run.queue', body.name)
 db.commit()
 redis.Redis.from_url(REDIS_URL).rpush('report-jobs', obj.id)
 return obj


def approve(db, user, id, note):
 obj = db.get(Run, id)
 if not obj:
  raise HTTPException(404, '报告不存在')
 if obj.status != 'completed':
  raise HTTPException(409, '仅可复核已完成报告')
 if obj.approved:
  raise HTTPException(409, '报告已经复核')
 obj.approved = True
 obj.review = note
 log(db, user.email, 'run.approve', id)
 db.commit()
 return obj
