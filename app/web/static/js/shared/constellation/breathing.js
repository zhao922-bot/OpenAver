/**
 * breathing.js — BreathingManager
 * CD-56B-T2: 8 顆 visible slot 呼吸漂浮（sine.inOut yoyo）+ rail endpoint ticker 跟隨
 *
 * API:
 *   start(visibleSlots)  — 建立 tweens + 啟動 ticker
 *   stop()               — kill 所有 tweens + 移除 ticker
 *   pauseOne(slotId)     — 暫停單顆（hover enter）
 *   resumeOne(slotId)    — 恢復單顆（hover leave）
 *
 * 設計選型（56e-fix: transform-only, CPU 10%→3%）：
 *   - 直接 tween 元素 y/rotation（GPU 合成層，跳過 layout/paint）
 *   - per-card random sign（±1）在 start() 決定，y 與 rotation 共用同一 sign 製造
 *     coherent「tilt + drift」感（同方向漂移 + 同方向傾斜）
 *   - ticker 僅做 rail y2 setAttribute（8 次 SVG geometry，無 layout reflow）
 *   - railEndpoint 在 start() 預算快取，ticker 讀 gsap.getProperty(card, 'y') 後更新 y2
 */

import { railEndpoint } from './anchors.js';

// Photo Picker（shared/burst-picker.js playPickerFloat）全套參數
// 折衷值（±6 / ±1.6° / 2.1-2.7s）user 驗收仍覺「一頓一頓」，改用 picker 全套
const BREATH_AMPLITUDE = 8;    // px，per-card sign 決定 +8 或 -8
const BREATH_ROTATION = 2.5;   // degrees，per-card sign 同 y（coherent tilt + drift）

export class BreathingManager {
  /**
   * @param {Object<string, HTMLElement>} cards       - slotId → DOM element
   * @param {Object<string, SVGLineElement>} railLines - slotId → SVG line element
   * @param {Array<{id: string, x: number, y: number}>} anchors - ANCHORS array
   */
  constructor(cards, railLines, anchors) {
    this._cards = cards;
    this._railLines = railLines;
    this._anchors = anchors;

    /** @type {Object<string, gsap.core.Tween>} */
    this._tweens = {};
    /** @type {Object<string, gsap.core.Tween>} */
    this._rotTweens = {};
    /** @type {Object<string, {x: number, y: number}>} */
    this._endpoints = {};
    /** @type {Function|null} */
    this._tickerFn = null;
  }

  /**
   * start — 為每個 visible slot 建立呼吸 tween + 啟動 ticker
   * defensive：先呼叫 stop() 清除舊狀態
   *
   * @param {Set<string>} visibleSlots - 8 個 visible slot id
   */
  start(visibleSlots) {
    this.stop(); // defensive：清除舊 tweens / ticker

    visibleSlots.forEach(id => {
      const anchor = this._anchors.find(a => a.id === id);
      const card = this._cards[id];
      if (!anchor || !card) return;

      // 預算 rail endpoint（anchor 固定，快取之）
      this._endpoints[id] = railEndpoint(anchor);

      // Per-card random sign（±1）：y 與 rotation 共用，coherent tilt + drift
      const sign = Math.random() > 0.5 ? 1 : -1;

      // 每張卡隨機 duration 製造相位差（y tween 與 rotation tween 共用同一 dur）
      // Picker 全套：1.5-1.9s（user 驗收為「不卡」基準；舊 2.1-2.7 仍覺一頓一頓）
      const dur = 1.5 + Math.random() * 0.4;

      // 確保起始 transform 乾淨（animations.js 已補 y:0，此處二次保險）
      // xPercent/yPercent: -50 接管 CSS translate(-50%,-50%) 置中，防 GSAP 覆蓋（P1 fix）
      gsap.set(card, { xPercent: -50, yPercent: -50, x: 0, y: 0, rotation: 0 });

      // 直接 tween 元素 y/rotation（GPU 合成層，跳過 layout/paint）
      this._tweens[id] = gsap.to(card, {
        y: BREATH_AMPLITUDE * sign,
        duration: dur,
        ease: 'sine.inOut',
        yoyo: true,
        repeat: -1,
      });

      this._rotTweens[id] = gsap.to(card, {
        rotation: BREATH_ROTATION * sign,
        duration: dur,
        ease: 'sine.inOut',
        yoyo: true,
        repeat: -1,
      });
    });

    // ticker callback：僅做 rail y2 跟隨（8 次 setAttribute，無 layout reflow）
    this._tickerFn = () => this._tickUpdate(visibleSlots);
    gsap.ticker.add(this._tickerFn);
  }

  /**
   * _tickUpdate — 每幀執行，僅更新 rail line y2（跟隨卡片 y transform）
   * 不再寫卡片 top/rotation（已由 GSAP tween 直接管理）
   *
   * @param {Set<string>} visibleSlots
   */
  _tickUpdate(visibleSlots) {
    // double-guard：stop() 後 _tickerFn = null，但 ticker 可能在同幀還呼叫一次
    if (!this._tickerFn) return;

    visibleSlots.forEach(id => {
      if (!this._tweens[id]) return; // stop 後 _tweens 清空，skip

      const card = this._cards[id];
      const line = this._railLines[id];
      const ep = this._endpoints[id];

      if (!card || !line || !ep) return;

      // 讀卡片當前 transform y（GSAP 管理），強制轉數字防版本差異
      const currentY = Number(gsap.getProperty(card, 'y')) || 0;
      line.setAttribute('y2', String(ep.y + currentY));
    });
  }

  /**
   * pauseOne — 暫停單顆呼吸（hover enter）
   * tween.pause() 凍結 card 在當前 y/rotation transform 值，card 靜止在漂浮中途位置
   *
   * @param {string} slotId
   */
  pauseOne(slotId) {
    if (this._tweens[slotId]) {
      this._tweens[slotId].pause();
    }
    if (this._rotTweens[slotId]) {
      this._rotTweens[slotId].pause();
    }
  }

  /**
   * resumeOne — 恢復單顆呼吸（hover leave）
   * tween.resume() 從凍結位置繼續，呼吸無縫銜接
   *
   * @param {string} slotId
   */
  resumeOne(slotId) {
    if (this._tweens[slotId]) {
      this._tweens[slotId].resume();
    }
    if (this._rotTweens[slotId]) {
      this._rotTweens[slotId].resume();
    }
  }

  /**
   * stop — kill 所有 tweens + 移除 ticker + 清空狀態 + normalize y/rotation
   * slip-through / onExit 前必須呼叫，確保 card transform 不殘留漂浮偏移
   *
   * clearProps: 'y,rotation' 確保下游 animations.js 的 top-based 定位不受
   * 殘留 transform 影響（y 偏移 + rotation 都歸零）。
   */
  stop() {
    // Kill all tweens
    Object.values(this._tweens).forEach(t => t.kill());
    Object.values(this._rotTweens).forEach(t => t.kill());

    // Remove ticker callback
    if (this._tickerFn) {
      gsap.ticker.remove(this._tickerFn);
      this._tickerFn = null;
    }

    // Normalize y + rotation on previously-active cards（在 clear state 前）
    // P2 fix: 同步重設 rail y2 endpoint 到靜止位置，防最後一幀殘留偏移
    Object.keys(this._tweens).forEach(id => {
      const card = this._cards[id];
      if (card) gsap.set(card, { xPercent: -50, yPercent: -50, x: 0, y: 0, rotation: 0 });
      const line = this._railLines[id];
      const ep = this._endpoints[id];
      if (line && ep) line.setAttribute('y2', String(ep.y));
    });

    // Clear state
    this._tweens = {};
    this._rotTweens = {};
    this._endpoints = {};
  }
}
