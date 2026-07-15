/**
 * Pure helpers for batch sample-image (剧照) fill on Scanner.
 * Keep Alpine/DOM free so Node can unit-test these.
 */

/**
 * Whether the missing-samples stats row should be visible.
 * Visible when there are candidates OR multi-folder exclusions to show.
 * @param {{count?:number, skippedMulti?:number}} opts
 * @returns {boolean}
 */
export function shouldShowMissingSamplesRow(opts) {
    const o = opts && typeof opts === 'object' ? opts : {};
    return Number(o.count || 0) > 0 || Number(o.skippedMulti || 0) > 0;
}

/**
 * Build i18n params for the missing-samples label.
 * @param {{count?:number, skippedMulti?:number}} opts
 * @returns {{count:number, skipped_multi:number}}
 */
export function missingSamplesLabelParams(opts) {
    const o = opts && typeof opts === 'object' ? opts : {};
    return {
        count: Number(o.count || 0),
        skipped_multi: Number(o.skippedMulti || 0),
    };
}

/**
 * Normalize a batch-fetch-samples done.summary into display counters.
 * @param {object} summary
 * @param {number} [checkPhaseSkippedMulti=0] multi-folder skips from the check
 *   phase (candidates already exclude these). Added to batch-phase
 *   skipped_multi without double-counting the same pool.
 * @returns {{success:number, images:number, noSamples:number, skipped:number, skippedMulti:number, failed:number}}
 */
export function summarizeBatchSamplesDone(summary, checkPhaseSkippedMulti) {
    const s = summary && typeof summary === 'object' ? summary : {};
    const skippedComplete = Number(s.skipped_complete || 0);
    const skippedMultiBatch = Number(s.skipped_multi || 0);
    const skippedMultiCheck = Number(checkPhaseSkippedMulti || 0);
    const skippedField = Number(s.skipped || 0);

    // "skipped" = already-complete only when we surface multi separately,
    // so check-phase multi is not mixed into the same counter as batch multi.
    let skipped = skippedComplete;
    if (skippedComplete === 0 && skippedField > 0) {
        // Older payloads: strip batch multi from combined skipped if present.
        skipped = Math.max(0, skippedField - skippedMultiBatch);
    }

    return {
        success: Number(s.success || 0),
        images: Number(s.images_downloaded || 0),
        noSamples: Number(s.no_samples || 0),
        skipped,
        // check-phase + batch-phase (TOCTOU / dir changed during run); not double-counted.
        skippedMulti: skippedMultiCheck + skippedMultiBatch,
        failed: Number(s.failed || 0),
    };
}

/**
 * Map one SSE item status to a log level + short key suffix for i18n.
 * @param {string} status
 * @returns {'info'|'warn'|'error'}
 */
export function sampleItemLogLevel(status) {
    if (status === 'success' || status === 'skipped_complete') return 'info';
    if (status === 'no_samples' || status === 'skipped_multi') return 'warn';
    return 'error';
}

/**
 * Whether a batch-fetch-samples HTTP response means "busy / re-entrant".
 * @param {number} status
 * @returns {boolean}
 */
export function isBatchSamplesBusyStatus(status) {
    return Number(status) === 409;
}
