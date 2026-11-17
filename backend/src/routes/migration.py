from fastapi import APIRouter, Depends
from src.config.database import database
from src.controllers.auth import current, admin
from src.services import migration
from src.utils.serialize import row
router = APIRouter()


@router.get('/migration')
def status(db=Depends(database), u=Depends(current)):
 return migration.status(db)


@router.post('/migration/start')
def start(db=Depends(database), u=Depends(admin)):
 return row(migration.start(db, u))


@router.post('/migration/restart')
def restart(db=Depends(database), u=Depends(admin)):
 return row(migration.start(db, u, restart=True))


@router.post('/migration/pause')
def pause(db=Depends(database), u=Depends(admin)):
 return row(migration.pause(db, u))


@router.post('/migration/verify')
def verify(db=Depends(database), u=Depends(admin)):
 return row(migration.verify(db, u))


@router.post('/migration/switch')
def switch(db=Depends(database), u=Depends(admin)):
 return row(migration.switch(db, u))


@router.post('/migration/rollback')
def rollback(db=Depends(database), u=Depends(admin)):
 return row(migration.rollback(db, u))
