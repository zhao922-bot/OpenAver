/**
 * Whether to clear the NFO update selection after a completed run.
 * Keep paths (and the retry control) whenever any hard failure remains so
 * the user can retry without re-scanning. Complete items preflight-skip.
 *
 * @param {number} failed Count of hard failures from the done payload.
 * @returns {boolean}
 */
export function shouldClearNfoSelectionAfterUpdate(failed) {
    return Number(failed || 0) === 0;
}
