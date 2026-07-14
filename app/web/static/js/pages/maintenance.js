document.addEventListener('alpine:init', () => {
    Alpine.data('operations', () => ({
        tab: 'issues',
        loading: false,
        repairing: false,
        backupBusy: false,
        sourcesBusy: false,
        renameBusy: false,
        message: '',
        messageType: 'success',
        build: {},
        dashboard: { summary: {}, items: [] },
        health: {},
        queueItems: [],
        tasks: [],
        backups: [],
        actressNames: [],
        actressUnverified: 0,
        aliasReview: { summary: {}, unrecognized: [], multi_chinese: [], merge_candidates: [], suggested_attach: [], mixed_script: [] },
        aliasReviewBusy: false,
        watchEnabled: false,
        selectedPaths: [],

        // data sources
        sources: [],
        dependencies: {},
        sourceSummary: {},

        // rename
        renameIssues: [],
        renamePreview: [],
        renameHistory: [],
        selectedRenamePaths: [],

        // locks
        lockItems: [],
        locksBusy: false,

        // duplicates
        dupGroups: [],
        dupSummary: {},
        dupBusy: false,

        // diagnostics
        diagPacks: [],
        diagBusy: false,

        async json(url, options = {}) {
            const response = await fetch(url, options);
            const data = await response.json();
            if (!response.ok || data.success === false) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
            return data;
        },
        notify(text, type = 'success') {
            this.message = text;
            this.messageType = type;
        },
        async init() { await this.refreshAll(); setInterval(() => this.loadTasks(), 5000); },
        async refreshAll() {
            this.loading = true;
            try {
                await Promise.all([
                    this.loadDashboard(),
                    this.loadQueue(),
                    this.loadTasks(),
                    this.loadBackups(),
                    this.loadNames(),
                    this.loadBuild(),
                ]);
            } catch (e) { this.notify(e.message, 'error'); }
            finally { this.loading = false; }
        },
        async loadBuild() { const data = await this.json('/api/maintenance/status'); this.build = data.build; },
        async loadDashboard() {
            const [dashboard, health] = await Promise.all([
                this.json('/api/operations/dashboard'),
                this.json('/api/showcase/health-check?include_details=true'),
            ]);
            this.dashboard = dashboard;
            this.health = health.summary || {};
        },
        async repairSelected() {
            this.repairing = true;
            try {
                const data = await this.json('/api/operations/repair', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({paths: this.selectedPaths}),
                });
                this.notify(`已补全 ${data.updated} 部，失败 ${data.failed} 部`, data.failed ? 'error' : 'success');
                this.selectedPaths = [];
                await this.loadDashboard();
            } catch (e) { this.notify(e.message, 'error'); }
            finally { this.repairing = false; }
        },
        async loadQueue() {
            const [items, status] = await Promise.all([this.json('/api/automation/items'), this.json('/api/automation/status')]);
            this.queueItems = items.items;
            this.watchEnabled = !!status.config.watch_enabled;
        },
        async saveWatch() {
            try {
                await this.json('/api/automation/watch', {
                    method: 'PUT', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({enabled:this.watchEnabled, interval_seconds:60, settle_seconds:30}),
                });
                this.notify(this.watchEnabled ? '新文件监控已开启' : '新文件监控已关闭');
            } catch (e) { this.watchEnabled = !this.watchEnabled; this.notify(e.message, 'error'); }
        },
        async scanNow() { try { const d=await this.json('/api/automation/scan-now',{method:'POST'}); this.notify(`发现 ${d.discovered} 个新文件`); await this.loadQueue(); } catch(e){this.notify(e.message,'error');} },
        async previewQueue(item) { try { await this.json(`/api/automation/items/${item.id}/preview`,{method:'POST'}); await this.loadQueue(); } catch(e){this.notify(e.message,'error');} },
        async applyQueue(item) { if(!window.confirm(`确认整理 ${item.number}？`)) return; try { await this.json(`/api/automation/items/${item.id}/apply`,{method:'POST'}); this.notify(`${item.number} 已整理`); await this.loadQueue(); await this.loadDashboard(); } catch(e){this.notify(e.message,'error');} },
        async dismissQueue(item) { try { await this.json(`/api/automation/items/${item.id}/dismiss`,{method:'POST'}); await this.loadQueue(); } catch(e){this.notify(e.message,'error');} },
        async rollbackQueue(item) { if(!window.confirm(`回退 ${item.number} 的整理结果？`)) return; try { await this.json(`/api/automation/items/${item.id}/rollback`,{method:'POST'}); this.notify(`${item.number} 已回退`); await this.loadQueue(); } catch(e){this.notify(e.message,'error');} },
        async loadTasks() { try { const d=await this.json('/api/tasks'); this.tasks=d.items; } catch(_e){} },
        async controlTask(task, action) { try { await this.json(`/api/tasks/${task.id}/${action}`,{method:'POST'}); await this.loadTasks(); } catch(e){this.notify(e.message,'error');} },
        async loadBackups() {
            const [d, diag] = await Promise.all([
                this.json('/api/maintenance/backups'),
                this.json('/api/maintenance/diagnostics').catch(() => ({ items: [] })),
            ]);
            this.backups = d.items;
            this.diagPacks = diag.items || [];
        },
        async createBackup() { this.backupBusy=true; try { await this.json('/api/maintenance/backups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'manual-ui'})}); this.notify('备份已创建'); await this.loadBackups(); } catch(e){this.notify(e.message,'error');} finally{this.backupBusy=false;} },
        async restoreBackup(backup) { if(!window.confirm(`恢复备份 ${backup.name}？恢复后需要重启软件。`)) return; try { await this.json('/api/maintenance/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:backup.name})}); this.notify('恢复完成，请重启 OpenAver'); } catch(e){this.notify(e.message,'error');} },
        async createDiagPack() {
            this.diagBusy = true;
            try {
                const data = await this.json('/api/maintenance/diagnostics', { method: 'POST' });
                this.notify(`诊断包已生成：${data.pack?.name || ''}`);
                await this.loadBackups();
            } catch (e) { this.notify(e.message, 'error'); }
            finally { this.diagBusy = false; }
        },
        async loadNames() { const d=await this.json('/api/operations/actress-names'); this.actressUnverified=d.unverified; this.actressNames=d.items.map(item=>({...item,source_url:item.evidence?.source_url||'',verified:!!item.evidence?.verified,confidence:item.evidence?.confidence??0.8})); },
        async saveEvidence(item) { try { await this.json(`/api/operations/actress-names/${encodeURIComponent(item.primary_name)}/evidence`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_url:item.source_url,confidence:item.confidence,verified:item.verified,notes:''})}); this.notify(`${item.primary_name} 已保存`); await this.loadNames(); } catch(e){this.notify(e.message,'error');} },
        async loadAliasReview() {
            this.aliasReviewBusy = true;
            try {
                const d = await this.json('/api/operations/actress-alias-review?limit=80');
                this.aliasReview = d;
            } catch (e) { this.notify(e.message, 'error'); }
            finally { this.aliasReviewBusy = false; }
        },
        async mergeAliasGroups(c) {
            if (!window.confirm(`将「${c.absorb}」合并到「${c.keep}」？`)) return;
            this.aliasReviewBusy = true;
            try {
                await this.json('/api/operations/actress-alias-merge', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ keep: c.keep, absorb: c.absorb }),
                });
                this.notify(`已合并：${c.absorb} → ${c.keep}`);
                await Promise.all([this.loadNames(), this.loadAliasReview()]);
            } catch (e) { this.notify(e.message, 'error'); }
            finally { this.aliasReviewBusy = false; }
        },
        async attachAliasName(s) {
            this.aliasReviewBusy = true;
            try {
                await this.json('/api/operations/actress-alias-attach', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: s.name, attach_to: s.attach_to }),
                });
                this.notify(`已挂接 ${s.name} → ${s.attach_to}`);
                await Promise.all([this.loadNames(), this.loadAliasReview()]);
            } catch (e) { this.notify(e.message, 'error'); }
            finally { this.aliasReviewBusy = false; }
        },
        async createAliasFromUnrecognized(u) {
            this.aliasReviewBusy = true;
            try {
                await this.json('/api/actress-aliases', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ primary_name: u.name, aliases: [] }),
                });
                this.notify(`已新建别名组：${u.name}`);
                await Promise.all([this.loadNames(), this.loadAliasReview()]);
            } catch (e) { this.notify(e.message, 'error'); }
            finally { this.aliasReviewBusy = false; }
        },

        // ── Data sources ──────────────────────────────────────────
        async loadSources(probe = false) {
            this.sourcesBusy = true;
            try {
                const data = await this.json(`/api/diagnostics/sources?probe=${probe ? 'true' : 'false'}`);
                this.sources = data.sources || [];
                this.dependencies = data.dependencies || {};
                this.sourceSummary = data.summary || {};
                if (probe) this.notify(`连通性探测完成：健康 ${this.sourceSummary.healthy || 0}，异常 ${this.sourceSummary.degraded || 0}`);
            } catch (e) {
                this.notify(e.message, 'error');
            } finally {
                this.sourcesBusy = false;
            }
        },

        // ── Rename ────────────────────────────────────────────────
        async loadRename() {
            try {
                const [issues, history] = await Promise.all([
                    this.json('/api/operations/rename-issues'),
                    this.json('/api/operations/rename-history'),
                ]);
                this.renameIssues = issues.items || [];
                this.renameHistory = history.items || [];
            } catch (e) {
                this.notify(e.message, 'error');
            }
        },
        selectAllRename() {
            this.selectedRenamePaths = this.renameIssues.map((i) => i.path);
        },
        async previewRename() {
            this.renameBusy = true;
            try {
                const data = await this.json('/api/operations/rename-preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: this.selectedRenamePaths, dry_run: true, rename_folder: true }),
                });
                this.renamePreview = data.results || [];
                this.notify(`预览：将重命名 ${data.would_rename || 0} 项`);
            } catch (e) {
                this.notify(e.message, 'error');
            } finally {
                this.renameBusy = false;
            }
        },
        async applyRename() {
            if (!window.confirm(`确认重命名选中的 ${this.selectedRenamePaths.length} 项？可在历史中回退。`)) return;
            this.renameBusy = true;
            try {
                const data = await this.json('/api/operations/rename-apply', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: this.selectedRenamePaths, dry_run: false, rename_folder: true }),
                });
                let msg =
                    `已重命名 ${data.renamed || 0}，跳过 ${data.skipped || 0}，失败 ${data.failed || 0}` +
                    (data.journal_id ? `（记录 ${data.journal_id}）` : '');
                if (data.truncated) {
                    msg += `；仅处理前 ${data.limit || 50}/${data.requested || '?'} 项（truncated）`;
                }
                this.notify(msg, data.failed || data.truncated ? 'error' : 'success');
                this.renamePreview = [];
                this.selectedRenamePaths = [];
                await this.loadRename();
                await this.loadDashboard();
            } catch (e) {
                this.notify(e.message, 'error');
            } finally {
                this.renameBusy = false;
            }
        },
        async rollbackRename(ev) {
            if (!window.confirm(`回退重命名记录 ${ev.id}？`)) return;
            this.renameBusy = true;
            try {
                const data = await this.json('/api/operations/rename-rollback', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ event_id: ev.id }),
                });
                this.notify(`已回退 ${data.restored || 0} 项`, data.success ? 'success' : 'error');
                await this.loadRename();
            } catch (e) {
                this.notify(e.message, 'error');
            } finally {
                this.renameBusy = false;
            }
        },

        // ── Field locks ───────────────────────────────────────────
        async loadLocks() {
            this.locksBusy = true;
            try {
                const data = await this.json('/api/operations/field-locks');
                this.lockItems = data.items || [];
            } catch (e) {
                this.notify(e.message, 'error');
            } finally {
                this.locksBusy = false;
            }
        },
        async unlockField(item, field) {
            try {
                await this.json('/api/operations/field-lock', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: item.path, field, locked: false }),
                });
                this.notify(`已解锁 ${item.number || ''} · ${field}`);
                await this.loadLocks();
            } catch (e) {
                this.notify(e.message, 'error');
            }
        },

        // ── Duplicates ────────────────────────────────────────────
        async loadDuplicates(withHash = false) {
            this.dupBusy = true;
            try {
                const data = await this.json(`/api/operations/duplicates?compute_hash=${withHash ? 'true' : 'false'}&limit=80`);
                this.dupSummary = data.summary || {};
                const groups = [];
                for (const g of (data.by_number || [])) groups.push(g);
                for (const g of (data.by_size || [])) groups.push(g);
                for (const g of (data.by_hash || [])) groups.push(g);
                this.dupGroups = groups;
                this.notify(
                    withHash
                        ? `重复检测完成（含指纹）：番号组 ${this.dupSummary.number_duplicate_groups || 0}，哈希组 ${this.dupSummary.hash_duplicate_groups || 0}`
                        : `重复检测完成：番号组 ${this.dupSummary.number_duplicate_groups || 0}，大小组 ${this.dupSummary.size_duplicate_groups || 0}`,
                );
            } catch (e) {
                this.notify(e.message, 'error');
            } finally {
                this.dupBusy = false;
            }
        },
    }));
});
