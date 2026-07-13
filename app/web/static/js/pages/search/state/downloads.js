/** Search page authorized-download state and interactions. */
export function searchStateDownloads() {
    return {
        downloadModalOpen: false,
        downloadLookupLoading: false,
        downloadStarting: false,
        downloadTasks: [],
        downloadDestinations: [],
        _downloadPollTimer: null,
        _downloadLookupGeneration: 0,
        downloadForm: {
            number: '', title: '', chineseTitle: '', mediaUrl: '', destination: '',
            rightsConfirmed: false, sourcePageUrl: '',
        },

        async _downloadJson(url, options = {}) {
            const response = await fetch(url, options);
            let data = {};
            try { data = await response.json(); } catch (_) { data = {}; }
            if (!response.ok) {
                const detail = data.detail;
                const message = typeof detail === 'string' ? detail : (detail?.reason || data.message || `HTTP ${response.status}`);
                const error = new Error(message);
                error.status = response.status;
                error.detail = detail;
                throw error;
            }
            return data;
        },

        async openDownloadModal() {
            const current = this.current();
            if (!current?.number) return;
            this.downloadForm = {
                number: current.number,
                title: current.title || '',
                chineseTitle: current.translated_title || this.chineseTitle() || '',
                mediaUrl: '',
                destination: '',
                rightsConfirmed: false,
                sourcePageUrl: current.url || '',
            };
            this.downloadModalOpen = true;
            await Promise.all([this.loadDownloadDestinations(), this.loadDownloadTasks()]);
            if (!this.downloadForm.destination && this.downloadDestinations.length) {
                this.downloadForm.destination = this.downloadDestinations[0];
            }
            this._startDownloadPolling();
        },

        closeDownloadModal() {
            this.downloadModalOpen = false;
            this.cleanupDownloadState();
        },

        cleanupDownloadState() {
            this._downloadLookupGeneration += 1;
            this.downloadLookupLoading = false;
            if (this._downloadPollTimer) {
                clearInterval(this._downloadPollTimer);
                this._downloadPollTimer = null;
            }
        },

        _startDownloadPolling() {
            this.cleanupDownloadState();
            this._downloadPollTimer = setInterval(() => {
                if (this.downloadModalOpen) this.loadDownloadTasks();
            }, 1000);
        },

        async loadDownloadDestinations() {
            try {
                const data = await this._downloadJson('/api/downloads/destinations');
                this.downloadDestinations = data.items || [];
            } catch (error) {
                this.downloadDestinations = [];
                this.showToast(error.message, 'error');
            }
        },

        async loadDownloadTasks() {
            try {
                const data = await this._downloadJson('/api/downloads');
                this.downloadTasks = data.items || [];
            } catch (error) {
                console.error('[Downloads] task refresh failed', error);
            }
        },

        async lookupJableTitles() {
            if (this.downloadLookupLoading || !this.downloadForm.number) return;
            const generation = ++this._downloadLookupGeneration;
            this.downloadLookupLoading = true;
            try {
                let data;
                for (let attempt = 0; attempt < 121; attempt++) {
                    if (generation !== this._downloadLookupGeneration) return;
                    try {
                        data = await this._downloadJson(`/api/downloads/jable-titles?number=${encodeURIComponent(this.downloadForm.number)}`);
                        break;
                    } catch (error) {
                        if (error.status !== 409 || error.detail?.reason !== 'cf_challenge') throw error;
                        if (attempt === 0) this.showToast(window.t('search.download.complete_browser_check'), 'info');
                        await new Promise(resolve => setTimeout(resolve, 1000));
                        if (generation !== this._downloadLookupGeneration) return;
                        const status = await this._downloadJson('/api/cf/status?key=jable');
                        if (!status.ready) continue;
                    }
                }
                if (!data) throw new Error(window.t('search.download.lookup_timeout'));
                const result = data.data || {};
                if (result.title_ja) this.downloadForm.title = result.title_ja;
                if (result.title_zh) this.downloadForm.chineseTitle = result.title_zh;
                if (result.candidates?.[0]?.url) this.downloadForm.sourcePageUrl = result.candidates[0].url;
                if (!result.title_ja && !result.title_zh) throw new Error(window.t('search.download.no_titles'));
                this.showToast(window.t('search.download.titles_loaded'), 'success');
            } catch (error) {
                const message = error.message === 'cf_unavailable'
                    ? window.t('search.download.desktop_required')
                    : error.message;
                this.showToast(message, 'error');
            } finally {
                if (generation === this._downloadLookupGeneration) this.downloadLookupLoading = false;
            }
        },

        async startAuthorizedDownload() {
            if (this.downloadStarting) return;
            this.downloadStarting = true;
            const current = this.current();
            try {
                await this._downloadJson('/api/downloads', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        media_url: this.downloadForm.mediaUrl.trim(),
                        destination: this.downloadForm.destination,
                        number: this.downloadForm.number,
                        title: this.downloadForm.title.trim(),
                        chinese_title: this.downloadForm.chineseTitle.trim(),
                        actors: current.actors || [],
                        tags: current.tags || [],
                        maker: current.maker || '',
                        date: current.date || '',
                        director: current.director || '',
                        series: current.series || '',
                        label: current.label || '',
                        cover: current.cover || '',
                        source_page_url: this.downloadForm.sourcePageUrl || current.url || '',
                        rights_confirmed: this.downloadForm.rightsConfirmed,
                    }),
                });
                this.downloadForm.mediaUrl = '';
                this.downloadForm.rightsConfirmed = false;
                await this.loadDownloadTasks();
                this.showToast(window.t('search.download.queued'), 'success');
            } catch (error) {
                this.showToast(error.message, 'error');
            } finally {
                this.downloadStarting = false;
            }
        },

        async controlDownload(task, action) {
            try {
                await this._downloadJson(`/api/downloads/${task.id}/${action}`, { method: 'POST' });
                await this.loadDownloadTasks();
            } catch (error) {
                this.showToast(error.message, 'error');
            }
        },

        async removeDownload(task) {
            try {
                await this._downloadJson(`/api/downloads/${task.id}`, { method: 'DELETE' });
                await this.loadDownloadTasks();
            } catch (error) {
                this.showToast(error.message, 'error');
            }
        },

        downloadCanStart() {
            return !!(this.downloadForm.mediaUrl.trim() && this.downloadForm.destination && this.downloadForm.rightsConfirmed);
        },

        downloadProgressText(task) {
            if (task.duration_seconds) {
                return `${this._formatDownloadTime(task.elapsed_seconds)} / ${this._formatDownloadTime(task.duration_seconds)}`;
            }
            return this._formatDownloadBytes(task.bytes_written || 0);
        },

        downloadStatusText(status) {
            const known = ['queued', 'probing', 'running', 'paused', 'cancelling', 'cancelled', 'completed', 'failed'];
            return known.includes(status) ? window.t(`search.download.status_${status}`) : status;
        },

        _formatDownloadTime(seconds) {
            const value = Math.max(0, Math.floor(Number(seconds) || 0));
            const h = Math.floor(value / 3600);
            const m = Math.floor((value % 3600) / 60);
            const s = value % 60;
            return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
        },

        _formatDownloadBytes(bytes) {
            const value = Number(bytes) || 0;
            if (value < 1024) return `${value} B`;
            if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
            if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
            return `${(value / 1024 ** 3).toFixed(2)} GB`;
        },
    };
}
