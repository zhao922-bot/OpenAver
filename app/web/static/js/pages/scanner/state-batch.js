export function stateBatch() {
    return {
        // ===== T10: Missing NFO/Cover Enrich =====
        missingPillVisible: false,
        missingBothCount: 0,
        missingNfoCount: 0,
        missingCoverCount: 0,
        missingItems: [],           // [{file_path, number}]
        missingEnrichOffset: 0,     // batch offset
        missingEnrichSuccess: 0,
        missingEnrichFailed: 0,
        resumePillVisible: false,
        missingConfirmModalOpen: false,   // TASK-13: 大批量補完 confirm dialog 開關
        _enrichAbortController: null,

        // ===== T10: Missing Pill Computed =====
        get missingPillLabel() {
            const parts = [];
            if (this.missingBothCount > 0) {
                parts.push(window.t('scanner.stats.missing_both_prefix') + ' ' + this.missingBothCount + window.t('scanner.stats.missing_suffix'));
            }
            if (this.missingNfoCount > 0) {
                parts.push(window.t('scanner.stats.missing_nfo_prefix') + ' ' + this.missingNfoCount + window.t('scanner.stats.missing_suffix'));
            }
            if (this.missingCoverCount > 0) {
                parts.push(window.t('scanner.stats.missing_cover_prefix') + ' ' + this.missingCoverCount + window.t('scanner.stats.missing_suffix'));
            }
            return parts.join(' ');
        },

        get missingEnrichButtonText() {
            if (this.state === 'enriching') {
                return '<span class="loading loading-spinner loading-sm"></span> ' + window.t('scanner.stats.missing_enrich_loading');
            }
            return '<i class="bi bi-file-earmark-plus"></i> ' + window.t('scanner.stats.missing_enrich_idle');
        },

        // ===== T10: Missing NFO/Cover Enrich Methods =====
        async checkMissing() {
            try {
                const resp = await fetch('/api/gallery/missing-check');
                const result = await resp.json();
                if (!result.success) return;
                const d = result.data;
                if (d.total_missing > 0) {
                    // TASK-13: 後端永遠回傳完整 items 清單；前端於 runMissingEnrich 內做 > 500 confirm gate
                    this.missingBothCount = d.missing_both || 0;
                    this.missingNfoCount = d.missing_nfo || 0;
                    this.missingCoverCount = d.missing_cover || 0;
                    this.missingItems = Array.isArray(d.items) ? d.items : [];
                    this.missingPillVisible = true;
                } else {
                    this.missingPillVisible = false;
                }
            } catch (e) {
                console.error('checkMissing failed:', e);
            }
        },

        async runMissingEnrich({ skipConfirm = false } = {}) {
            if (this.isGenerating || this.missingItems.length === 0) return;

            // TASK-13: 大批量 confirm gate — > 500 筆彈 modal，等用戶按確認才繼續
            if (!skipConfirm && this.missingItems.length > 500) {
                this.missingConfirmModalOpen = true;
                return;
            }

            this.state = 'enriching';
            this.missingEnrichOffset = 0;
            this.missingEnrichSuccess = 0;
            this.missingEnrichFailed = 0;
            this.progressStatus = window.t('scanner.stats.missing_enrich_loading');
            this.progressCurrent = 0;
            this.progressTotal = this.missingItems.length;
            this.clearLogs();

            const controller = new AbortController();
            this._enrichAbortController = controller;

            const items = this.missingItems.slice();  // snapshot

            try {
                while (this.missingEnrichOffset < items.length) {
                    const batch = items.slice(this.missingEnrichOffset, this.missingEnrichOffset + 20);

                    // Save remaining items to localStorage before each batch
                    localStorage.setItem('avlist_enrich_pending', JSON.stringify(items.slice(this.missingEnrichOffset)));

                    let resp;
                    try {
                        resp = await fetch('/api/batch-enrich', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ items: batch, mode: 'fill_missing' }),
                            signal: controller.signal,
                        });
                    } catch (fetchErr) {
                        if (fetchErr.name === 'AbortError') {
                            // cleanup() already saved remaining items
                            return;
                        }
                        this.addLog('error', '連線失敗: ' + fetchErr.message);
                        this.flushLogs();
                        this.state = 'error';
                        // Save remaining
                        localStorage.setItem('avlist_enrich_pending', JSON.stringify(items.slice(this.missingEnrichOffset)));
                        this.showToast(window.t('scanner.stats.missing_enrich_disconnect'), 'error');
                        return;
                    }

                    if (!resp.ok) {
                        const errText = await resp.text().catch(() => '');
                        this.addLog('error', window.t('scanner.stats.missing_enrich_batch_fail', { status: resp.status, error: errText }));
                        this.flushLogs();
                        this.state = 'error';
                        localStorage.setItem('avlist_enrich_pending', JSON.stringify(items.slice(this.missingEnrichOffset)));
                        this.showToast(window.t('scanner.stats.missing_enrich_error'), 'error');
                        return;
                    }

                    // Read SSE stream
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    let batchDone = false;

                    while (!batchDone) {
                        let readResult;
                        try {
                            readResult = await reader.read();
                        } catch (readErr) {
                            if (readErr.name === 'AbortError') return;
                            break;
                        }
                        const { done, value } = readResult;
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop();  // keep incomplete last line

                        for (const line of lines) {
                            if (!line.startsWith('data: ')) continue;
                            let event;
                            try {
                                event = JSON.parse(line.slice(6));
                            } catch { continue; }

                            if (event.type === 'progress') {
                                this.progressStatus = event.status || window.t('scanner.stats.missing_enrich_loading');
                                this.progressCurrent = this.missingEnrichOffset + (event.current || 0);
                                this.progressTotal = items.length;
                            } else if (event.type === 'result-item') {
                                if (event.success) {
                                    this.missingEnrichSuccess++;
                                } else {
                                    this.missingEnrichFailed++;
                                    this.addLog('warn', `失敗: ${event.number || ''} — ${event.error || ''}`);
                                }
                            } else if (event.type === 'log') {
                                this.addLog(event.level || 'info', event.message || '');
                            } else if (event.type === 'done') {
                                batchDone = true;
                            } else if (event.type === 'error') {
                                this.addLog('error', '錯誤: ' + (event.message || ''));
                                this.flushLogs();
                                this.state = 'error';
                                localStorage.setItem('avlist_enrich_pending', JSON.stringify(items.slice(this.missingEnrichOffset)));
                                this.showToast(window.t('scanner.stats.missing_enrich_stream_error'), 'error');
                                return;
                            }
                        }
                    }

                    // P1 fix: only advance offset if batch actually completed (got 'done' SSE event)
                    if (!batchDone) {
                        this.state = 'error';
                        localStorage.setItem('avlist_enrich_pending', JSON.stringify(items.slice(this.missingEnrichOffset)));
                        this.showToast(window.t('scanner.stats.missing_enrich_disconnect'), 'error');
                        return;
                    }

                    this.missingEnrichOffset += batch.length;
                    this.progressCurrent = this.missingEnrichOffset;
                }

                // All batches complete
                localStorage.removeItem('avlist_enrich_pending');
                this.state = 'done';
                this.progressStatus = window.t('scanner.stats.missing_enrich_done');
                const summary = this.missingEnrichFailed > 0
                    ? window.t('scanner.stats.missing_enrich_toast_mixed', { success: this.missingEnrichSuccess, failed: this.missingEnrichFailed })
                    : window.t('scanner.stats.missing_enrich_toast_success', { success: this.missingEnrichSuccess });
                this.showToast(summary, this.missingEnrichFailed > 0 ? 'warn' : 'success');
                this.flushLogs();
                this.checkMissing();

            } catch (e) {
                if (e.name === 'AbortError') return;
                console.error('runMissingEnrich error:', e);
                this.state = 'error';
                localStorage.setItem('avlist_enrich_pending', JSON.stringify(items.slice(this.missingEnrichOffset)));
                this.showToast(window.t('scanner.stats.missing_enrich_interrupted'), 'error');
            } finally {
                if (this._enrichAbortController === controller) {
                    this._enrichAbortController = null;
                }
            }
        },

        resumeMissingEnrich() {
            // TASK-13: 不在此清 localStorage；交由 runMissingEnrich 成功完成時統一清除。
            // 若補完途中失敗或用戶取消，pending 保留、下次 reload 仍可恢復。
            // skipConfirm:true — 用戶前一次已明確按過「一鍵補完」，resume 視為延續既有意圖。
            this.resumePillVisible = false;
            this.runMissingEnrich({ skipConfirm: true });
        },

        // TASK-13: confirm modal 的確認按鈕 — 跳過 confirm gate、直接啟動補完
        async confirmLargeMissingEnrich() {
            this.missingConfirmModalOpen = false;
            await this.runMissingEnrich({ skipConfirm: true });
        },

        // TASK-13: confirm modal 的取消按鈕 — 只關 modal，不清 localStorage（保留 resume 恢復點）
        cancelLargeMissingEnrich() {
            this.missingConfirmModalOpen = false;
        },

        dismissResume() {
            this.resumePillVisible = false;
            localStorage.removeItem('avlist_enrich_pending');
            // P2 fix: re-fetch DB state to repopulate missingItems (不清空，讓 pill 按鈕可用)
            this.checkMissing();
        },
    };
}
