import { readFileSync } from 'node:fs';
import path from 'node:path';
import { register } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';

globalThis.window = globalThis;
globalThis.window.t = (key) => key;

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

const { stateSamples } = await import('../state-samples.js');
const { _setFilteredVideos } = await import('../state-base.js');

function makeState(overrides = {}) {
    return Object.assign(stateSamples(), {
        showToast() {},
        async fetchVideos() {},
        applyFilterAndSort() {},
    }, overrides);
}

test('missing scan opens an in-app confirmation with backend candidates', async () => {
    _setFilteredVideos([{ path: 'file:///a.mp4' }, { path: 'file:///b.mp4' }]);
    let body;
    globalThis.fetch = async (_url, options) => {
        body = JSON.parse(options.body);
        return {
            ok: true,
            json: async () => ({ success: true, items: [{ path: 'file:///a.mp4', number: 'A-1' }] }),
        };
    };
    const state = makeState();

    await state.requestFillMissingSamples();

    assert.deepEqual(body.paths, ['file:///a.mp4', 'file:///b.mp4']);
    assert.equal(state.samplesBatchModalOpen, true);
    assert.equal(state.samplesBatchItems.length, 1);
});

test('batch queue uses at most two workers and separates outcomes', async () => {
    let active = 0;
    let maximum = 0;
    const statuses = ['succeeded', 'unavailable', 'failed', 'succeeded'];
    const state = makeState({
        async _fetchSamplesBatchItem(item) {
            active += 1;
            maximum = Math.max(maximum, active);
            await new Promise(resolve => setTimeout(resolve, 5));
            active -= 1;
            return statuses[item.index];
        },
    });
    const items = statuses.map((_status, index) => ({ index }));

    await state._runSamplesBatch(items);

    assert.equal(maximum, 2);
    assert.equal(state.samplesBatchSucceeded, 2);
    assert.equal(state.samplesBatchUnavailable, 1);
    assert.equal(state.samplesBatchFailed, 1);
    assert.deepEqual(state.samplesBatchFailedItems, [{ index: 2 }]);
});

test('single batch request classifies zero downloaded images as unavailable', async () => {
    globalThis.fetch = async () => ({
        ok: true,
        json: async () => ({ success: true, extrafanart_written: 0 }),
    });
    const state = makeState();

    assert.equal(
        await state._fetchSamplesBatchItem({ path: 'file:///a.mp4', number: 'A-1' }),
        'unavailable',
    );
});

test('batch stills UI avoids browser confirmation APIs', () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../../..');
    const source = readFileSync(
        path.join(repoRoot, 'web/static/js/pages/showcase/state-samples.js'),
        'utf8',
    );
    const template = readFileSync(path.join(repoRoot, 'web/templates/showcase.html'), 'utf8');

    assert.doesNotMatch(source, /window\.(confirm|prompt)/);
    assert.match(source, /\/api\/sample-batches\/missing/);
    assert.match(template, /samplesBatchModalOpen/);
});
