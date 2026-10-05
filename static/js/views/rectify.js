/* 视图：文档矫正（自动摆正 + 透视校正，四角拖拽微调，前后对比）。 */
window.Views = window.Views || {};
window.Views.rectify = (function () {
  const C = window.Common;

  // 模块状态
  const S = {
    el: null,
    imageId: null,
    imageRec: null,
    mode: "skew",                 // "skew" | "perspective"
    // 倾斜
    detectedAngle: null,         // 服务端检测到的摆正角；null 表示尚未检测
    confidence: 0,
    trim: true,
    // 透视
    corners: null,               // 归一化 [[x,y]×4] TL/TR/BR/BL
    quadConfidence: 0,
    quadMethod: "",
    // 结果
    resultUrl: null,
    // 对比
    splitPos: 50,
    // 请求标记（避免过期响应覆盖新状态）
    reqSeq: 0,
  };

  function $(sel) { return S.el.querySelector(sel); }

  // ------------------------------------------------------------------ 挂载
  function mount(el) {
    S.el = el;
    el.innerHTML = `
      <div class="split">
        <div class="col">
          <div class="panel">
            <div class="panel-title">选择图像</div>
            <div id="rc-gallery" style="max-height:260px;overflow:auto"></div>
          </div>
          <div class="panel">
            <div class="panel-title">矫正方式</div>
            <div class="mode-tabs" id="rc-mode-tabs">
              <button class="mode-tab active" data-mode="skew">📐 倾斜摆正</button>
              <button class="mode-tab" data-mode="perspective">🧾 透视校正</button>
            </div>
          </div>
          <div class="panel" id="rc-skew-panel">
            <div class="panel-title">自动摆正<span class="dim">检测水平/竖直方向</span></div>
            <button class="btn btn-primary" id="rc-auto-skew" style="width:100%;justify-content:center">🔍 自动检测并摆正</button>
            <div class="field" style="margin-top:14px">
              <label>角度微调 <span class="hint">检测不准时拖动，结果实时更新</span></label>
              <div class="range-row">
                <input type="range" id="rc-angle" min="-45" max="45" step="0.1" value="0" disabled>
                <span class="range-val" id="rc-angle-val">0.0°</span>
              </div>
              <div id="rc-skew-meta" style="margin-top:6px"><span class="dim">尚未检测</span></div>
            </div>
            <div class="field" style="display:flex;align-items:center;gap:8px;margin-bottom:0">
              <input type="checkbox" id="rc-trim" checked> <label style="margin:0">自动裁掉旋转白边</label>
            </div>
          </div>
          <div class="panel" id="rc-persp-panel" hidden>
            <div class="panel-title">透视校正<span class="dim">按四条边把梯形拉回矩形</span></div>
            <button class="btn btn-primary" id="rc-auto-quad" style="width:100%;justify-content:center">🔍 自动检测文档四角</button>
            <div class="field" style="margin-top:10px">
              <label>四角微调 <span class="hint">在右侧图上直接拖拽黄点，松手即更新</span></label>
              <div class="corner-list mono" id="rc-corner-list"></div>
            </div>
            <div class="field" style="display:flex;align-items:center;gap:8px;margin-bottom:0">
              <input type="checkbox" id="rc-persp-trim" checked> <label style="margin:0">裁掉边缘空白</label>
            </div>
          </div>
          <div class="panel">
            <div class="panel-title">结果</div>
            <div id="rc-result-meta" class="keypoint-stats" style="margin-bottom:10px"><span class="dim">选择图像后操作</span></div>
            <a class="btn" id="rc-download" style="width:100%;justify-content:center;display:none" download="rectified.png">⬇️ 下载矫正结果</a>
          </div>
        </div>

        <div class="col">
          <div class="panel">
            <div class="panel-title">前后对比<span class="dim">拖动分隔线 · 左原图 / 右矫正后</span></div>
            <div class="compare-wrap rectify-wrap" id="rc-wrap">
              <img id="rc-before" alt="原图">
              <div class="rc-after-clip" id="rc-after-clip">
                <img id="rc-after" class="placeholder" alt="矫正后">
              </div>
              <div class="rc-overlay-clip" id="rc-overlay-clip">
                <canvas id="rc-overlay"></canvas>
              </div>
              <div class="compare-handle" id="rc-handle" style="left:50%"></div>
              <div id="rc-empty"><div class="empty"><span class="big">📑</span>选择一张拍歪的文档或纸张照片<br>然后点击「自动检测」一键摆正</div></div>
            </div>
            <div id="rc-loading" hidden><div class="loading">矫正计算中…</div></div>
            <div class="caption" style="text-align:center;color:var(--text-faint);font-size:12px;margin-top:8px">左：原图 ｜ 右：矫正结果</div>
          </div>
        </div>
      </div>`;

    // 图库
    C.fetchImages().then((images) => {
      const g = $("#rc-gallery");
      g.innerHTML = C.galleryHTML(images);
      C.bindGallery(g, images, (id, rec) => selectImage(id, rec));
    });

    // 模式切换
    $("#rc-mode-tabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".mode-tab");
      if (btn) setMode(btn.dataset.mode);
    });

    // 倾斜
    $("#rc-auto-skew").onclick = autoSkew;
    const angleSlider = $("#rc-angle");
    angleSlider.addEventListener("input", () => {
      $("#rc-angle-val").textContent = Number(angleSlider.value).toFixed(1) + "°";
    });
    // 拖动滑块实时更新（300ms 节流）
    angleSlider.addEventListener("input", C.debounce(() => {
      if (S.imageId && S.detectedAngle !== null && !angleSlider.disabled) applySkew();
    }, 300));
    $("#rc-trim").onchange = () => { S.trim = $("#rc-trim").checked; if (S.resultUrl) applySkew(); };

    // 透视
    $("#rc-auto-quad").onclick = autoQuad;
    $("#rc-persp-trim").onchange = () => { S.trim = $("#rc-persp-trim").checked; if (S.corners) runPerspective(); };

    $("#rc-before").addEventListener("load", () => { layoutImages(); drawOverlay(); });
    bindCompareSlider();
    bindCornerDrag();
    window.addEventListener("resize", () => { layoutImages(); drawOverlay(); });
  }

  // ------------------------------------------------------------------ 图像选择
  function selectImage(id, rec) {
    S.imageId = id;
    S.imageRec = rec;
    S.detectedAngle = null;
    S.confidence = 0;
    S.corners = null;
    S.quadConfidence = 0;
    S.resultUrl = null;
    S.reqSeq++;

    $("#rc-before").src = rec.file_url;
    const after = $("#rc-after");
    after.removeAttribute("src");
    after.classList.add("placeholder");
    after.style.left = after.style.top = after.style.width = after.style.height = "";
    $("#rc-empty").hidden = false;
    $("#rc-download").style.display = "none";
    const slider = $("#rc-angle");
    slider.value = 0;
    slider.disabled = true;
    $("#rc-angle-val").textContent = "0.0°";
    $("#rc-skew-meta").innerHTML = `<span class="dim">尚未检测</span>`;
    $("#rc-corner-list").innerHTML = `<span class="dim">点击上方按钮自动检测</span>`;
    $("#rc-result-meta").innerHTML = `<span class="dim">${C.esc(rec.filename)} · ${rec.width}×${rec.height}</span>`;
    S.splitPos = 50;
    applySplit();
    layoutImages();
    drawOverlay();
  }

  // ------------------------------------------------------------------ 模式
  function setMode(mode) {
    S.mode = mode;
    S.el.querySelectorAll(".mode-tab").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    $("#rc-skew-panel").hidden = mode !== "skew";
    $("#rc-persp-panel").hidden = mode !== "perspective";
    drawOverlay();
  }

  // ------------------------------------------------------------------ 倾斜
  async function autoSkew() {
    if (!S.imageId) { C.toast("请先选择图像", "error"); return; }
    setLoading(true);
    try {
      const info = await Api.post("/api/rectify/detect-skew", { image_id: S.imageId });
      if (S.mode !== "skew") return;
      S.detectedAngle = info.angle;
      S.confidence = info.confidence;
      const slider = $("#rc-angle");
      slider.disabled = false;
      slider.value = info.angle;
      $("#rc-angle-val").textContent = info.angle.toFixed(1) + "°";
      const pct = (info.confidence * 100) | 0;
      const confTxt = info.confidence >= 0.6 ? `<span class="badge green">高 ${pct}%</span>`
        : info.confidence >= 0.25 ? `<span class="badge amber">中 ${pct}%</span>`
        : `<span class="badge red">低 ${pct}%</span>`;
      $("#rc-skew-meta").innerHTML =
        `${confTxt} 检测倾斜 ${info.tilt.toFixed(2)}° · 建议旋转 ${info.angle.toFixed(2)}°` +
        (info.confidence < 0.25 ? " · 置信较低，建议手动微调" : "");
      await applySkew();
    } catch (e) {
      C.toast("检测失败：" + e.message, "error");
    } finally {
      setLoading(false);
    }
  }

  async function applySkew() {
    if (!S.imageId || S.detectedAngle === null) return;
    const angle = Number($("#rc-angle").value);
    const seq = ++S.reqSeq;
    setLoading(true);
    try {
      const r = await Api.post("/api/rectify/straighten", {
        image_id: S.imageId, angle, trim: S.trim,
      });
      if (seq !== S.reqSeq) return;
      showResult(r);
    } catch (e) {
      if (seq === S.reqSeq) C.toast("摆正失败：" + e.message, "error");
    } finally {
      if (seq === S.reqSeq) setLoading(false);
    }
  }

  // ------------------------------------------------------------------ 透视
  async function autoQuad() {
    if (!S.imageId) { C.toast("请先选择图像", "error"); return; }
    setLoading(true);
    try {
      const r = await Api.post("/api/rectify/detect-quad", { image_id: S.imageId });
      S.corners = r.normalized;
      S.quadConfidence = r.confidence;
      S.quadMethod = r.method;
      renderCornerList();
      drawOverlay();
      await runPerspective();
      if (r.confidence < 0.25) C.toast("自动检测置信度低，请拖动四角手动微调", "error");
    } catch (e) {
      C.toast("四角检测失败：" + e.message, "error");
    } finally {
      setLoading(false);
    }
  }

  async function runPerspective() {
    if (!S.imageId || !S.corners) return;
    const seq = ++S.reqSeq;
    setLoading(true);
    try {
      const r = await Api.post("/api/rectify/perspective", {
        image_id: S.imageId, corners: S.corners, trim: S.trim,
      });
      if (seq !== S.reqSeq) return;
      showResult(r);
    } catch (e) {
      if (seq === S.reqSeq) C.toast("透视校正失败：" + e.message, "error");
    } finally {
      if (seq === S.reqSeq) setLoading(false);
    }
  }

  function renderCornerList() {
    const names = ["左上 TL", "右上 TR", "右下 BR", "左下 BL"];
    const pct = (S.quadConfidence * 100) | 0;
    const conf = S.quadConfidence >= 0.6 ? `<span class="badge green">${pct}%</span>`
      : S.quadConfidence >= 0.25 ? `<span class="badge amber">${pct}%</span>`
      : `<span class="badge red">${pct}%</span>`;
    $("#rc-corner-list").innerHTML =
      `<div style="margin-bottom:6px">${conf} ${S.quadMethod === "boundary" ? "轮廓检测成功" : "默认估计，请手动调整"}</div>` +
      S.corners.map((p, i) => `<div>${names[i]}：(${p[0].toFixed(3)}, ${p[1].toFixed(3)})</div>`).join("");
  }

  // ------------------------------------------------------------------ 结果
  // 计算图像在容器内的 contain 布局（两图共用同一高度区域，按各自宽高比缩放居中）
  function letterbox(natW, natH, boxW, boxH) {
    const scale = Math.min(boxW / natW, boxH / natH);
    const w = natW * scale, h = natH * scale;
    return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h, scale };
  }

  function showResult(r) {
    S.resultUrl = r.file_url;
    const url = r.file_url + "?t=" + Date.now();
    const after = $("#rc-after");
    after.classList.remove("placeholder");
    after.onload = () => { layoutImages(); drawOverlay(); };
    after.src = url;
    $("#rc-empty").hidden = true;
    const dl = $("#rc-download");
    dl.style.display = "inline-flex";
    dl.href = url;

    const parts = [];
    if (r.angle !== undefined) parts.push(`旋转 ${Number(r.angle).toFixed(2)}°`);
    if (r.width && r.height && r.out_width === undefined) parts.push(`输出 ${r.width}×${r.height}`);
    if (r.out_width) parts.push(`输出 ${r.out_width}×${r.out_height}`);
    parts.push(r.cache_hit ? "缓存命中" : "已计算");
    $("#rc-result-meta").innerHTML = parts.map(C.esc).join(" · ");
    layoutImages();
  }

  // 让原图与结果图在同一容器内按各自宽高比 contain 居中（底部原图决定整体高度）
  function layoutImages() {
    const before = $("#rc-before");
    const after = $("#rc-after");
    const wrap = $("#rc-wrap");
    const wrapRect = wrap.getBoundingClientRect();
    if (before.naturalWidth) {
      // 容器高度跟随原图宽度比例
      const bh = wrapRect.width * before.naturalHeight / before.naturalWidth;
      wrap.style.height = bh + "px";
    }
    const boxW = wrap.clientWidth, boxH = wrap.clientHeight;
    if (after.naturalWidth && after.classList.contains("placeholder") === false) {
      const b = letterbox(after.naturalWidth, after.naturalHeight, boxW, boxH);
      after.style.left = b.x + "px";
      after.style.top = b.y + "px";
      after.style.width = b.w + "px";
      after.style.height = b.h + "px";
    }
  }

  function setLoading(on) { $("#rc-loading").hidden = !on; }

  // ------------------------------------------------------------------ 对比滑块
  function bindCompareSlider() {
    const wrap = $("#rc-wrap");
    const handle = $("#rc-handle");
    const moveTo = (clientX) => {
      const rect = wrap.getBoundingClientRect();
      S.splitPos = Math.max(0, Math.min(100, (clientX - rect.left) / rect.width * 100));
      applySplit();
    };
    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const move = (ev) => moveTo(ev.clientX);
      const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  function applySplit() {
    const p = S.splitPos;
    $("#rc-after-clip").style.clipPath = `inset(0 0 0 ${p.toFixed(2)}%)`;
    // 四角覆盖层只跟随左侧原图（右侧是矫正结果，不应被检测框遮挡）
    $("#rc-overlay-clip").style.clipPath = `inset(0 ${(100 - p).toFixed(2)}% 0 0)`;
    $("#rc-handle").style.left = p + "%";
  }

  // ------------------------------------------------------------------ 四角覆盖层与拖拽
  function drawOverlay() {
    if (!S.el) return;
    const canvas = $("#rc-overlay");
    const before = $("#rc-before");
    if (!before.complete || !before.naturalWidth) return;
    const wrap = $("#rc-wrap");
    const boxW = wrap.clientWidth, boxH = wrap.clientHeight;
    if (boxW < 10 || boxH < 10) return;
    // overlay 与原图对齐（原图铺满容器宽度，letterbox 竖直居中）
    const b = letterbox(before.naturalWidth, before.naturalHeight, boxW, boxH);
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(b.w * dpr);
    canvas.height = Math.round(b.h * dpr);
    canvas.style.left = b.x + "px";
    canvas.style.top = b.y + "px";
    canvas.style.width = b.w + "px";
    canvas.style.height = b.h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, b.w, b.h);
    if (S.mode !== "perspective" || !S.corners) { applySplit(); return; }

    const pts = S.corners.map(([nx, ny]) => ({ x: nx * b.w, y: ny * b.h }));
    ctx.strokeStyle = "#ff5252";
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < 4; i++) ctx.lineTo(pts[i].x, pts[i].y);
    ctx.closePath();
    ctx.fillStyle = "rgba(79,140,255,0.07)";
    ctx.fill();
    ctx.stroke();
    pts.forEach((p) => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 9, 0, Math.PI * 2);
      ctx.fillStyle = "#ffc107";
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.stroke();
    });
    applySplit();
  }

  function bindCornerDrag() {
    const canvas = $("#rc-overlay");
    let dragIdx = -1;
    const hit = (e) => {
      const r = canvas.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      if (!S.corners) return -1;
      for (let i = 0; i < 4; i++) {
        const p = { x: S.corners[i][0] * r.width, y: S.corners[i][1] * r.height };
        if (Math.hypot(x - p.x, y - p.y) < 16) return i;
      }
      return -1;
    };
    canvas.addEventListener("mousedown", (e) => {
      if (S.mode !== "perspective" || !S.corners) return;
      dragIdx = hit(e);
      if (dragIdx >= 0) { e.preventDefault(); e.stopPropagation(); canvas.style.cursor = "grabbing"; }
    });
    document.addEventListener("mousemove", (e) => {
      if (dragIdx < 0) return;
      const r = canvas.getBoundingClientRect();
      const nx = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
      const ny = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
      S.corners[dragIdx] = [Number(nx.toFixed(4)), Number(ny.toFixed(4))];
      drawOverlay();
      renderCornerList();
    });
    document.addEventListener("mouseup", () => {
      if (dragIdx >= 0) {
        dragIdx = -1;
        canvas.style.cursor = "";
        runPerspective();
      }
    });
  }

  return { mount };
})();
