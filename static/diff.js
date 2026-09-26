"use strict";

// SRT 版本差异审校：粘贴旧版/新版 SRT，按块序做全局最小代价对齐
// （1:1 / 1:2 / 2:1 / 单侧缺项），标出未变/仅改时/文字改动/拆分/合并/新增/删除，
// 并排显示两侧时间码与正文，可下载 UTF-8 CSV
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    oldSrt: $("dfOld"),
    newSrt: $("dfNew"),
    runBtn: $("dfRunBtn"),
    clearBtn: $("dfClearBtn"),
    status: $("dfStatus"),
    summary: $("dfSummary"),
    resultWrap: $("dfResultWrap"),
    body: $("dfBody"),
    downloadBtn: $("dfDownloadBtn"),
  };

  const SAMPLE_OLD = [
    "1",
    "00:00:01,000 --> 00:00:02,500",
    "你好，世界！",
    "",
    "2",
    "00:00:03,000 --> 00:00:04,500",
    "今天我们来聊聊字幕校对。",
    "",
    "3",
    "00:00:05,000 --> 00:00:06,000",
    "这句稍后会被删掉。",
    "",
    "4",
    "00:00:06,500 --> 00:00:08,000",
    "这一句在新版里被拆成了两句短句。",
    "",
    "5",
    "00:00:08,500 --> 00:00:09,000",
    "前一半。",
    "",
    "6",
    "00:00:09,000 --> 00:00:09,500",
    "后一半。",
    "",
    "7",
    "00:00:10,000 --> 00:00:11,000",
    "再见！",
    "",
  ].join("\n");

  const SAMPLE_NEW = [
    "1",
    "00:00:01,000 --> 00:00:02,500",
    "你好，世界！",
    "",
    "2",
    "00:00:03,200 --> 00:00:04,700",
    "今天我们来聊聊字幕校对。",
    "",
    "3",
    "00:00:04,800 --> 00:00:05,200",
    "This line is brand new.",
    "",
    "4",
    "00:00:06,500 --> 00:00:07,000",
    "这一句在新版里被拆开了。",
    "",
    "5",
    "00:00:07,000 --> 00:00:08,000",
    "拆成了两句短句。",
    "",
    "6",
    "00:00:08,500 --> 00:00:09,500",
    "前一半。后一半。",
    "",
    "7",
    "00:00:10,000 --> 00:00:11,000",
    "再会！",
    "",
  ].join("\n");

  $("dfSampleOld").addEventListener("click", () => {
    els.oldSrt.value = SAMPLE_OLD;
  });
  $("dfSampleNew").addEventListener("click", () => {
    els.newSrt.value = SAMPLE_NEW;
  });

  els.clearBtn.addEventListener("click", () => {
    els.oldSrt.value = "";
    els.newSrt.value = "";
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

  const STATUS_META = {
    unchanged: { label: "未变", badge: "badge-unchanged", row: "row-unchanged" },
    time_only: { label: "仅改时", badge: "badge-time-only", row: "row-time-only" },
    text_changed: { label: "文字改动", badge: "badge-text-changed", row: "row-text-changed" },
    split: { label: "拆分", badge: "badge-split", row: "row-split" },
    merge: { label: "合并", badge: "badge-merge", row: "row-merge" },
    added: { label: "新增", badge: "badge-added", row: "row-added" },
    deleted: { label: "删除", badge: "badge-deleted", row: "row-deleted" },
  };

  function costCell(r) {
    if (r.status === "added" || r.status === "deleted") {
      return `<span class="dim">(${r.cost.toFixed(2)})</span>`;
    }
    const cls = r.cost >= 0.3 ? "cost-high" : r.cost >= 0.1 ? "cost-mid" : "cost-low";
    return `<span class="${cls}">${r.cost.toFixed(3)}</span>`;
  }

  function render(data) {
    const s = data.summary;
    $("dfCntUnchanged").textContent = s.unchanged;
    $("dfCntTimeOnly").textContent = s.time_only;
    $("dfCntTextChanged").textContent = s.text_changed;
    $("dfCntSplit").textContent = s.split;
    $("dfCntMerge").textContent = s.merge;
    $("dfCntAdded").textContent = s.added;
    $("dfCntDeleted").textContent = s.deleted;
    $("dfCntTotal").textContent = s.total_rows;
    $("dfTotalCost").textContent = s.total_cost.toFixed(4);

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const meta = STATUS_META[r.status];
      const tr = document.createElement("tr");
      tr.className = meta.row;
      const oldTc = r.old_start_tc
        ? `${escapeHtml(r.old_start_tc)} → ${escapeHtml(r.old_end_tc)}`
        : "";
      const newTc = r.new_start_tc
        ? `${escapeHtml(r.new_start_tc)} → ${escapeHtml(r.new_end_tc)}`
        : "";
      const dev = r.time_dev_ms === null ? "" : r.time_dev_ms;
      tr.innerHTML = `
        <td><span class="badge ${meta.badge}">${meta.label}</span></td>
        <td class="cell-num">${costCell(r)}</td>
        <td class="cell-idx cell-multiline">${r.old_index.join("<br>")}</td>
        <td class="cell-tc">${oldTc}</td>
        <td class="cell-text cell-multiline">${escapeHtml(r.old_text)}</td>
        <td class="cell-idx cell-multiline">${r.new_index.join("<br>")}</td>
        <td class="cell-tc">${newTc}</td>
        <td class="cell-text cell-multiline">${escapeHtml(r.new_text)}</td>
        <td class="cell-num">${dev}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    lastRequest = { old_srt: els.oldSrt.value, new_srt: els.newSrt.value };

    els.runBtn.disabled = true;
    setStatus("比较中……", false);
    try {
      const resp = await fetch("/api/diff", {
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
        `完成：${s.total_rows} 行，未变 ${s.unchanged}、新增 ${s.added}、` +
        `删除 ${s.deleted}、拆分 ${s.split}、合并 ${s.merge}`,
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

  async function downloadCsv() {
    if (!lastRequest) {
      setStatus("请先执行比较", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/diff.csv", {
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
      a.download = "diff.csv";
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
  els.downloadBtn.addEventListener("click", downloadCsv);
})();
