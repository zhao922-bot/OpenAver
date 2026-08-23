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

const { stateVideos } = await import('../state-videos.js');
const { _setFilteredVideos } = await import('../state-base.js');

function makeState(overrides = {}) {
    return Object.assign(stateVideos(), {
        paginatedVideos: [],
        currentLightboxVideo: null,
        showToast() {},
    }, overrides);
}

test('only untranslated Japanese local titles are eligible', () => {
    const state = makeState();
    assert.equal(state.canTranslateVideo({ path: 'file:///a.mp4', title: '日本語タイトル' }), true);
    assert.equal(state.canTranslateVideo({
        path: 'file:///a.mp4',
        title: '中文标题',
        original_title: '日本語タイトル',
    }), false);
    assert.equal(state.canTranslateVideo({ path: 'actress:name', title: '日本語' }), false);
});

test('translate request uses the local video path and merges the response', async () => {
    const video = { path: 'file:///a.mp4', title: '日本語タイトル' };
    let requestBody;
    let merged;
    globalThis.fetch = async (_url, options) => {
        requestBody = JSON.parse(options.body);
        return {
            ok: true,
            json: async () => ({ success: true, video: { ...video, title: '中文标题' } }),
        };
    };
    const state = makeState({ _mergeTranslatedVideo: value => { merged = value; } });

    await state.translateVideo(video);

    assert.deepEqual(requestBody, { path: video.path, force: false });
    assert.equal(merged.title, '中文标题');
});

test('batch translation opens the in-app confirmation state without browser confirm', () => {
    const state = makeState();
    _setFilteredVideos([
        { path: 'file:///a.mp4', title: '日本語タイトル一' },
        { path: 'file:///b.mp4', title: 'Chinese title' },
    ]);

    state.requestTranslateFilteredVideos();

    assert.equal(state.translationBatchModalOpen, true);
    assert.equal(state.translationBatchCount, 1);
    assert.equal(state._translationBatchItems[0].path, 'file:///a.mp4');
});

test('showcase translation UI uses i18n and no browser confirm', () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../../..');
    const stateSource = readFileSync(
        path.join(repoRoot, 'web/static/js/pages/showcase/state-videos.js'),
        'utf8',
    );
    const template = readFileSync(path.join(repoRoot, 'web/templates/showcase.html'), 'utf8');
    assert.doesNotMatch(stateSource, /window\.confirm|window\.prompt/);
    assert.match(template, /showcase\.translation\.batch_confirm/);
    assert.match(template, /rollbackTranslation\(video\)/);
});
