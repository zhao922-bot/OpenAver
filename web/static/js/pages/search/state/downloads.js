/** Authorized direct-media download state for the search page. */
export function searchStateDownloads() {
    return {
        downloadModalOpen: false,
        downloadStarting: false,
        downloadSettingsSaving: false,
        downloadRetryUrlModalOpen: false,
        downloadRetryTask: null,
        downloadRetryUrl: '',
        downloadClearModalOpen: false,
        downloadClearOnlyFailed: false,
        downloadTasks: [],
        downloadDestinations: [],
        downloadSettings: { maxConcurrentDownloads: 4, fragmentThreads: 16, engineAvailable: true },
        downloadForm: {
            number: '', title: '', mediaUrl: '', destination: '', rightsConfirmed: false, sourcePageUrl: '',
        },
        _downloadPollTimer: null,
        _downloadPollFailures: 0,

        async _downloadJson(url, options = {}) {
            const response = await fetch(url, options);
            let data = {};
            try {
                data = await response.json();
            } catch (_) {
                data = {};
            }
            if (!response.ok) {
                const code = data?.detail?.code || data?.code || 'request_failed';
                const error = new Error(window.t(`search.download.error_${code}`));
                error.code = code;
                error.status = response.status;
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
                mediaUrl: '',
                destination: '',
                rightsConfirmed: false,
                sourcePageUrl: current.url || '',
            };
            this.downloadModalOpen = true;
            await Promise.all([
                this.loadDownloadDestinations(),
                this.loadDownloadTasks(),
                this.loadDownloadSettings(),
            ]);
            if (!this.downloadForm.destination && this.downloadDestinations.length) {
                this.downloadForm.destination = this.downloadDestinations[0];
            }
            this._startDownloadPolling();
        },

        closeDownloadModal() {
            this.downloadModalOpen = false;
            this.closeDownloadRetryUrlModal();
            this.downloadClearModalOpen = false;
            this.cleanupDownloadState();
        },

        cleanupDownloadState() {
            if (this._downloadPollTimer) {
                clearInterval(this._downloadPollTimer);
                this._downloadPollTimer = null;
            }
            this._downloadPollFailures = 0;
        },

        _startDownloadPolling() {
            this.cleanupDownloadState();
            this._downloadPollTimer = setInterval(async () => {
                if (!this.downloadModalOpen) return;
                const ok = await this.loadDownloadTasks({ quiet: true });
                if (ok) {
                    this._downloadPollFailures = 0;
                    return;
                }
                this._downloadPollFailures += 1;
                if (this._downloadPollFailures >= 3) {
                    this.cleanupDownloadState();
                    this.showToast(window.t('search.download.poll_stopped'), 'warning');
                }
            }, 1000);
        },

        async loadDownloadDestinations() {
            try {
                const data = await this._downloadJson('/api/downloads/destinations');
                this.downloadDestinations = data.items || [];
                return true;
            } catch (error) {
                this.downloadDestinations = [];
                this.showToast(error.message, 'error');
                return false;
            }
        },

        async loadDownloadTasks({ quiet = false } = {}) {
            try {
                const data = await this._downloadJson('/api/downloads');
                this.downloadTasks = data.items || [];
                return true;
            } catch (error) {
                if (!quiet) this.showToast(error.message, 'error');
                return false;
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
                return true;
            } catch (error) {
                this.showToast(error.message, 'error');
                return false;
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
            if (this.downloadStarting || !this.downloadCanStart()) return;
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
                await this.loadDownloadTasks({ quiet: true });
                this.showToast(window.t('search.download.queued'), 'success');
            } catch (error) {
                this.showToast(error.message, 'error');
            } finally {
                this.downloadStarting = false;
            }
        },

        async controlDownload(task, action) {
            try {
                await this._downloadJson(`/api/downloads/${task.id}/control/${action}`, { method: 'POST' });
                await this.loadDownloadTasks({ quiet: true });
            } catch (error) {
                this.showToast(error.message, 'error');
            }
        },

        async removeDownload(task) {
            try {
                await this._downloadJson(`/api/downloads/${task.id}`, { method: 'DELETE' });
                await this.loadDownloadTasks({ quiet: true });
            } catch (error) {
                this.showToast(error.message, 'error');
            }
        },

        downloadCanStart() {
            return Boolean(
                this.downloadSettings.engineAvailable &&
                this.downloadForm.mediaUrl.trim() &&
                this.downloadForm.destination &&
                this.downloadForm.rightsConfirmed
            );
        },

        downloadStatusText(status) {
            const known = new Set([
                'queued', 'probing', 'running', 'paused', 'cancelling', 'cancelled', 'completed', 'failed',
            ]);
            return known.has(status) ? window.t(`search.download.status_${status}`) : status;
        },

        downloadTaskMessage(task) {
            if (task.error_code) return window.t(`search.download.error_${task.error_code}`);
            if (task.message === 'completed_with_warnings') {
                return window.t('search.download.completed_with_warnings');
            }
            return '';
        },

        downloadProgressText(task) {
            const parts = [];
            if (task.duration_seconds) {
                parts.push(`${this._formatDownloadTime(task.elapsed_seconds)} / ${this._formatDownloadTime(task.duration_seconds)}`);
            } else {
                parts.push(this._formatDownloadBytes(task.bytes_written || 0));
            }
            if (task.speed) parts.push(task.speed);
            if (task.eta_seconds > 0 && task.status === 'running') {
                parts.push(window.t('search.download.remaining', { time: this._formatDownloadTime(task.eta_seconds) }));
            }
            return parts.filter(Boolean).join(' · ');
        },

        openDownloadRetryUrlModal(task) {
            this.downloadRetryTask = task;
            this.downloadRetryUrl = '';
            this.downloadRetryUrlModalOpen = true;
        },

        closeDownloadRetryUrlModal() {
            this.downloadRetryUrlModalOpen = false;
            this.downloadRetryTask = null;
            this.downloadRetryUrl = '';
        },

        async confirmDownloadRetryUrl() {
            const task = this.downloadRetryTask;
            const mediaUrl = this.downloadRetryUrl.trim();
            if (!task || !mediaUrl) return;
            try {
                await this._downloadJson(`/api/downloads/${task.id}/retry-with-url`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ media_url: mediaUrl }),
                });
                this.closeDownloadRetryUrlModal();
                await this.loadDownloadTasks({ quiet: true });
                this.showToast(window.t('search.download.queued'), 'success');
            } catch (error) {
                this.showToast(error.message, 'error');
            }
        },

        requestClearDownloadHistory(onlyFailed = false) {
            this.downloadClearOnlyFailed = onlyFailed;
            this.downloadClearModalOpen = true;
        },

        async confirmClearDownloadHistory() {
            const onlyFailed = this.downloadClearOnlyFailed;
            try {
                const data = await this._downloadJson(
                    `/api/downloads/history?only_failed=${onlyFailed ? 'true' : 'false'}`,
                    { method: 'DELETE' },
                );
                await this.loadDownloadTasks({ quiet: true });
                this.downloadClearModalOpen = false;
                this.showToast(
                    window.t('search.download.history_cleared', { count: data.removed || 0 }),
                    'success',
                );
            } catch (error) {
                this.showToast(error.message, 'error');
            }
        },

        _formatDownloadTime(seconds) {
            const value = Math.max(0, Math.floor(Number(seconds) || 0));
            const hours = Math.floor(value / 3600);
            const minutes = Math.floor((value % 3600) / 60);
            const secs = value % 60;
            return hours
                ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
                : `${minutes}:${String(secs).padStart(2, '0')}`;
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
