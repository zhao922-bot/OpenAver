/**
 * state-delete.js — Showcase ESM（71-T7）
 *
 * 影片「從收藏移除」：燈箱垃圾桶 → 破壞性確認 modal → DELETE /api/showcase/video
 * → splice _videos 即時移卡 + 關燈箱 + 成功 toast。
 *
 * 只刪 DB row + 衍生縮圖（後端 delete_by_paths + thumbnail_cache.invalidate），
 * **絕不刪磁碟上的影片檔或原始封面**。鏡像 state-actress.js 的 remove modal 三段路徑。
 *
 * 從 state-base.js import 共用大陣列（F1：移出 Alpine reactive scope），
 * splice _videos 即影響 grid。
 */

import { _videos } from '@/showcase/state-base.js';

export function stateDelete() {
    return {

        // --- 71-T7: Delete Video fluent-modal 狀態（必宣告 stub，Alpine 3 ReferenceError）---
        deleteVideoModalOpen: false,
        _pendingDeleteNumber: null,
        _pendingDeletePath: null,
        _deleteLoading: false,

        // --- 71-T7: Delete Video 三段路徑（鏡像 openRemoveActressModal / cancel / confirm）---
        openDeleteVideoModal() {
            if (!this.currentLightboxVideo?.path) return;
            this._pendingDeletePath = this.currentLightboxVideo.path;
            this._pendingDeleteNumber = this.currentLightboxVideo.number || '';
            this.deleteVideoModalOpen = true;
        },

        cancelDeleteVideo() {
            // 取消 / ESC / backdrop 統一走此：只關 modal + 清 pending，no-op 不發請求
            this.deleteVideoModalOpen = false;
            this._pendingDeletePath = null;
            this._pendingDeleteNumber = null;
        },

        async confirmDeleteVideo() {
            // 請求發起時鎖定 path（避免請求期間切換燈箱導致 splice 誤刪別片）
            const path = this._pendingDeletePath;
            if (!path) {
                this.deleteVideoModalOpen = false;
                return;
            }
            this._deleteLoading = true;
            try {
                const resp = await fetch(
                    '/api/showcase/video?path=' + encodeURIComponent(path),
                    { method: 'DELETE' }
                );
                const data = await resp.json();
                if (resp.ok) {
                    // splice by path（非 index）：用鎖定的 _pendingDeletePath 反查
                    const idx = _videos.findIndex(v => v.path === path);
                    if (idx >= 0) {
                        _videos.splice(idx, 1);
                        // videoCount 是 reactive scalar（state-base），applyFilterAndSort 不重算它
                        // → 手動 decrement（控制 grid vs empty-state 顯示 + 總數）
                        if (this.videoCount > 0) this.videoCount -= 1;
                    }
                    this.applyFilterAndSort();
                    this.closeLightbox();
                    this.showToast(window.t('showcase.video.delete_success'), 'success');
                } else {
                    this.showToast(window.t('showcase.video.delete_failed'), 'error');
                }
            } catch (e) {
                // 失敗：不 splice、不關燈箱（卡仍在，使用者可重試）
                this.showToast(window.t('showcase.video.delete_failed'), 'error');
            } finally {
                this._deleteLoading = false;
                this.deleteVideoModalOpen = false;
                this._pendingDeletePath = null;
                this._pendingDeleteNumber = null;
            }
        },

    };
}
