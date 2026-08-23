/** Rename preview, apply, history, and recovery state for the local library. */

import { _filteredVideos, _recomputeAllBadges } from '@/showcase/state-base.js';

const RENAME_LIMIT = 50;

function responseCode(data, fallback) {
    return data?.detail?.code || data?.error || fallback;
}

export function stateRenames() {
    return {
        renameModalOpen: false,
        renameMode: 'preview',
        renameFolder: true,
        renameLoading: false,
        renameApplying: false,
        renamePreviewItems: [],
        renamePreviewFailures: [],
        renamePreviewTruncated: false,
        renameHistory: [],
        _renameRequestedPaths: [],
        _renameRequestTruncated: false,
        renameRollbackConfirmOpen: false,
        _pendingRenameRollback: null,
        renameRecoverConfirmOpen: false,

        _renameMessage(code, fallback = 'failed') {
            const known = new Set([
                'file_not_found', 'multiple_videos', 'metadata_incomplete', 'path_too_long',
                'target_exists', 'preview_stale', 'not_writable', 'library_sync_failed', 'recovery_required',
                'already_rolled_back', 'operation_busy',
            ]);
            return window.t(`showcase.rename.errors.${known.has(code) ? code : fallback}`);
        },

        async _renameJson(url, options = {}) {
            const response = await fetch(url, options);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(responseCode(data, 'failed'));
            return data;
        },

        async requestRenameFilteredVideos(video = null) {
            if (this.renameLoading || this.renameApplying) return;
            const allPaths = video?.path
                ? [video.path]
                : _filteredVideos.map(item => item?.path).filter(Boolean);
            const paths = allPaths.slice(0, RENAME_LIMIT);
            if (!paths.length) {
                this.showToast(window.t('showcase.rename.none'), 'info');
                return;
            }
            this._renameRequestedPaths = paths;
            this._renameRequestTruncated = allPaths.length > RENAME_LIMIT;
            this.renameMode = 'preview';
            this.renameModalOpen = true;
            await this.refreshRenamePreview();
        },

        async refreshRenamePreview() {
            if (this.renameLoading || !this._renameRequestedPaths.length) return;
            this.renameLoading = true;
            this.renamePreviewItems = [];
            this.renamePreviewFailures = [];
            try {
                const data = await this._renameJson('/api/renames/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        paths: this._renameRequestedPaths,
                        rename_folder: Boolean(this.renameFolder),
                    }),
                });
                const results = data.results || [];
                this.renamePreviewItems = results.filter(item => item.success && item.renamed);
                this.renamePreviewFailures = results.filter(item => !item.success);
                this.renamePreviewTruncated = this._renameRequestTruncated || Boolean(data.truncated);
                if (!this.renamePreviewItems.length && !this.renamePreviewFailures.length) {
                    this.showToast(window.t('showcase.rename.none'), 'info');
                }
            } catch (error) {
                this.showToast(this._renameMessage(error.message), 'error');
                this.renameModalOpen = false;
            } finally {
                this.renameLoading = false;
            }
        },

        closeRenameModal() {
            if (this.renameApplying) return;
            this.renameModalOpen = false;
            this.renamePreviewItems = [];
            this.renamePreviewFailures = [];
            this._renameRequestedPaths = [];
            this._renameRequestTruncated = false;
        },

        async confirmRenamePreview() {
            if (this.renameApplying || !this.renamePreviewItems.length) return;
            this.renameApplying = true;
            try {
                const paths = this.renamePreviewItems.map(item => item.path);
                const expectedNewPaths = Object.fromEntries(
                    this.renamePreviewItems.map(item => [item.path, item.new_path]),
                );
                const data = await this._renameJson('/api/renames/apply', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        paths,
                        rename_folder: Boolean(this.renameFolder),
                        expected_new_paths: expectedNewPaths,
                    }),
                });
                const warningCount = (data.results || []).filter(item => item.warning).length;
                this.showToast(
                    window.t(data.failed ? 'showcase.rename.partial' : 'showcase.rename.success', {
                        renamed: data.renamed || 0,
                        failed: data.failed || 0,
                    }),
                    data.failed || warningCount ? 'error' : 'success',
                );
                this.renameModalOpen = false;
                await this.fetchVideos();
                _recomputeAllBadges();
                this.applyFilterAndSort();
            } catch (error) {
                this.showToast(this._renameMessage(error.message), 'error');
            } finally {
                this.renameApplying = false;
            }
        },

        async openRenameHistory() {
            this.renameMode = 'history';
            this.renameModalOpen = true;
            this.renameLoading = true;
            try {
                const data = await this._renameJson('/api/renames/history?limit=50');
                this.renameHistory = data.items || [];
            } catch (error) {
                this.showToast(this._renameMessage(error.message), 'error');
            } finally {
                this.renameLoading = false;
            }
        },

        requestRenameRollback(event) {
            if (!event?.id || event.status === 'rolled_back') return;
            this._pendingRenameRollback = event;
            this.renameModalOpen = false;
            this.renameRollbackConfirmOpen = true;
        },

        cancelRenameRollback() {
            if (this.renameApplying) return;
            this.renameRollbackConfirmOpen = false;
            this._pendingRenameRollback = null;
            this.renameModalOpen = true;
            this.renameMode = 'history';
        },

        async confirmRenameRollback() {
            const event = this._pendingRenameRollback;
            if (!event?.id || this.renameApplying) return;
            this.renameApplying = true;
            try {
                await this._renameJson('/api/renames/rollback', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ event_id: event.id }),
                });
                this.renameRollbackConfirmOpen = false;
                this._pendingRenameRollback = null;
                this.showToast(window.t('showcase.rename.rollback_success'), 'success');
                await this.openRenameHistory();
                await this.fetchVideos();
                _recomputeAllBadges();
                this.applyFilterAndSort();
            } catch (error) {
                this.showToast(this._renameMessage(error.message), 'error');
            } finally {
                this.renameApplying = false;
            }
        },

        requestRecoverPendingRenames() {
            if (!this.renameHistory.some(item => ['pending', 'recovery_required'].includes(item.status))) return;
            this.renameModalOpen = false;
            this.renameRecoverConfirmOpen = true;
        },

        cancelRecoverPendingRenames() {
            if (this.renameApplying) return;
            this.renameRecoverConfirmOpen = false;
            this.renameModalOpen = true;
            this.renameMode = 'history';
        },

        async confirmRecoverPendingRenames() {
            if (this.renameApplying) return;
            this.renameApplying = true;
            try {
                const data = await this._renameJson('/api/renames/recover-pending', { method: 'POST' });
                this.renameRecoverConfirmOpen = false;
                this.showToast(
                    window.t('showcase.rename.recovery_result', {
                        recovered: data.recovered || 0,
                        failed: data.failed || 0,
                    }),
                    data.failed ? 'error' : 'success',
                );
                await this.openRenameHistory();
                await this.fetchVideos();
                _recomputeAllBadges();
                this.applyFilterAndSort();
            } catch (error) {
                this.showToast(this._renameMessage(error.message), 'error');
            } finally {
                this.renameApplying = false;
            }
        },
    };
}
