import json

import pytest
from fastapi.testclient import TestClient

from src.config.database import Session
from src.main import app
from src.models.identity import User
from src.models.run import Run
from src.utils.security import password_hash
from src.services import migration, source_store
from tests.test_migration import seed_rows, run_tick, user


@pytest.fixture()
def client():
 with TestClient(app) as c:
  yield c


def login(client, role='admin'):
 with Session() as db:
  if not db.query(User).filter_by(email=f'{role}@demo.local').first():
   db.add(User(email=f'{role}@demo.local', role=role, password=password_hash('Demo2026!')))
   db.commit()
 token = client.post('/auth/login', json={'email': f'{role}@demo.local', 'password': 'Demo2026!'}).json()['token']
 return {'Authorization': 'Bearer ' + token}


def test_source_reads_by_device_and_date_and_roles(client):
 seed_rows(9)
 with Session() as db:
  migration.start(db, user())
 run_tick()
 with Session() as db:
  migration.switch(db, user())
 analyst = login(client, 'analyst')
 res = client.get('/sources', params={'department': '试制机B', 'period': '2026-11-02'}, headers=analyst)
 assert res.status_code == 200
 rows = res.json()
 assert rows and all(r['data']['department'] == '试制机B' and r['data']['period'] == '2026-11-02' for r in rows)
 facets = client.get('/source-facets', headers=analyst).json()
 assert facets['active_store'] == 'split'
 assert facets['departments'] == ['试制机A', '试制机B', '试制机C']
 viewer = login(client, 'viewer')
 assert client.get('/sources', headers=viewer).status_code == 200
 create = client.post('/sources', json={'data': {}}, headers=viewer)
 assert create.status_code == 403


def test_migration_page_api_roles_and_switch(client):
 seed_rows(6)
 admin = login(client, 'admin')
 analyst = login(client, 'analyst')
 assert client.post('/migration/start', headers=analyst).status_code == 403
 assert client.post('/migration/start', headers=admin).status_code == 200
 run_tick()
 status = client.get('/migration', headers=analyst).json()
 assert status['phase'] == 'verified'
 assert status['active_store'] == 'legacy'
 assert status['diffs']['devices']
 assert all(x['一致'] for x in status['diffs']['devices'])
 switched = client.post('/migration/switch', headers=admin)
 assert switched.status_code == 200
 assert client.get('/migration', headers=analyst).json()['active_store'] == 'split'
 assert client.post('/migration/rollback', headers=admin).status_code == 200
 assert client.get('/migration', headers=analyst).json()['active_store'] == 'legacy'


def test_run_snapshot_and_export_preserved_after_switch(client):
 seed_rows(9)
 with Session() as db:
  migration.start(db, user())
 run_tick()
 with Session() as db:
  migration.switch(db, user())
 analyst = login(client, 'analyst')
 admin = login(client, 'admin')
 policies = client.get('/policies', headers=analyst).json()
 run = client.post('/runs', json={'name': '十一月核算', 'policy_id': policies[0]['id'], 'period': '全部'}, headers=analyst)
 assert run.status_code == 200
 run_id = run.json()['id']
 with Session() as db:
  obj = db.get(Run, run_id)
  from src.services.calculation import calculate
  obj.result = calculate(obj.snapshot['sources'], obj.snapshot['policy'])
  obj.status = 'completed'
  db.commit()
 csv = client.get(f'/runs/{run_id}/csv', headers=analyst)
 assert csv.status_code == 200 and csv.text.startswith('﻿')
 assert 'OEE' in csv.text
 assert client.post(f'/runs/{run_id}/approve', json={'note': '数值已逐设备核对'}, headers=admin).status_code == 200
 assert client.get(f'/runs/{run_id}/csv', headers=login(client, 'viewer')).status_code == 200


def test_edit_and_delete_before_migration_are_caught_up(client):
 seed_rows(5)
 analyst = login(client, 'analyst')
 with Session() as db:
  from src.models.source import Source
  obj = db.query(Source).filter_by(natural_key='demo-1').first()
  target = obj.id
 client.put(f'/sources/{target}', json={'data': {'period': '2026-11-01', 'department': '试制机A', 'start': 480,
  'end': 1020, 'ideal_seconds': 25, 'total': 700, 'good': 700, 'downtime': [[500, 520]]}}, headers=analyst)
 with Session() as db:
  from src.models.source import Source
  other = db.query(Source).filter_by(natural_key='demo-3').first().id
 client.delete(f'/sources/{other}', headers=analyst)
 admin = login(client, 'admin')
 client.post('/migration/start', headers=admin)
 run_tick()
 status = client.get('/migration', headers=admin).json()
 assert status['phase'] == 'verified'
 keys = {r['natural_key'] for r in client.get('/sources', headers=admin).json()}
 assert 'demo-3' not in keys
 changed = [r for r in client.get('/sources', headers=admin).json() if r['natural_key'] == 'demo-1'][0]
 assert changed['data']['good'] == 700 and changed['data']['downtime'] == [[500, 520]]
