/**
 * rails.js — Rail 三態邏輯 + SVG line 操作 helpers
 * CD-56B-3: railRole + setRailCoords + railDrawIn / railDrawOut
 *
 * T4fix（CD-56B-3 / CD-T2FIX-3 contract amendment，2026-05-07）：
 *   - sweep 從 DrawSVG draw-in → stroke-dashoffset 流動（single packet）
 *   - focus / clicked / shimmer 從 strokeWidth pulse → stroke-opacity tween
 *   - 所有 inline strokeOpacity tween 收尾必 clearProps，由 CSS class 或 baseline 接管
 *   - 強度數值定錨於 TASK-56b-T4fix.md §C state model hierarchy 表
 *
 * ESM export — 不走 window 全域
 */

import { ANCHORS, railEndpoint } from './anchors.js';

/**
 * railRole — 判斷 rail 三態（純函數，無副作用）
 * CD-56B-3 契約
 */
export function railRole(slotId, prevVisible, nextVisible) {
  const inPrev = prevVisible.has(slotId);
  const inNext = nextVisible.has(slotId);

  if (inPrev && inNext)  return 'persist';
  if (!inPrev && inNext) return 'enter';
  if (inPrev && !inNext) return 'exit';
  return 'absent';
}

/**
 * setRailCoords — 設定 SVG line 的 x1/y1（中心）和 x2/y2（endpoint）
 */
export function setRailCoords(line, anchor) {
  if (!line || !anchor) return;
  const ep = railEndpoint(anchor);
  line.setAttribute('x1', 480);
  line.setAttribute('y1', 310);
  line.setAttribute('x2', ep.x);
  line.setAttribute('y2', ep.y);
}

/**
 * railDrawIn — DrawSVG 0% → 100% enter 動畫
 *
 * T4fix §F option 1：DrawSVG 期間會覆寫 CSS dasharray baseline（點珠模式），
 * enter 完成後必須 clearProps 讓 CSS 接管。strokeOpacity 一併納入（§I 契約）。
 */
export function railDrawIn(line, tl, pos) {
  if (!line || !tl) return;

  if (typeof DrawSVGPlugin !== 'undefined') {
    tl.call(() => {
      line.classList.remove('rail--hidden');
      // T5 (CD-T5-2)：CSS baseline 已改為 stroke-opacity:0；DrawSVG 進場必須注入
      // strokeOpacity:0.10，否則在透明線上畫畫＝完全看不見。clearProps 在 pos+0.55
      // 會清掉，rail 自動落回 baseline 0（spec §3.2「動畫結束消失」）。
      gsap.set(line, { drawSVG: '0%', opacity: 1, strokeOpacity: 0.10 });
    }, null, Math.max(0, pos - 0.01));
    tl.to(line, { drawSVG: '0% 100%', duration: 0.55, ease: 'fluent-decel' }, pos);
    // T4fix §F option 1：DrawSVG 完成後還 CSS baseline（dasharray 點珠 + strokeOpacity 0.30）
    tl.set(line, { clearProps: 'strokeDasharray,strokeDashoffset,strokeOpacity' }, pos + 0.55);
    // strokeWidth micro pulse 保留（在點珠 baseline 上仍可見）
    tl.to(line, { strokeWidth: 2.2, duration: 0.09, ease: 'fluent' }, pos + 0.55);
    tl.to(line, { strokeWidth: 1.5, duration: 0.13, ease: 'fluent' }, pos + 0.64);
  } else {
    // Fallback：opacity 0 → 1（無 DrawSVG，dasharray baseline 不會被覆寫；strokeOpacity 仍清）
    tl.call(() => {
      line.classList.remove('rail--hidden');
      // T5 (CD-T5-2 symmetry guard)：DrawSVG 與 Fallback 兩 branch 對稱注入 strokeOpacity:0.10。
      // 起點 element opacity:0 不可見，加 strokeOpacity 在那一刻無視覺差異；tween opacity 升到 1
      // 那刻 element 浮現、strokeOpacity 已就緒、rail 立刻可見。clearProps 在 pos+0.55 清掉，
      // rail 落回 CSS baseline 0（spec §3.2）。
      gsap.set(line, { opacity: 0, strokeOpacity: 0.10 });
    }, null, Math.max(0, pos - 0.01));
    tl.to(line, { opacity: 1, duration: 0.55, ease: 'fluent-decel' }, pos);
    tl.set(line, { clearProps: 'strokeOpacity' }, pos + 0.55);
  }
}

/**
 * railDrawOut — opacity 1 → 0 exit 動畫（CD-56B-3）
 */
export function railDrawOut(line, tl, pos) {
  if (!line || !tl) return;
  tl.to(line, { opacity: 0, duration: 0.35, ease: 'fluent-accel' }, pos);
}

// ---------------------------------------------------------------------------
// T2fix2 Rail Energy helpers — T4fix 改寫（CD-T2FIX-3 amendment）
// 強度數值依 TASK-56b-T4fix.md §C state model hierarchy 表
// ---------------------------------------------------------------------------

/**
 * railSweep — sweep overlay line 單一光點流動（T4fix：DrawSVG → stroke-dashoffset）
 *
 * 單一 bead packet 設計（task §B 策略 1）：
 *   dasharray: `${beadW} ${len}` → period = len + beadW > 線長，任一時刻只看到 1 個 bead
 *   dashoffset 從 0 → -len：bead 從中心側流向 anchor 側
 *
 * 啟動前 copy x1/y1/x2/y2 自 sourceLine（同步呼吸 y 偏移）。
 * 收尾 clearProps 完整還 CSS baseline（§I 契約）。
 */
export function railSweep(sweepLine, sourceLine, { onComplete } = {}) {
  if (!sweepLine || !sourceLine) return;

  // CRITICAL: 啟動前從 sourceLine copy 座標
  sweepLine.setAttribute('x1', sourceLine.getAttribute('x1'));
  sweepLine.setAttribute('y1', sourceLine.getAttribute('y1'));
  sweepLine.setAttribute('x2', sourceLine.getAttribute('x2'));
  sweepLine.setAttribute('y2', sourceLine.getAttribute('y2'));

  const len = sweepLine.getTotalLength();
  if (!(len > 0)) return; // 0-length line（座標重合）— 跳過 sweep
  const beadW = 12;

  gsap.set(sweepLine, {
    strokeDasharray: `${beadW} ${len}`,
    strokeDashoffset: 0,
    strokeOpacity: 1,
    opacity: 1,
  });

  gsap.to(sweepLine, {
    strokeDashoffset: -len,
    duration: 0.50,
    ease: 'fluent',
    onComplete: () => {
      gsap.set(sweepLine, {
        opacity: 0,
        clearProps: 'strokeDasharray,strokeDashoffset,strokeOpacity',
      });
      onComplete?.();
    },
  });
}

/**
 * railFocusPulse — hover focused rail 短 entry pulse（T4fix）
 *
 * state model（§C）：
 *   1. add `.rail--bright` class → CSS steady 0.85
 *   2. GSAP set strokeOpacity peak 0.95 → ease 回 0.85（0.18s）
 *   3. clearProps:'strokeOpacity' → CSS class 接管 hover 期間 steady
 */
export function railFocusPulse(line) {
  if (!line) return;
  line.classList.add('rail--bright');
  gsap.set(line, { strokeOpacity: 0.95 });
  gsap.to(line, {
    strokeOpacity: 0.85,
    duration: 0.18,
    ease: 'fluent-decel',
    onComplete: () => gsap.set(line, { clearProps: 'strokeOpacity' }),
  });
}

/**
 * railClickedPulse — clicked rail 脈衝收掉（接 timeline，T4fix：strokeWidth → strokeOpacity）
 *
 * state model（§C）：
 *   1. strokeOpacity 短脈衝 peak 0.95（無 class，0.12s）
 *   2. element opacity 收掉（exit 機制，0.18s）
 *   3. clearProps:'strokeOpacity' 雙保險（§I）
 */
export function railClickedPulse(line, tl, pos) {
  if (!line || !tl) return;
  tl.to(line, { strokeOpacity: 0.95, duration: 0.12, ease: 'fluent' }, pos);
  tl.to(line, { opacity: 0, duration: 0.18, ease: 'fluent-accel' }, pos + 0.12);
  tl.set(line, { clearProps: 'strokeOpacity' }, pos + 0.30);
}

/**
 * railShimmer — carry-over persist rail 微脈衝（T4fix：strokeWidth → strokeOpacity）
 *
 * state model（§C）：peak 0.50（無 class）→ 回 baseline 0.30 → clearProps（§I）
 */
export function railShimmer(line) {
  if (!line) return;
  gsap.timeline()
    .to(line, { strokeOpacity: 0.50, duration: 0.14, ease: 'fluent' })
    .to(line, { strokeOpacity: 0.30, duration: 0.24, ease: 'fluent' })
    .call(() => gsap.set(line, { clearProps: 'strokeOpacity' }));
}

/**
 * resetSweepLine — sweep-line 強制重置（T4fix：去掉 drawSVG 字面字）
 * killTweens + opacity 0 + clearProps 完整還 CSS baseline（§I）
 */
export function resetSweepLine(sweepLine) {
  if (!sweepLine) return;
  gsap.killTweensOf(sweepLine);
  gsap.set(sweepLine, {
    opacity: 0,
    clearProps: 'strokeDasharray,strokeDashoffset,strokeOpacity',
  });
}
