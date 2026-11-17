from sqlalchemy import create_engine,event
from sqlalchemy.orm import sessionmaker,DeclarativeBase
from src.config.settings import DATABASE_URL
class Base(DeclarativeBase): pass
_options={}
if DATABASE_URL.startswith('sqlite'):
 _options['connect_args']={'check_same_thread':False,'timeout':30}
engine=create_engine(DATABASE_URL,pool_pre_ping=True,**_options)
if DATABASE_URL.startswith('sqlite'):
 @event.listens_for(engine,'connect')
 def _sqlite_pragmas(connection,_):
  cursor=connection.cursor()
  cursor.execute('PRAGMA journal_mode=WAL')
  cursor.execute('PRAGMA busy_timeout=30000')
  cursor.execute('PRAGMA foreign_keys=ON')
  cursor.close()
Session=sessionmaker(bind=engine,expire_on_commit=False)
def database():
 with Session() as db: yield db
