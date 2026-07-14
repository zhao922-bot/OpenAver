/** Search page authorized-download state and interactions. */
export function searchStateDownloads() {
    return {
        downloadModalOpen: false,
        downloadStarting: false,
        downloadSettingsSaving: false,
        downloadTasks: [],
        downloadDestinations: [],
        downloadSettings: { maxConcurrentDownloads: 4, fragmentThreads: 16, engineAvailable: true },
        _downloadPollTimer: null,
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
            await Promise.all([this.loadDownloadDestinations(), this.loadDownloadTasks(), this.loadDownloadSettings()]);
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

        async loadDownloadSettings() {
            try {
                const data = await this._downloadJson('/api/downloads/settings');
                const settings = data.settings || {};
                this.downloadSettings = {
                    maxConcurrentDownloads: settings.max_concurrent_downloads || 4,
                    fragmentThreads: settings.fragment_threads || 16,
                    engineAvailable: settings.engine_available !== false,
                };
            } catch (error) {
                this.showToast(error.message, 'error');
            }
        },

        async saveDownloadSettings() {
            if (this.downloadSettingsSaving) return;
            this.downloadSettingsSaving = true;
            try {
                const data = await this._downloadJson('/api/downloads/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        max_concurrent_downloads: Number(this.downloadSettings.maxConcurrentDownloads),
                        fragment_threads: Number(this.downloadSettings.fragmentThreads),
                    }),
                });
                const settings = data.settings || {};
                this.downloadSettings.maxConcurrentDownloads = settings.max_concurrent_downloads;
                this.downloadSettings.fragmentThreads = settings.fragment_threads;
                this.showToast(window.t('search.download.settings_saved'), 'success');
            } catch (error) {
                await this.loadDownloadSettings();
                this.showToast(error.message, 'error');
            } finally {
                this.downloadSettingsSaving = false;
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

        downloadTaskMessage(task) {
            const keys = {
                http_403: 'error_http_403',
                engine_missing: 'error_engine_missing',
                target_exists: 'error_target_exists',
                download_failed: 'error_download_failed',
            };
            return keys[task.error_code]
                ? window.t(`search.download.${keys[task.error_code]}`)
                : (task.message || '');
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
