from sqlalchemy import Column,Integer,String,Float,Text,JSON,ForeignKey,Index
from src.config.database import Base


class ShiftRecord(Base):
 """班次主记录：按设备与日期建索引，核算与页面不再全量加载单条JSON底稿。"""
 __tablename__ = 'shift_records'
 id = Column(Integer, primary_key=True)
 natural_key = Column(String(120), unique=True, nullable=False)
 source_id = Column(Integer, unique=True, nullable=True)  # 对应旧底稿自增ID，切换后仍沿用同一业务编号
 department = Column(String(100), nullable=False)
 work_date = Column(String(20), nullable=False)
 start_minute = Column(Integer, nullable=False)
 end_minute = Column(Integer, nullable=False)
 ideal_seconds = Column(Float, nullable=False)
 total_count = Column(Integer, nullable=False)
 good_count = Column(Integer, nullable=False)
 revision = Column(Integer, nullable=False, default=0)
 updated = Column(Float, nullable=False, default=0)
 __table_args__ = (Index('ix_shift_device_date', 'department', 'work_date'),)


class DowntimeInterval(Base):
 """停机区间拆分存储；同一班次内保留原始顺序，核算时再裁剪合并。"""
 __tablename__ = 'downtime_intervals'
 id = Column(Integer, primary_key=True)
 shift_id = Column(Integer, ForeignKey('shift_records.id', ondelete='CASCADE'), nullable=False, index=True)
 ordinal = Column(Integer, nullable=False, default=0)
 start_minute = Column(Float, nullable=False)
 end_minute = Column(Float, nullable=False)


class SourceRevision(Base):
 """旧底稿的修订号，不改变现有底稿JSON结构，用于迁移时防止旧批次覆盖新写入。"""
 __tablename__ = 'source_revisions'
 natural_key = Column(String(120), primary_key=True)
 revision = Column(Integer, nullable=False, default=0)
 updated = Column(Float, nullable=False, default=0)


class SourceTombstone(Base):
 """删除墓碑：迁移重试或滞后批次不得把已删除班次重新复制回来。"""
 __tablename__ = 'source_tombstones'
 natural_key = Column(String(120), primary_key=True)
 revision = Column(Integer, nullable=False, default=0)
 deleted_at = Column(Float, nullable=False, default=0)


class SourceChange(Base):
 """底稿变更日志。迁移追平、重试与回退都依据该序号，处理函数幂等。"""
 __tablename__ = 'source_changes'
 id = Column(Integer, primary_key=True, autoincrement=True)
 natural_key = Column(String(120), nullable=False, index=True)
 operation = Column(String(20), nullable=False)
 revision = Column(Integer, nullable=False, default=0)
 at = Column(Float, nullable=False, default=0)


class MigrationControl(Base):
 """迁移单例控制行。切换/回退都在该行锁内完成，给核算冻结一个明确的分界。"""
 __tablename__ = 'migration_control'
 id = Column(Integer, primary_key=True)
 active_store = Column(String(20), nullable=False, default='legacy')
 phase = Column(String(30), nullable=False, default='idle')
 legacy_cursor = Column(Integer, nullable=False, default=0)
 change_cursor = Column(Integer, nullable=False, default=0)
 total_legacy = Column(Integer, nullable=False, default=0)
 copied_count = Column(Integer, nullable=False, default=0)
 started_at = Column(Float)
 updated_at = Column(Float)
 verified_at = Column(Float)
 switched_at = Column(Float)
 rolled_back_at = Column(Float)
 error = Column(Text)
 diffs = Column(JSON)
 last_actor = Column(String(120))
