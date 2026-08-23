import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { searchStateDownloads } from '../state/downloads.js';

globalThis.window = globalThis;
globalThis.window.t = key => key;

function makeState(current) {
  const state = Object.assign(searchStateDownloads(), {
    current: () => current,
    showToast() {},
  });
  state.loadDownloadDestinations = async () => true;
  state.loadDownloadTasks = async () => true;
  state.loadDownloadSettings = async () => true;
  state._startDownloadPolling = () => {};
  return state;
}

test('download form contains no Chinese title field', () => {
  assert.equal(Object.hasOwn(searchStateDownloads().downloadForm, 'chineseTitle'), false);
});

test('open modal keeps the Japanese title even when an AI translation exists', async () => {
  const state = makeState({
    number: 'ABC-123',
    title: 'Japanese title',
    translated_title: 'AI Chinese title',
    url: 'https://source.example/item',
  });

  await state.openDownloadModal();

  assert.equal(state.downloadForm.title, 'Japanese title');
  assert.equal(Object.hasOwn(state.downloadForm, 'chineseTitle'), false);
});

test('download request sends one title field only', async () => {
  const current = { number: 'ABC-123', title: 'Japanese title' };
  const state = makeState(current);
  state.downloadSettings.engineAvailable = true;
  state.downloadForm = {
    number: current.number,
    title: current.title,
    mediaUrl: 'https://cdn.example/video.m3u8',
    destination: 'D:/Videos/JAV',
    rightsConfirmed: true,
    sourcePageUrl: '',
  };
  let requestBody;
  state._downloadJson = async (_url, options) => {
    requestBody = JSON.parse(options.body);
    return { success: true };
  };

  await state.startAuthorizedDownload();

  assert.equal(requestBody.title, 'Japanese title');
  assert.equal(Object.hasOwn(requestBody, 'chinese_title'), false);
});

test('download layout is two columns on desktop and one on narrow screens', async () => {
  const cssUrl = new URL('../../../../css/pages/search.css', import.meta.url);
  const templateUrl = new URL('../../../../../templates/search.html', import.meta.url);
  const [css, template] = await Promise.all([
    readFile(cssUrl, 'utf8'),
    readFile(templateUrl, 'utf8'),
  ]);

  assert.match(css, /\.download-fields\s*\{[^}]*grid-template-columns:\s*repeat\(2,/s);
  assert.match(css, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.download-fields\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s);
  const downloadMarkup = template.slice(
    template.indexOf(':class="{ \'modal-open\': downloadModalOpen }"'),
    template.indexOf('<!-- Fix-1: 覆蓋警告 Modal'),
  );
  assert.doesNotMatch(downloadMarkup, /chineseTitle|chinese_title/);
});
