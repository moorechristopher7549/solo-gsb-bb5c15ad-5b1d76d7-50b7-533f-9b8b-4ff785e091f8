"use strict";

// 双语术语核对：粘贴每块恰好两行的双语 SRT 与「英文术语<TAB>指定中文译法」
// 词表，在英文轨拼接段内按词边界查找术语，核对对应中文轨是否使用指定译法，
// 预览合格/缺译结果并下载 UTF-8 CSV
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("tcSrt"),
    glossary: $("tcGlossary"),
    runBtn: $("tcRunBtn"),
    clearBtn: $("tcClearBtn"),
    status: $("tcStatus"),
    summary: $("tcSummary"),
    resultWrap: $("tcResultWrap"),
    body: $("tcBody"),
    downloadBtn: $("tcDownloadBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:03,000",
    "机器学习正在改变世界。",
    "Machine learning is changing the world.",
    "",
    "2",
    "00:00:03,200 --> 00:00:05,000",
    "神经网络是关键技术。",
    "The neural network is the key technology.",
    "",
    "3",
    "00:00:06,000 --> 00:00:08,000",
    "深度学习需要大量数据。",
    "Deep learning needs lots of data.",
    "",
  ].join("\n");

  const SAMPLE_GLOSSARY = [
    "machine learning\t机器学习",
    "neural network\t神经网络",
    "key technology\t核心技术",
    "deep learning\t深度学习",
  ].join("\n");

  $("tcSampleSrt").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
  });
  $("tcSampleGlossary").addEventListener("click", () => {
    els.glossary.value = SAMPLE_GLOSSARY;
  });

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
    els.glossary.value = "";
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
    ok: { label: "合格", badge: "badge-ok", row: "row-ok" },
    missing: { label: "缺译", badge: "badge-missing", row: "row-missing" },
  };

  function render(data) {
    const s = data.summary;
    $("tcCntOk").textContent = s.ok;
    $("tcCntMissing").textContent = s.missing;
    $("tcCntHits").textContent = s.hits;
    $("tcCntBlocks").textContent = s.total_blocks;
    $("tcCntChains").textContent = s.chains;
    $("tcCntTerms").textContent = s.terms;

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
        <td class="cell-text">${escapeHtml(r.en_line)}</td>
        <td class="cell-text">${escapeHtml(r.zh_line)}</td>
        <td class="cell-text">${escapeHtml(r.term)}</td>
        <td class="cell-text">${escapeHtml(r.expected_zh)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    lastRequest = { srt: els.srt.value, glossary: els.glossary.value };

    els.runBtn.disabled = true;
    setStatus("核对中……", false);
    try {
      const resp = await fetch("/api/termcheck", {
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
        `完成：命中 ${s.hits} 处，合格 ${s.ok} 项，缺译 ${s.missing} 项`,
        false
      );
    } catch (e) {
      // 词表空项、重复英文术语、非双行块等错误：清空旧结果
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
      setStatus("请先执行核对", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/termcheck.csv", {
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
      a.download = "termcheck.csv";
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
