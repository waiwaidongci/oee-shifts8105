# 002 班次与停机拆分存储迁移

## 背景

原实现把每条班次（含停机区间 JSON）存在 `downtimeevents.data` 单列 JSON 中，核算与页面每次全量载入。现在新增拆分存储并按设备/日期读取，历史数据通过可中断、可重试的迁移导入；切换前旧存储仍是唯一事实来源，切换后才读新表。

新增表（SQLAlchemy 启动时自动创建）：

- `shift_records`：班次主记录，`natural_key` 唯一，`source_id` 对应旧底稿自增 ID 以沿用业务编号，`(department, work_date)` 联合索引。
- `downtime_intervals`：停机区间，按 `shift_id` 级联，`ordinal` 保留录入顺序。
- `source_revisions`：每个自然键的修订号，旧底稿写入时自增，迁移重放用其防止旧批次覆盖新写入。
- `source_change`：变更日志（create/update/delete），id 单调递增，是迁移追平、重试和回退的依据。
- `source_tombstones`：删除墓碑。已删除自然键的墓碑会让任何滞后复制/重放跳过，班次不会复活。
- `migration_control`：单例控制行，记录 `active_store`、阶段、两个断点、核对结果与差异。

## 运行步骤

1. 负责人访问「迁移与切换」页或 `POST /api/migration/start`；worker 每轮复制 `MIGRATION_BATCH`（默认 50）条并消费变更日志。
2. 迁移期间业务底稿照常增改删：写入旧表同时（迁移开始后）镜像新表，并追加变更日志。
3. 复制与追平完成后 worker 自动核对，也可 `POST /api/migration/verify` 手动复核。核对逐设备、逐设备日期比较班次数量、总产量、合格数、停机并集分钟与汇总 OEE。
4. 存在差异时 `POST /api/migration/switch` 返回 409 且不切换，差异保留在页面。
5. 核对通过后切换：在 `migration_control` 行锁内再次追平并核对，随后把 `active_store` 置为 `split` 并在同一事务内自检；异常则回滚事务且保持旧存储，阶段记为 `switch_failed`。
6. `POST /api/migration/rollback` 可随时回退到旧存储；切换后对底稿的更正同时写入两套存储，回退不丢失。
7. 中断（进程重启/暂停）后再次 `start` 从断点续跑；`restart` 清空拆分表从零重迁，结果与续跑一致。

## 一致性与幂等

- 快照：核算提交时先读存储标记、读完再确认标记未变，保证冻结的底稿完整来自一套存储；期间过滤在数据库侧完成。
- 幂等：复制与变更重放都按 `natural_key` upsert，重复执行不产生重复班次或停机区间。
- 不复活：删除写墓碑，复制批次遇到墓碑只推进断点。
- 追平：迁移期间任何写入都会记录变更日志；核对与切换前都会先消费完日志再看差异。
- 原有报告、交付与 CSV 导出结构不变：读取层把拆分记录还原为与原底稿相同的 JSON 结构。

## 回滚与升级注意

已有库升级前先备份。新增表由启动时 `create_all` 创建，不修改旧表；如以后需要删除旧表，应在确认切换稳定后以独立迁移执行。`MIGRATION_BATCH` 可调整每轮复制条数。
