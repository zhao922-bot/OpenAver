import { readFileSync } from 'node:fs';
import path from 'node:path';
import { register } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';

globalThis.window = globalThis;
globalThis.window.t = (key, values = {}) => Object.entries(values)
    .reduce((text, [name, value]) => text.replace(`{${name}}`, value), key);

const STATIC_JS_ROOT = pathToFileURL(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../') + '/',
).href;
const loaderCode = `
const map = {
  '@/settings/': 'pages/settings/', '@/shared/': 'shared/', '@/components/': 'components/',
  '@/search/': 'pages/search/', '@/showcase/': 'pages/showcase/', '@/scanner/': 'pages/scanner/'
};
const root = ${JSON.stringify(STATIC_JS_ROOT)};
export async function resolve(specifier, context, nextResolve) {
  for (const [prefix, rel] of Object.entries(map)) {
    if (specifier.startsWith(prefix)) return nextResolve(root + rel + specifier.slice(prefix.length), context);
  }
  return nextResolve(specifier, context);
}`;
register(`data:text/javascript,${encodeURIComponent(loaderCode)}`, import.meta.url);

const { stateRenames } = await import('../state-renames.js');
const { _setFilteredVideos } = await import('../state-base.js');

function makeState(overrides = {}) {
    return Object.assign(stateRenames(), {
        showToast() {},
        async fetchVideos() {},
        applyFilterAndSort() {},
    }, overrides);
}

test('rename preview submits only filtered local paths', async () => {
    _setFilteredVideos([{ path: 'file:///a.mp4' }, { path: 'file:///b.mp4' }]);
    let requestBody;
    globalThis.fetch = async (_url, options) => {
        requestBody = JSON.parse(options.body);
        return {
            ok: true,
            json: async () => ({
                success: true,
                results: [{ path: 'file:///a.mp4', success: true, renamed: true }],
            }),
        };
    };
    const state = makeState();

    await state.requestRenameFilteredVideos();

    assert.deepEqual(requestBody, {
        paths: ['file:///a.mp4', 'file:///b.mp4'],
        rename_folder: true,
    });
    assert.equal(state.renameModalOpen, true);
    assert.equal(state.renamePreviewItems.length, 1);
});

test('applying preview refreshes the local library', async () => {
    let refreshCount = 0;
    let filterCount = 0;
    let applyBody;
    globalThis.fetch = async (url, options = {}) => ({
        ok: true,
        json: async () => {
            if (url.endsWith('/apply')) {
                applyBody = JSON.parse(options.body);
                return { success: true, renamed: 1, failed: 0, results: [] };
            }
            return { success: true };
        },
    });
    const state = makeState({
        renameModalOpen: true,
        renamePreviewItems: [{ path: 'file:///a.mp4', new_path: 'A:/renamed/a.mp4' }],
        async fetchVideos() { refreshCount += 1; },
        applyFilterAndSort() { filterCount += 1; },
    });

    await state.confirmRenamePreview();

    assert.equal(state.renameModalOpen, false);
    assert.equal(refreshCount, 1);
    assert.equal(filterCount, 1);
    assert.deepEqual(applyBody.expected_new_paths, {
        'file:///a.mp4': 'A:/renamed/a.mp4',
    });
});

test('rename preview reports when the filtered library exceeds the batch limit', async () => {
    _setFilteredVideos(Array.from({ length: 51 }, (_, index) => ({
        path: `file:///video-${index}.mp4`,
    })));
    let requestBody;
    globalThis.fetch = async (_url, options) => {
        requestBody = JSON.parse(options.body);
        return {
            ok: true,
            json: async () => ({ success: true, truncated: false, results: [] }),
        };
    };
    const state = makeState();

    await state.requestRenameFilteredVideos();

    assert.equal(requestBody.paths.length, 50);
    assert.equal(state.renamePreviewTruncated, true);
});

test('rollback confirmation does not stack over the history modal', () => {
    const state = makeState({ renameModalOpen: true, renameMode: 'history' });
    state.requestRenameRollback({ id: 'event-1', status: 'completed' });

    assert.equal(state.renameModalOpen, false);
    assert.equal(state.renameRollbackConfirmOpen, true);

    state.cancelRenameRollback();
    assert.equal(state.renameModalOpen, true);
    assert.equal(state.renameMode, 'history');
});

test('rename UI uses in-app dialogs and stable endpoints', () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../../..');
    const source = readFileSync(
        path.join(repoRoot, 'web/static/js/pages/showcase/state-renames.js'),
        'utf8',
    );
    const template = readFileSync(path.join(repoRoot, 'web/templates/showcase.html'), 'utf8');

    assert.doesNotMatch(source, /window\.(confirm|prompt)/);
    assert.match(source, /\/api\/renames\/preview/);
    assert.match(source, /\/api\/renames\/rollback/);
    assert.match(template, /renameRollbackConfirmOpen/);
});
