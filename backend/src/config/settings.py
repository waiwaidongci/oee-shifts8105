import os,json
from pathlib import Path

def _config_path():
 for candidate in (os.getenv('PROJECT_CONFIG'),'/app/project.json',str(Path(__file__).resolve().parents[2]/'project.json')):
  if candidate and Path(candidate).is_file(): return Path(candidate)
 raise FileNotFoundError('未找到project.json，请设置PROJECT_CONFIG')

CONFIG=json.loads(_config_path().read_text())
DATABASE_URL=os.environ['DATABASE_URL']
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
SECRET=os.environ['JWT_SECRET']
MIGRATION_BATCH=int(os.getenv('MIGRATION_BATCH','50'))
