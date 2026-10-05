/* 视图：文档矫正（倾斜自动摆正 + 透视校正，支持手动微调和前后对比）。 */
window.Views = window.Views || {};
window.Views.rectify = (function () {
  const C = window.Common;
  let imgId = null, imgRec = null;
  // 倾斜
  let autoAngle = 0, confidence = 0;
  let resultUrl = null, resultId = null, mode = "deskew";
  let perspView = "edit";   // 透视模式下：edit 拖角 / result 看结果
  let reqToken = 0;
  let cmpPos = 50;          // 对比滑块位置

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div class="col">
            <div class="panel"><div class="panel-title">选择图像</div><div id="rf-gallery"></div></div>
            <div class="panel">
              <div class="panel-title">矫正方式</div>
              <div class="select-row" style="margin-bottom:10px">
                <button class="btn btn-sm" id="rf-mode-deskew">倾斜摆正</button>
                <button class="btn btn-sm" id="rf-mode-persp">透视校正</button>
              </div>

              <div id="rf-deskew-panel">
                <button class="btn btn-primary" id="rf-auto-angle">① 自动检测倾斜角</button>
                <div class="keypoint-stats" style="margin:8px 0">
                  <span class="dim">检测角度：</span><strong id="rf-auto-val">—</strong>
                  <span class="dim" id="rf-conf"></span>
                </div>
                <div class="field">
                  <label>旋转角度（度）<span class="hint">检测不准时拖动微调，结果实时更新</span></label>
                  <div class="range-row">
                    <input type="range" id="rf-angle" min="-45" max="45" step="0.1" value="0">
                    <span class="range-val" id="rf-angle-val">0</span>
                  </div>
                </div>
                <label style="font-size:12px;display:flex;align-items:center;gap:6px;margin-bottom:10px">
                  <input type="checkbox" id="rf-autocrop" checked> 自动裁掉旋转白边
                </label>
              </div>

              <div id="rf-persp-panel" hidden>
                <button class="btn btn-primary" id="rf-auto-corners">① 自动检测文档四角</button>
                <div class="keypoint-stats" style="margin:8px 0">
                  <span class="dim">检测置信度：</span><strong id="rf-corner-score">—</strong>
                </div>
                <div class="dim" style="font-size:12px;line-height:1.6">
                  ② 在图上拖动四个角点（左上/右上/右下/左下）对齐纸张四角，结果实时更新。<br>
                  角点顺序错误时点「重置为整幅」重新拖。
                </div>
                <div class="select-row" style="margin-top:8px">
                  <button class="btn btn-sm" id="rf-reset-corners">重置为整幅</button>
                  <button class="btn btn-sm" id="rf-view-result">查看矫正结果</button>
                </div>
              </div>

              <div style="display:flex;gap:8px;margin-top:12px">
                <button class="btn" id="rf-apply">应用矫正</button>
                <a class="btn btn-ghost" id="rf-download" download="corrected.png" style="text-decoration:none;text-align:center">下载结果</a>
              </div>
            </div>
          </div>

          <div class="col">
            <div class="panel">
              <div class="panel-title">矫正前后对比<span class="dim">拖动分隔线 · 左原图 / 右矫正后</span></div>
              <div class="compare-wrap" id="rf-wrap" style="min-height:320px">
                <div style="position:absolute;inset:0;display:grid;place-items:center" id="rf-orig-slot">
                  <span class="dim">请先选择图像</span>
                </div>
                <img id="rf-result" style="position:absolute;inset:0;width:100%;height:100%;object-fit:contain;clip-path:inset(0 50% 0 0);z-index:2" hidden>
                <canvas id="rf-corner-canvas" style="position:absolute;inset:0;width:100%;height:100%;cursor:crosshair;z-index:2" hidden></canvas>
                <div class="compare-handle" id="rf-handle" style="left:50%;z-index:3"></div>
                <button class="btn btn-sm" id="rf-back-edit"
                  style="position:absolute;top:10px;right:10px;z-index:5;display:none">← 调整角点</button>
              </div>
              <div class="caption" id="rf-status" style="text-align:center;margin-top:8px;color:var(--text-faint);font-size:12px"></div>
            </div>
          </div>
        </div>`;

      C.fetchImages().then((images) => {
        el.querySelector("#rf-gallery").innerHTML = C.galleryHTML(images);
        C.bindGallery(el.querySelector("#rf-gallery"), images, (id, rec) => selectImage(el, id, rec));
      });

      el.querySelector("#rf-mode-deskew").onclick = () => setMode(el, "deskew");
      el.querySelector("#rf-mode-persp").onclick = () => setMode(el, "persp");

      el.querySelector("#rf-auto-angle").onclick = () => detectAngle(el);
      el.querySelector("#rf-angle").addEventListener("input", C.debounce((e) => {
        el.querySelector("#rf-angle-val").textContent = Number(e.target.value).toFixed(1);
        scheduleDeskew(el);
      }, 180));
      el.querySelector("#rf-autocrop").onchange = () => scheduleDeskew(el);

      el.querySelector("#rf-auto-corners").onclick = () => detectCorners(el);
      el.querySelector("#rf-reset-corners").onclick = () => resetCorners(el);
      el.querySelector("#rf-view-result").onclick = () => setPerspView(el, "result");
      el.querySelector("#rf-back-edit").onclick = () => setPerspView(el, "edit");

      el.querySelector("#rf-apply").onclick = () => {
        if (resultUrl) C.toast("结果已生成，可直接下载或在结果对比中查看", "success");
        else C.toast("请先生成矫正结果", "error");
      };
      el.querySelector("#rf-download").onclick = (e) => {
        if (!resultUrl) { e.preventDefault(); C.toast("请先矫正", "error"); }
      };

      bindSlider(el);
      bindCornerDrag(el);
      setMode(el, "deskew");
    },

    refresh() {
      C.refreshImages().then(() => {
        const el = document.querySelector('.view[data-view="rectify"]');
        if (!el || !this.mounted) return;
        C.fetchImages().then((images) => {
          el.querySelector("#rf-gallery").innerHTML = C.galleryHTML(images);
          C.bindGallery(el.querySelector("#rf-gallery"), images, (id, rec) => selectImage(el, id, rec));
        });
      });
    },
  };

  // ---------------------------------------------------------------- 模式
  function setMode(el, m) {
    mode = m;
    el.querySelector("#rf-mode-deskew").classList.toggle("btn-primary", m === "deskew");
    el.querySelector("#rf-mode-persp").classList.toggle("btn-primary", m === "persp");
    el.querySelector("#rf-deskew-panel").hidden = m !== "deskew";
    el.querySelector("#rf-persp-panel").hidden = m !== "persp";
    if (m === "deskew") setPerspView(el, "result");  // 倾斜模式直接看结果对比
    else setPerspView(el, perspView === "result" && resultUrl ? "result" : "edit");
  }

  // 透视模式的舞台：edit 显示可拖角画布；result 显示原图/结果滑块对比。
  function setPerspView(el, view) {
    perspView = view;
    const canvas = el.querySelector("#rf-corner-canvas");
    const result = el.querySelector("#rf-result");
    const handle = el.querySelector("#rf-handle");
    const origSlot = el.querySelector("#rf-orig-slot");
    const back = el.querySelector("#rf-back-edit");
    const showEdit = mode === "persp" && view === "edit";
    canvas.hidden = !showEdit;
    if (showEdit) drawCornerCanvas(el);
    // 对比视图：deskew 或 persp-result
    const showCompare = (mode === "deskew") || (mode === "persp" && view === "result");
    result.hidden = !(showCompare && result.src);
    handle.style.display = showCompare ? "" : "none";
    origSlot.style.display = showCompare ? "" : "none";
    back.style.display = (mode === "persp" && view === "result") ? "" : "none";
    if (showCompare) result.style.clipPath = `inset(0 ${100 - cmpPos}% 0 0)`;
  }

  function selectImage(el, id, rec) {
    imgId = id; imgRec = rec;
    resultUrl = null; resultId = null; autoAngle = 0; confidence = 0;
    corners = null; baseImage = null;
    el.querySelector("#rf-orig-slot").innerHTML =
      `<img src="${rec.file_url}" style="width:100%;height:100%;object-fit:contain;display:block">`;
    const res = el.querySelector("#rf-result");
    res.src = ""; res.hidden = true;
    el.querySelector("#rf-angle").value = 0;
    el.querySelector("#rf-angle-val").textContent = "0";
    el.querySelector("#rf-auto-val").textContent = "—";
    el.querySelector("#rf-conf").textContent = "";
    el.querySelector("#rf-corner-score").textContent = "—";
    el.querySelector("#rf-status").textContent = "已选择：" + rec.filename;
    el.querySelector("#rf-download").removeAttribute("href");
    setPerspView(el, mode === "persp" ? "edit" : "result");
  }

  // ---------------------------------------------------------------- 倾斜
  async function detectAngle(el) {
    if (!imgId) return C.toast("请先选择图像", "error");
    el.querySelector("#rf-status").textContent = "正在检测倾斜角…";
    try {
      const r = await Api.post("/api/rectify/skew", { image_id: imgId });
      autoAngle = r.angle; confidence = r.confidence;
      el.querySelector("#rf-auto-val").textContent = `${r.angle.toFixed(2)}°`;
      el.querySelector("#rf-conf").textContent =
        r.confidence > 0.05 ? `（置信度 ${(r.confidence * 100) | 0}%）` : "（未检测到明显倾斜）";
      el.querySelector("#rf-angle").value = r.angle;
      el.querySelector("#rf-angle-val").textContent = r.angle.toFixed(1);
      scheduleDeskew(el);
    } catch (e) { el.querySelector("#rf-status").textContent = "检测失败：" + e.message; }
  }

  const runDeskew = C.debounce(async (el) => {
    if (!imgId) return;
    const myReq = ++reqToken;
    const angle = Number(el.querySelector("#rf-angle").value);
    const autocrop = el.querySelector("#rf-autocrop").checked;
    el.querySelector("#rf-status").textContent = "矫正中…";
    try {
      const r = await Api.post("/api/rectify/deskew", { image_id: imgId, angle, autocrop });
      if (myReq !== reqToken) return;
      showResult(el, r);
      el.querySelector("#rf-status").textContent =
        `已旋转 ${r.angle}°${r.cropped ? "，并裁掉白边" : ""}`;
    } catch (e) { if (myReq === reqToken) el.querySelector("#rf-status").textContent = "失败：" + e.message; }
  }, 120);

  function scheduleDeskew(el) { runDeskew(el); }

  // ---------------------------------------------------------------- 透视
  let corners = null;     // 原图像素坐标
  let baseImage = null;   // 已加载的 HTMLImageElement
  let dragIndex = -1;

  function defaultCorners(rec) {
    const mx = rec.width * 0.05, my = rec.height * 0.05;
    return [[mx, my], [rec.width - mx, my],
            [rec.width - mx, rec.height - my], [mx, rec.height - my]];
  }

  async function detectCorners(el) {
    if (!imgId) return C.toast("请先选择图像", "error");
    el.querySelector("#rf-status").textContent = "正在检测文档四角…";
    try {
      const r = await Api.post("/api/rectify/corners", { image_id: imgId });
      corners = r.corners;
      el.querySelector("#rf-corner-score").textContent =
        r.score > 0 ? `${(r.score * 100) | 0}%` : "检测失败（显示整幅，请手动拖角）";
      drawCornerCanvas(el);
      runPerspective(el);
    } catch (e) { el.querySelector("#rf-status").textContent = "检测失败：" + e.message; }
  }

  function resetCorners(el) {
    if (!imgRec) return;
    corners = defaultCorners(imgRec);
    drawCornerCanvas(el);
    runPerspective(el);
  }

  function canvasRect(el) {
    const canvas = el.querySelector("#rf-corner-canvas");
    const wrap = el.querySelector("#rf-wrap").getBoundingClientRect();
    // object-fit: contain 的实际显示矩形
    const iw = baseImage.naturalWidth, ih = baseImage.naturalHeight;
    const scale = Math.min(wrap.width / iw, wrap.height / ih);
    const dw = iw * scale, dh = ih * scale;
    return {
      scale,
      dx: (wrap.width - dw) / 2, dy: (wrap.height - dh) / 2, dw, dh,
      cssW: wrap.width, cssH: wrap.height, canvas,
    };
  }

  function drawCornerCanvas(el) {
    if (!imgRec || !baseImage || !baseImage.naturalWidth) return;
    const canvas = el.querySelector("#rf-corner-canvas");
    const wrap = el.querySelector("#rf-wrap").getBoundingClientRect();
    if (wrap.width < 10 || wrap.height < 10) return;
    canvas.width = wrap.width; canvas.height = wrap.height;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!baseImage || baseImage.dataset.id !== imgId) {
      baseImage = new Image();
      baseImage.dataset.id = imgId;
      baseImage.onload = () => drawCornerCanvas(el);
      baseImage.src = imgRec.file_url;
      return;
    }
    const rc = canvasRect(el);
    ctx.drawImage(baseImage, rc.dx, rc.dy, rc.dw, rc.dh);
    if (!corners) corners = defaultCorners(imgRec);
    const pts = corners.map(([x, y]) => ({ x: rc.dx + x * rc.scale, y: rc.dy + y * rc.scale }));
    ctx.strokeStyle = "rgba(255,82,82,.95)";
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
    ctx.closePath(); ctx.stroke();
    pts.forEach((p, i) => {
      ctx.fillStyle = "#ff5252";
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(p.x, p.y, 8, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#fff"; ctx.font = "bold 11px sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(String(i + 1), p.x, p.y);
    });
  }

  function toImgCoords(el, clientX, clientY) {
    const wrap = el.querySelector("#rf-wrap").getBoundingClientRect();
    const rc = canvasRect(el);
    return {
      x: (clientX - wrap.left - rc.dx) / rc.scale,
      y: (clientY - wrap.top - rc.dy) / rc.scale,
    };
  }

  function bindCornerDrag(el) {
    const canvas = el.querySelector("#rf-corner-canvas");
    const hit = (ev) => {
      if (!corners) return -1;
      const wrap = el.querySelector("#rf-wrap").getBoundingClientRect();
      const rc = canvasRect(el);
      let best = -1, bd = 14;
      corners.forEach(([x, y], i) => {
        const px = rc.dx + x * rc.scale, py = rc.dy + y * rc.scale;
        const d = Math.hypot(ev.clientX - wrap.left - px, ev.clientY - wrap.top - py);
        if (d < bd) { bd = d; best = i; }
      });
      return best;
    };
    canvas.addEventListener("mousedown", (e) => { dragIndex = hit(e); });
    window.addEventListener("mousemove", (e) => {
      if (dragIndex < 0 || mode !== "persp") return;
      const p = toImgCoords(el, e.clientX, e.clientY);
      p.x = Math.max(0, Math.min(imgRec.width, p.x));
      p.y = Math.max(0, Math.min(imgRec.height, p.y));
      corners[dragIndex] = [Math.round(p.x), Math.round(p.y)];
      drawCornerCanvas(el);
      schedulePerspective(el);
    });
    window.addEventListener("mouseup", () => { dragIndex = -1; });
  }

  let perspToken = 0;
  const schedulePerspective = C.debounce((el) => {
    if (!imgId || !corners) return;
    const myReq = ++perspToken;
    el.querySelector("#rf-status").textContent = "透视校正中…";
    Api.post("/api/rectify/perspective", { image_id: imgId, corners }).then((r) => {
      if (myReq !== perspToken) return;
      showResult(el, r);
      el.querySelector("#rf-status").textContent =
        `已拉正为 ${r.out_width}×${r.out_height} 的矩形（拖角实时更新，点「查看矫正结果」对比）`;
    }).catch((e) => { if (myReq === perspToken) el.querySelector("#rf-status").textContent = "失败：" + e.message; });
  }, 220);

  function runPerspective(el) { schedulePerspective(el); }

  // ---------------------------------------------------------------- 结果
  function showResult(el, r) {
    resultId = r.result_id;
    resultUrl = `/api/results/${r.result_id}/file?t=${Date.now()}`;
    const res = el.querySelector("#rf-result");
    res.src = resultUrl;
    el.querySelector("#rf-download").href = resultUrl;
    el.querySelector("#rf-download").setAttribute("download",
      mode === "deskew" ? "deskewed.png" : "perspective_corrected.png");
    // 倾斜模式立即显示对比；透视模式停在拖角视图，用户点「查看矫正结果」
    if (mode === "deskew") setPerspView(el, "result");
  }

  function bindSlider(el) {
    const wrap = el.querySelector("#rf-wrap");
    const handle = el.querySelector("#rf-handle");
    const result = el.querySelector("#rf-result");
    const apply = () => {
      result.style.clipPath = `inset(0 ${100 - cmpPos}% 0 0)`;
      handle.style.left = cmpPos + "%";
    };
    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const move = (ev) => {
        const rect = wrap.getBoundingClientRect();
        cmpPos = Math.max(0, Math.min(100, (ev.clientX - rect.left) / rect.width * 100));
        apply();
      };
      const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }
})();
