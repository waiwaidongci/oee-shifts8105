import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # backend/
sys.path.insert(0, str(ROOT))
os.environ.setdefault('PROJECT_CONFIG', str(ROOT / 'project.json'))
import tempfile
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(Path(tempfile.mkdtemp(prefix='oee-test-')) / 'test.db'))
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('MIGRATION_BATCH', '5')

import pytest
import redis


class FakeRedis:
 def __init__(self): self.queue = []

 def rpush(self, key, value): self.queue.append((key, value))

 def lpop(self, key):
  return None


@pytest.fixture(scope='session', autouse=True)
def _redis_stub():
 fake = FakeRedis()
 redis.Redis.from_url = classmethod(lambda cls, *a, **k: fake)
 return fake


@pytest.fixture(autouse=True)
def clean_db():
 from src.config.database import Base, engine
 from src.models import identity, source, run, shift
 Base.metadata.drop_all(engine)
 Base.metadata.create_all(engine)
 from src.services.source_store import ensure_control
 from src.config.database import Session
 with Session() as db:
  ensure_control(db)
  db.commit()
 yield
