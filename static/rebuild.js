"use strict";

// 剪辑字幕重建：粘贴原 SRT 与 JSON 剪辑数组，按素材区间裁切/平移字幕，
// 乱序、重复、目标区间重叠均可处理；同一原块多段覆盖只显示一次。
// 预览重建后时间码、原块序位与正文（保留换行），可下载从 1 编号的 SRT。
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("rbSrt"),
    clips: $("rbClips"),
    runBtn: $("rbRunBtn"),
    clearBtn: $("rbClearBtn"),
    status: $("rbStatus"),
    summary: $("rbSummary"),
    resultWrap: $("rbResultWrap"),
    body: $("rbBody"),
    downloadBtn: $("rbDownloadBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:00,500 --> 00:00:02,000",
    "第一句字幕",
    "",
    "2",
    "00:00:03,000 --> 00:00:06,000",
    "第二句字幕第一行",
    "第二句字幕第二行",
    "",
    "3",
    "00:00:07,000 --> 00:00:09,000",
    "第三句字幕",
    "",
  ].join("\n");

  // 故意乱序：第 1 段素材重复使用两次，且第二份目标区间与块 2 重叠
  const SAMPLE_CLIPS = [
    "[",
    '  {"source_in": 3000, "source_out": 6000, "target_in": 2000},',
    '  {"source_in": 0,    "source_out": 3000, "target_in": 0},',
    '  {"source_in": 0,    "source_out": 3000, "target_in": 1500}',
    "]",
  ].join("\n");

  $("rbSampleSrt").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
  });
  $("rbSampleClips").addEventListener("click", () => {
    els.clips.value = SAMPLE_CLIPS;
  });

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
    els.clips.value = "";
    els.summary.classList.add("hidden");
    els.resultWrap.classList.add("hidden");
    setStatus("", false);
  });

  function setStatus(msg, isError) {
    els.status.textContent = msg;
    els.status.classList.toggle("error", !!isError);
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // 仅做 JSON 语法层面的前置校验；字段/取值合法性由后端逐项指出项号
  function parseClips(text) {
    const trimmed = text.trim();
    if (!trimmed) throw new Error("请填写 JSON 剪辑数组（至少一段剪辑区间）");
    let data;
    try {
      data = JSON.parse(trimmed);
    } catch (e) {
      throw new Error(`剪辑数组不是合法 JSON：${e.message}`);
    }
    if (!Array.isArray(data)) throw new Error("剪辑清单须为 JSON 数组");
    return data;
  }

  function render(data) {
    const s = data.summary;
    $("rbCnt").textContent = s.total;
    $("rbClipsCnt").textContent = s.clips;
    $("rbBlocks").textContent = s.source_blocks;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const tr = document.createElement("tr");
      tr.className = "row-match";
      const indices = r.source_indices
        .map((i) => `<span class="rb-pos">${i}</span>`)
        .join(" ");
      tr.innerHTML = `
        <td class="cell-idx">${r.new_index}</td>
        <td class="cell-tc cell-tc-new">${escapeHtml(r.start_tc)} → ${escapeHtml(r.end_tc)}</td>
        <td class="cell-idx rb-idx-cell">${indices}</td>
        <td class="cell-text cell-multiline">${escapeHtml(r.text)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    let clips;
    try {
      clips = parseClips(els.clips.value);
    } catch (e) {
      setStatus(e.message, true);
      return;
    }
    lastRequest = { srt: els.srt.value, clips };

    els.runBtn.disabled = true;
    setStatus("重建中……", false);
    try {
      const resp = await fetch("/api/rebuild", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `请求失败（${resp.status}）` }));
        throw new Error(err.detail || `请求失败（${resp.status}）`);
      }
      const data = await resp.json();
      render(data);
      const s = data.summary;
      setStatus(
        `完成：${s.clips} 段剪辑，重建出 ${s.total} 条字幕，涉及 ${s.source_blocks} 个原块`,
        false
      );
    } catch (e) {
      lastRequest = null;
      els.summary.classList.add("hidden");
      els.resultWrap.classList.add("hidden");
      setStatus(e.message, true);
    } finally {
      els.runBtn.disabled = false;
    }
  }

  async function downloadSrt() {
    if (!lastRequest) {
      setStatus("请先执行重建", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/rebuild.srt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "下载失败" }));
        throw new Error(err.detail || "下载失败");
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "rebuilt.srt";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      els.downloadBtn.disabled = false;
    }
  }

  els.runBtn.addEventListener("click", run);
  els.downloadBtn.addEventListener("click", downloadSrt);
})();
