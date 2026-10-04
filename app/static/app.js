"use strict";

const $ = (sel) => document.querySelector(sel);
const taskBody = $("#taskTable tbody");
const lockBody = $("#lockTable tbody");
const eventBody = $("#eventTable tbody");

// --------------------------------------------------------------------- rows
function addRow(body, cells) {
  const tr = document.createElement("tr");
  cells.forEach((c) => {
    const td = document.createElement("td");
    td.appendChild(c);
    tr.appendChild(td);
  });
  body.appendChild(tr);
  return tr;
}

function textInput(value, ph) {
  const i = document.createElement("input");
  i.type = "text";
  i.value = value || "";
  if (ph) i.placeholder = ph;
  return i;
}
function numInput(value) {
  const i = document.createElement("input");
  i.type = "number";
  i.value = value ?? "";
  return i;
}
function delBtn() {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "mini danger";
  b.textContent = "删除";
  b.onclick = () => b.closest("tr").remove();
  return b;
}

function addTask(id = "", pri = "") {
  addRow(taskBody, [textInput(id, "任务标识"), numInput(pri), delBtn()]);
}
function addLock(id = "") {
  addRow(lockBody, [textInput(id, "锁标识"), delBtn()]);
}

function addEvent(type = "acquire", task = "", target = "") {
  const typeSel = document.createElement("select");
  ["acquire", "release", "set-priority", "cancel"].forEach((t) => {
    const o = document.createElement("option");
    o.value = t;
    o.textContent = t;
    typeSel.appendChild(o);
  });
  typeSel.value = type;

  const taskInp = textInput(task, "任务标识");
  const targetInp =
    type === "set-priority" ? numInput(target) : textInput(target, "锁标识");

  const refreshTarget = () => {
    const td = targetInp.closest("td");
    const ni =
      typeSel.value === "set-priority"
        ? numInput(targetInp.value)
        : textInput(targetInp.value, "锁标识");
    td.replaceChildren(ni);
    ni.dataset.role = "target";
  };
  targetInp.dataset.role = "target";
  typeSel.onchange = refreshTarget;

  const idx = document.createElement("span");
  idx.className = "event-idx";
  addRow(eventBody, [idx, typeSel, taskInp, targetInp, delBtn()]);
  renumberEvents();
}

function renumberEvents() {
  [...eventBody.rows].forEach((r, i) => {
    r.cells[0].textContent = String(i + 1);
  });
}

$("#addTask").onclick = () => addTask();
$("#addLock").onclick = () => addLock();
$("#addEvent").onclick = () => addEvent();

// ------------------------------------------------------------------ payload
function collect() {
  const tasks = [...taskBody.rows].map((r) => ({
    id: r.cells[0].querySelector("input").value.trim(),
    priority: parseInt(r.cells[1].querySelector("input").value, 10),
  }));
  const locks = [...lockBody.rows].map((r) => ({
    id: r.cells[0].querySelector("input").value.trim(),
  }));
  const events = [...eventBody.rows].map((r) => {
    const type = r.cells[1].querySelector("select").value;
    const task = r.cells[2].querySelector("input").value.trim();
    const targetVal = r.cells[3].querySelector("input").value;
    const ev = { type, task };
    if (type === "set-priority") ev.priority = parseInt(targetVal, 10);
    else ev.lock = targetVal.trim();
    return ev;
  });
  return { auditId: $("#auditId").value.trim(), tasks, locks, events };
}

// -------------------------------------------------------------------- render
function chip(text, waiting) {
  const c = document.createElement("span");
  c.className = "chip" + (waiting ? " waiting" : "");
  c.innerHTML = text;
  return c;
}

function taskChip(t) {
  return chip(
    `<b>${t.task}</b> ` +
      `<span class="base">基准 ${t.basePriority}</span> / ` +
      `<span class="eff">有效 ${t.effectivePriority}</span>`,
    false
  );
}

function renderVerdict(v) {
  $("#result").hidden = false;
  const meta = $("#verdictMeta");
  meta.innerHTML = "";

  const st = $("#status");
  st.className = "status " + (v.status === "ok" ? "ok" : v.status || "");
  const stText = {
    ok: "✓ 接受：裁决已冻结",
    rejected: "✗ 拒绝：事件非法，旧成功已清除",
    conflict: "⚠ 冲突：审计标识已被不同内容占用",
    missing: "？ 未找到冻结裁决",
  }[v.status] || v.status;
  st.textContent = stText;

  $("#errorbox").hidden = v.status !== "rejected";
  if (v.status === "rejected") {
    $("#errorbox").textContent =
      `第 ${v.failedEvent} 个事件：${v.error}`;
  }

  const lines = [];
  lines.push(`<div class="meta-line">状态：<b>${v.status}</b></div>`);
  if (typeof v.frozen === "boolean")
    lines.push(
      `<div class="meta-line">冻结裁决：<span class="${
        v.frozen ? "badge-true" : "badge-false"
      }">${v.frozen ? "是（重传/重读）" : "否（首次提交）"}</span></div>`
    );
  if (typeof v.replayed === "boolean")
    lines.push(
      `<div class="meta-line">重传回放一致：<span class="${
        v.replayed ? "badge-true" : "badge-false"
      }">${v.replayed ? "是" : "否"}</span></div>`
    );
  if (v.error && v.status !== "rejected")
    lines.push(`<div class="meta-line">${v.error}</div>`);
  meta.innerHTML = lines.join("");

  const wrap = $("#steps");
  wrap.innerHTML = "";
  (v.steps || []).forEach((s, i) => {
    const det = document.createElement("details");
    det.className = "step";
    if (i === v.steps.length - 1 || i === 0) det.open = i === v.steps.length - 1;

    const sum = document.createElement("summary");
    const label = s.index === null ? "初始" : `第 ${s.index + 1} 步`;
    sum.innerHTML =
      `<span>${label}</span>` +
      `<span class="tag ${s.type || "initial"}">${s.type || "initial"}</span>` +
      `<span class="note">${s.note}</span>`;
    det.appendChild(sum);

    const body = document.createElement("div");
    body.className = "stepBody";

    const colRun = document.createElement("div");
    colRun.innerHTML = "<h3>运行（含被继承后的有效优先级）</h3>";
    s.running.forEach((t) => colRun.appendChild(taskChip(t)));
    if (!s.running.length) colRun.innerHTML += "<p class='note'>（无）</p>";

    const colWait = document.createElement("div");
    colWait.innerHTML = "<h3>等待</h3>";
    s.waiting.forEach((w) => {
      colWait.appendChild(
        chip(
          `<b>${w.task}</b> 等待 <b>${w.lock}</b>` +
            `（持有者 ${w.owner}）` +
            ` <span class="base">基准 ${w.basePriority}</span> / ` +
            `<span class="eff">有效 ${w.effectivePriority}</span>`,
          true
        )
      );
    });
    if (!s.waiting.length) colWait.innerHTML += "<p class='note'>（无）</p>";

    const colHold = document.createElement("div");
    colHold.innerHTML = "<h3>持锁</h3>";
    s.locks.forEach((lk) => {
      const d = document.createElement("div");
      d.className = "lockblock";
      const ownerTxt = lk.owner
        ? `<span class="owner">${lk.owner}</span>`
        : `<span class="free">空闲</span>`;
      let html = `锁 <b>${lk.lock}</b> → ${ownerTxt}`;
      if (lk.waiters.length) {
        html +=
          ` <span class="waiters">等待: ` +
          lk.waiters
            .map(
              (w) =>
                `${w.task}(有效${w.effectivePriority}/基准${w.basePriority})`
            )
            .join(", ") +
          `</span>`;
      }
      d.innerHTML = html;
      colHold.appendChild(d);
    });

    body.append(colRun, colWait, colHold);
    det.appendChild(body);
    wrap.appendChild(det);
  });
}

// ------------------------------------------------------------------ actions
async function submitAudit() {
  const payload = collect();
  const st = $("#status");
  st.className = "status";
  st.textContent = "提交中…";
  try {
    const resp = await fetch("/api/audits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderVerdict(await resp.json());
  } catch (e) {
    st.className = "status rejected";
    st.textContent = "请求失败: " + e;
  }
}

async function reread() {
  const id = $("#auditId").value.trim();
  if (!id) {
    $("#status").textContent = "请先填写审计标识";
    return;
  }
  const resp = await fetch("/api/audits/" + encodeURIComponent(id));
  renderVerdict(await resp.json());
}

$("#submit").onclick = submitAudit;
$("#reread").onclick = reread;

// ------------------------------------------------------------------ presets
const PRESETS = {
  // T1(1,紧急) -> 等待 L1(持有者 T3) -> T3 等待 L2(持有者 T2)：
  // 紧急度沿两跳传递，T2/T3 有效优先级都被提升到 1。
  twohop: {
    auditId: "FC-TWOHOP-001",
    tasks: [
      { id: "T1", priority: 1 },
      { id: "T2", priority: 5 },
      { id: "T3", priority: 10 },
    ],
    locks: [{ id: "L1" }, { id: "L2" }],
    events: [
      { type: "acquire", task: "T3", lock: "L1" },
      { type: "acquire", task: "T2", lock: "L2" },
      { type: "acquire", task: "T3", lock: "L2" },
      { type: "acquire", task: "T1", lock: "L1" },
    ],
  },
  // 紧急任务阻塞低优先级持有者 -> 持有者有效优先级被抬升；
  // 释放并移交后，持有者有效优先级回落到自身基准值。
  rollback: {
    auditId: "FC-ROLLBACK-001",
    tasks: [
      { id: "A", priority: 8 },
      { id: "U", priority: 2 },
    ],
    locks: [{ id: "M" }],
    events: [
      { type: "acquire", task: "A", lock: "M" },
      { type: "acquire", task: "U", lock: "M" },
      { type: "release", task: "A", lock: "M" },
      { type: "release", task: "U", lock: "M" },
    ],
  },
  // 两个有效优先级并列的等待者，锁移交给任务标识最小者。
  tie: {
    auditId: "FC-TIE-001",
    tasks: [
      { id: "O", priority: 9 },
      { id: "W1", priority: 4 },
      { id: "W2", priority: 4 },
    ],
    locks: [{ id: "K" }],
    events: [
      { type: "acquire", task: "O", lock: "K" },
      { type: "acquire", task: "W1", lock: "K" },
      { type: "acquire", task: "W2", lock: "K" },
      { type: "release", task: "O", lock: "K" },
    ],
  },
};

function loadPreset(name) {
  const p = PRESETS[name];
  $("#auditId").value = p.auditId;
  taskBody.innerHTML = "";
  lockBody.innerHTML = "";
  eventBody.innerHTML = "";
  p.tasks.forEach((t) => addTask(t.id, t.priority));
  p.locks.forEach((l) => addLock(l.id));
  p.events.forEach((e) => {
    if (e.type === "set-priority") addEvent(e.type, e.task, e.priority);
    else addEvent(e.type, e.task, e.lock);
  });
  $("#result").hidden = true;
  $("#status").textContent = "已载入轨迹，点击提交审计";
  $("#status").className = "status";
}
document.querySelectorAll("[data-preset]").forEach((b) => {
  b.onclick = () => loadPreset(b.dataset.preset);
});

// init with the two-hop trajectory
loadPreset("twohop");
