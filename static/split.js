"use strict";

// 双语 SRT 拆轨：粘贴每块恰好两行的双语 SRT，并排预览中文轨/英文轨归属
// 与总错配分，分别下载保留原时间码、从 1 重编号的两轨 SRT
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("spSrt"),
    runBtn: $("spRunBtn"),
    clearBtn: $("spClearBtn"),
    status: $("spStatus"),
    summary: $("spSummary"),
    resultWrap: $("spResultWrap"),
    body: $("spBody"),
    downloadZhBtn: $("spDownloadZhBtn"),
    downloadEnBtn: $("spDownloadEnBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:03,000",
    "你好，世界！",
    "Hello, world!",
    "",
    "2",
    "00:00:03,500 --> 00:00:05,500",
    "今天我们来聊聊双语字幕。",
    "Today we talk about bilingual subtitles.",
    "",
    "3",
    "00:00:06,000 --> 00:00:08,000",
    "Splitting tracks is easy.",
    "拆轨其实很简单。",
    "",
    "4",
    "00:00:08,500 --> 00:00:10,000",
    "……",
    "……",
    "",
    "5",
    "00:00:10,500 --> 00:00:12,500",
    "再见！",
    "See you!",
    "",
  ].join("\n");

  $("spSample").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
  });

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
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

  const ORDER_META = {
    zh_top: { label: "中文在上", badge: "badge-zh-top", row: "row-zh-top" },
    en_top: { label: "英文在上", badge: "badge-en-top", row: "row-en-top" },
  };

  function mismatchCell(n) {
    const cls = n === 0 ? "cost-low" : n <= 2 ? "cost-mid" : "cost-high";
    return `<span class="${cls}">${n}</span>`;
  }

  function render(data) {
    const s = data.summary;
    $("spCnt").textContent = s.total;
    $("spMismatch").textContent = s.mismatch_chars;
    $("spSwitches").textContent = s.switches;
    $("spTotal").textContent = s.total_cost;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const meta = ORDER_META[r.order];
      const tr = document.createElement("tr");
      tr.className = meta.row;
      const switchFlag = r.switched
        ? ' <span class="switch-flag" title="与上一块轨序不同，另计 2 分">⇄+2</span>'
        : "";
      tr.innerHTML = `
        <td class="cell-idx">${r.new_index}</td>
        <td class="cell-idx">${r.index}</td>
        <td class="cell-tc">${escapeHtml(r.start_tc)} → ${escapeHtml(r.end_tc)}</td>
        <td><span class="badge ${meta.badge}">${meta.label}</span>${switchFlag}</td>
        <td class="cell-text">${escapeHtml(r.zh_line)}</td>
        <td class="cell-text">${escapeHtml(r.en_line)}</td>
        <td class="cell-num">${mismatchCell(r.mismatch)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    lastRequest = { srt: els.srt.value };

    els.runBtn.disabled = true;
    setStatus("拆轨中……", false);
    try {
      const resp = await fetch("/api/split", {
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
        `完成：${s.total} 个字幕块，错配字符 ${s.mismatch_chars} 个，` +
        `顺序变化 ${s.switches} 次，总错配分 ${s.total_cost}`,
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

  async function downloadSrt(track) {
    if (!lastRequest) {
      setStatus("请先执行拆轨", true);
      return;
    }
    const btn = track === "zh" ? els.downloadZhBtn : els.downloadEnBtn;
    btn.disabled = true;
    try {
      const resp = await fetch(`/api/split.${track}.srt`, {
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
      a.download = `split.${track}.srt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  els.runBtn.addEventListener("click", run);
  els.downloadZhBtn.addEventListener("click", () => downloadSrt("zh"));
  els.downloadEnBtn.addEventListener("click", () => downloadSrt("en"));
})();
