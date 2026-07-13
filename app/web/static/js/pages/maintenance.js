document.addEventListener('alpine:init', () => {
    Alpine.data('operations', () => ({
        tab: 'issues',
        loading: false,
        repairing: false,
        backupBusy: false,
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
        watchEnabled: false,
        selectedPaths: [],

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
                await Promise.all([this.loadDashboard(), this.loadQueue(), this.loadTasks(), this.loadBackups(), this.loadNames(), this.loadBuild()]);
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
        async loadBackups() { const d=await this.json('/api/maintenance/backups'); this.backups=d.items; },
        async createBackup() { this.backupBusy=true; try { await this.json('/api/maintenance/backups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'manual-ui'})}); this.notify('备份已创建'); await this.loadBackups(); } catch(e){this.notify(e.message,'error');} finally{this.backupBusy=false;} },
        async restoreBackup(backup) { if(!window.confirm(`恢复备份 ${backup.name}？恢复后需要重启软件。`)) return; try { await this.json('/api/maintenance/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:backup.name})}); this.notify('恢复完成，请重启 OpenAver'); } catch(e){this.notify(e.message,'error');} },
        async loadNames() { const d=await this.json('/api/operations/actress-names'); this.actressUnverified=d.unverified; this.actressNames=d.items.map(item=>({...item,source_url:item.evidence?.source_url||'',verified:!!item.evidence?.verified,confidence:item.evidence?.confidence??0.8})); },
        async saveEvidence(item) { try { await this.json(`/api/operations/actress-names/${encodeURIComponent(item.primary_name)}/evidence`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_url:item.source_url,confidence:item.confidence,verified:item.verified,notes:''})}); this.notify(`${item.primary_name} 已保存`); await this.loadNames(); } catch(e){this.notify(e.message,'error');} },
    }));
});
