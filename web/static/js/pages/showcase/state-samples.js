/** Missing-stills scan and bounded batch queue for the local library. */

import { _filteredVideos, _recomputeAllBadges } from '@/showcase/state-base.js';

const SAMPLE_SCAN_LIMIT = 500;
const SAMPLE_WORKERS = 2;

export function stateSamples() {
    return {
        samplesBatchModalOpen: false,
        samplesBatchStage: 'confirm',
        samplesBatchScanning: false,
        samplesBatchRunning: false,
        samplesBatchItems: [],
        samplesBatchFailedItems: [],
        samplesBatchProcessed: 0,
        samplesBatchSucceeded: 0,
        samplesBatchUnavailable: 0,
        samplesBatchFailed: 0,

        get samplesBatchProgress() {
            if (!this.samplesBatchItems.length) return 0;
            return Math.round((this.samplesBatchProcessed / this.samplesBatchItems.length) * 100);
        },

        async requestFillMissingSamples() {
            if (this.samplesBatchScanning || this.samplesBatchRunning) return;
            const paths = _filteredVideos
                .map(video => video?.path)
                .filter(Boolean)
                .slice(0, SAMPLE_SCAN_LIMIT);
            if (!paths.length) {
                this.showToast(window.t('showcase.samples.batch_none'), 'info');
                return;
            }
            this.samplesBatchScanning = true;
            try {
                const response = await fetch('/api/sample-batches/missing', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) throw new Error('scan_failed');
                this.samplesBatchItems = data.items || [];
                this.samplesBatchFailedItems = [];
                this._resetSamplesBatchCounts();
                if (!this.samplesBatchItems.length) {
                    this.showToast(window.t('showcase.samples.batch_none'), 'info');
                    return;
                }
                this.samplesBatchStage = 'confirm';
                this.samplesBatchModalOpen = true;
            } catch (_) {
                this.showToast(window.t('showcase.samples.batch_scan_failed'), 'error');
            } finally {
                this.samplesBatchScanning = false;
            }
        },

        _resetSamplesBatchCounts() {
            this.samplesBatchProcessed = 0;
            this.samplesBatchSucceeded = 0;
            this.samplesBatchUnavailable = 0;
            this.samplesBatchFailed = 0;
        },

        closeSamplesBatchModal() {
            if (this.samplesBatchRunning) return;
            this.samplesBatchModalOpen = false;
        },

        async _fetchSamplesBatchItem(item) {
            try {
                const response = await fetch('/api/scraper/fetch-samples', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_path: item.path, number: item.number }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) return 'failed';
                return Number(data.extrafanart_written || 0) > 0 ? 'succeeded' : 'unavailable';
            } catch (_) {
                return 'failed';
            }
        },

        async _runSamplesBatch(items) {
            let cursor = 0;
            const failedItems = [];
            const worker = async () => {
                while (cursor < items.length) {
                    const item = items[cursor];
                    cursor += 1;
                    const status = await this._fetchSamplesBatchItem(item);
                    if (status === 'succeeded') this.samplesBatchSucceeded += 1;
                    else if (status === 'unavailable') this.samplesBatchUnavailable += 1;
                    else {
                        this.samplesBatchFailed += 1;
                        failedItems.push(item);
                    }
                    this.samplesBatchProcessed += 1;
                }
            };
            await Promise.all(
                Array.from({ length: Math.min(SAMPLE_WORKERS, items.length) }, () => worker()),
            );
            this.samplesBatchFailedItems = failedItems;
        },

        async confirmFillMissingSamples(retry = false) {
            if (this.samplesBatchRunning) return;
            const items = retry ? this.samplesBatchFailedItems.slice() : this.samplesBatchItems.slice();
            if (!items.length) return;
            this.samplesBatchItems = items;
            this._resetSamplesBatchCounts();
            this.samplesBatchStage = 'running';
            this.samplesBatchRunning = true;
            try {
                await this._runSamplesBatch(items);
                this.samplesBatchStage = 'result';
                await this.fetchVideos();
                _recomputeAllBadges();
                this.applyFilterAndSort();
                this.showToast(
                    window.t('showcase.samples.batch_result', {
                        succeeded: this.samplesBatchSucceeded,
                        unavailable: this.samplesBatchUnavailable,
                        failed: this.samplesBatchFailed,
                    }),
                    this.samplesBatchFailed ? 'error' : 'success',
                );
            } finally {
                this.samplesBatchRunning = false;
            }
        },
    };
}
