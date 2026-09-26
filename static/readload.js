"use strict";

// 字幕阅读负载审查：粘贴 SRT + 设置阅读速度（每秒显示宽度单位数），
// 在同一阅读时间轴上按最早出点优先模拟分配，按时间预览各块
// 所需/已分配时长与合格/逾期，汇总逾期数并下载 UTF-8 CSV
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("rdSrt"),
    speed: $("rdSpeed"),
    runBtn: $("rdRunBtn"),
    clearBtn: $("rdClearBtn"),
    status: $("rdStatus"),
    summary: $("rdSummary"),
    resultWrap: $("rdResultWrap"),
    body: $("rdBody"),
    downloadBtn: $("rdDownloadBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:04,000",
    "你好，世界！今天天气真不错。",
    "",
    "2",
    "00:00:04,200 --> 00:00:05,000",
    "这一块时间太短根本读不完。",
    "",
    "3",
    "00:00:05,000 --> 00:00:08,000",
    "This line is easy to read.",
    "",
    "4",
    "00:00:06,000 --> 00:00:07,500",
    "插入一块出点更早的",
    "",
  ].join("\n");

  $("rdSample").addEventListener("click", () => {
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

  function readSpeed() {
    const speed = parseInt(els.speed.value, 10);
    if (!Number.isInteger(speed) || speed < 4 || speed > 30) {
      throw new Error("阅读速度须为 4~30 之间的整数（显示宽度单位/秒）");
    }
    return speed;
  }

  const STATUS_META = {
    ok: { label: "合格", badge: "badge-ok", row: "row-ok" },
    overdue: { label: "逾期", badge: "badge-overdue", row: "row-overdue" },
  };

  function render(data) {
    const s = data.summary;
    $("rdCntOk").textContent = s.ok;
    $("rdCntOverdue").textContent = s.overdue;
    $("rdCnt").textContent = s.total;
    $("rdSpeedShown").textContent = s.speed;
    $("rdUnallocated").textContent = s.unallocated_ms;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const meta = STATUS_META[r.status];
      const tr = document.createElement("tr");
      tr.className = meta.row;
      tr.innerHTML = `
        <td><span class="badge ${meta.badge}">${meta.label}</span></td>
        <td class="cell-idx">${r.block_pos}</td>
        <td class="cell-idx">${r.index}</td>
        <td class="cell-tc">${escapeHtml(r.start_tc)} → ${escapeHtml(r.end_tc)}</td>
        <td class="cell-num">${r.required_ms}</td>
        <td class="cell-num">${r.allocated_ms}</td>
        <td class="cell-num">${r.unallocated_ms}</td>
        <td class="cell-text">${escapeHtml(r.text)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    let speed;
    try {
      speed = readSpeed();
    } catch (e) {
      setStatus(e.message, true);
      return;
    }
    lastRequest = { srt: els.srt.value, speed };

    els.runBtn.disabled = true;
    setStatus("审查中……", false);
    try {
      const resp = await fetch("/api/readload", {
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
        s.overdue
          ? `完成：${s.total} 个字幕块，逾期 ${s.overdue} 块，未分配合计 ${s.unallocated_ms} ms`
          : `完成：${s.total} 个字幕块均可在自身区间内读完`,
        false
      );
    } catch (e) {
      // 入点不早于出点（指出块号）等错误：清空旧结果
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
      setStatus("请先执行审查", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/readload.csv", {
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
      a.download = "readload.csv";
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
