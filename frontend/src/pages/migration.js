import {heading} from '../components/common/header.js';import {table} from '../components/common/table.js';import {esc,time} from '../utils/format.js';import {store} from '../stores/business.js';
const PHASES={idle:'未开始',running:'迁移中',paused:'已暂停',verifying:'核对中',verified:'核对通过',diff:'存在差异',switched:'已切换',rolled_back:'已回退',error:'异常',switch_failed:'切换失败'};
const STORES={legacy:'旧底稿（单条JSON）',split:'拆分存储（班次+停机）'};
const pct=v=>Math.max(0,Math.min(100,Number(v)||0)).toFixed(1)+'%';
const can=(c,s)=>c.id!=='gb-0047'||s?.role==='admin';
const button=(action,label,disabled=false,kind='')=>`<button class="migration-action ${kind}" data-action="${action}" ${disabled?'disabled':''}>${label}</button>`;
export function migration(c,d){
 const s=d.migration||{},diffs=s.diffs||{},devices=diffs.devices||[],rows=diffs.rows||[],phase=s.phase||'idle';
 const percent=s.active_store==='split'?100:Number(s.percent||0);
 const badge=`<span class="status ${phase==='diff'||phase==='error'||phase==='switch_failed'?'failed':''}">${esc(PHASES[phase]||phase)}</span>`;
 const progress=`<section class="migration-card"><div class="migration-head"><h2>底稿迁移进度</h2>${badge}</div>
  <div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div>
  <div class="metrics"><div><small>已复制班次</small><strong>${s.copied_count??0} / ${s.total_legacy??0}</strong></div>
  <div><small>待追平变更</small><strong>${s.pending_changes??0}</strong></div>
  <div><small>迁移断点</small><strong>${s.legacy_cursor??0}</strong></div>
  <div><small>当前读取</small><strong class="store-name">${esc(STORES[s.active_store]||s.active_store||'旧底稿')}</strong></div></div>
  <p class="method">迁移期间底稿的增改删照常可用；worker 按变更日志追平，已删班次写入墓碑不会复活，重试按自然键幂等，进程中断后从断点续跑。核算提交时冻结一份完整底稿快照。</p>
  ${s.error?`<p class="error">${esc(s.error)}</p>`:''}
  <div class="migration-actions">${can(c,store.session)?`${button('start','开始迁移',s.active_store==='split'||phase==='running')}${button('restart','从零重迁',s.active_store==='split')}${button('pause','暂停',phase!=='running')}${button('verify','复核核对',s.active_store==='split'||phase==='running')}${button('switch','核对并切换',s.active_store==='split')}${button('rollback','回退到旧底稿',s.active_store!=='split','ghost')}`:'<p class="empty">仅负责人可以执行迁移、切换与回退。</p>'}</div>
  <p class="checksum">已核对：${s.verified_at?time(s.verified_at):'—'} · 已切换：${s.switched_at?time(s.switched_at):'—'} · 已回退：${s.rolled_back_at?time(s.rolled_back_at):'—'}</p></section>`;
 const deviceTable=devices.length?table(devices):'<div class="empty">尚未核对。开始迁移后 worker 复制完成会自动逐设备核对。</div>';
 const rowTable=rows.length?table(rows.filter(r=>!r['一致'])):'<div class="empty">当前没有差异行。</div>';
 const blocked=diffs.count>0||diffs.blocked===true;
 return heading('MIGRATION CONSOLE / 07','底稿拆分迁移与切换核对','班次与停机拆分存储，按设备/日期读取；核对班次数量、停机并集与 OEE，通过后才切换。')+progress+
  `<h2>逐设备核对</h2><p class="method">按设备汇总班次数量、总产量、合格数、停机并集分钟与汇总 OEE（不简单平均各班 OEE）。任一设备不一致即阻止切换。</p>${deviceTable}`+
  `<h2>差异明细</h2><p class="method">差异 ${diffs.count??0} 项${blocked?'，切换已阻止':'，可执行切换'}。切换前系统会在控制行锁内重新核对并追平全部变更。</p>${rowTable}`;
}
export const migrationPage=migration;
