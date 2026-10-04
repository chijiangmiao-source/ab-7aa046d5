# 飞控共享总线锁 · 优先级继承审计

面向值班员复核「捕获轨迹中有效优先级与锁移交是否一致」的 Web 审计系统。
后端按 **数值越小越紧急** 处理不可重入互斥锁，对任意嵌套深度的等待链做
优先级继承；页面真实调用 API，展示两跳继承、释放后优先级回落与并列移交。

## 规则模型

- 任务 1..16 个、锁 1..32 把、事件至多 128 项；优先级为整数，越小越紧急。
- 不可重入互斥锁：已持锁不能再次 acquire；每把锁至多一个拥有者。
- **嵌套继承**：等待边 `waiter → (等待锁) → owner`，拥有者的有效优先级是
  自身基准优先级与所有能经等待图到达它的任务的有效优先级中的最小值（最紧急）。
- **释放 / cancel 后从当前等待图重算**；释放的锁移交给
  「有效优先级最高（数值最小），并列时任务标识最小」的等待者。
- 非法事件会定位事件序号并**整体回滚**（裁决 `rejected`、`steps` 为空）：
  非拥有者释放、取消运行任务、重复等待同一锁、任务同时等待两把锁、
  不可重入 acquire、制造等待环（含间接环）、引用不存在的任务/锁。
- cancel 只允许作用于**阻塞中**的任务；若该任务此前还嵌套持有其他锁，
  这些锁同样按最紧急等待者移交。

## 冻结裁决与重放

- 提交 `POST /api/audits`：以稳定审计标识（auditId）冻结裁决。
- 相同 auditId + 相同输入重传：现场重放并与冻结结论逐字节比对（`replayed=true`）。
- 相同 auditId + 不同内容：返回 `409 conflict`，冻结裁决不被覆盖。
- `GET /api/audits/<auditId>`：随时重新读取冻结裁决。
- 非法事件的裁决同样会冻结，重传得到一致的 `422 rejected`。

## 本地运行（无 Docker）

```bash
cd app
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python app.py                 # 默认 8080，可用 PORT 覆盖
```

打开 http://localhost:8080 ，点击「载入：两跳继承 / 释放回落 / 并列移交」
三个预置轨迹后提交即可看到每步运行 / 等待 / 持锁快照。

## Docker / Compose

宿主端口可配（默认 8080）：

```bash
HOST_PORT=9090 docker compose up -d --build app
curl -s http://localhost:9090/api/health
```

verify 容器针对预置轨迹运行三阶段检查后退出，并以退出码报告：

```bash
docker compose up --build verify
docker inspect fc-lock-audit-verify-1 --format '{{.State.ExitCode}}'   # 0 = 全部通过
```

1. 构建/静态检查：`py_compile`、页面资源存在性
2. 规则测试：`pytest tests/`（26 项：两跳/三跳继承、回落、并列移交、
   cancel 链上移交、等待环、各类非法事件定位与回滚、输入上限）
3. API/HTTP 冒烟：对运行中的 `app` 真实发起 health/页面/提交/重放/重读/
   冲突/非法定位 请求

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/audits` | 提交并冻结裁决（200 接受 / 422 非法事件 / 409 冲突 / 400 输入错误） |
| GET | `/api/audits/<auditId>` | 读取冻结裁决（404 不存在） |
| GET | `/api/health` | 健康响应 |

请求体示例：

```json
{
  "auditId": "FC-TRAJ-001",
  "tasks": [{"id": "T1", "priority": 1}, {"id": "T3", "priority": 10}],
  "locks": [{"id": "L1"}],
  "events": [
    {"type": "acquire", "task": "T3", "lock": "L1"},
    {"type": "acquire", "task": "T1", "lock": "L1"}
  ]
}
```

每步快照含 `running / waiting / holding / locks / effectivePriorities`。
