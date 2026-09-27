# 公共交通中断改道发布服务

一个仅使用 Python 标准库实现的线路、站点、班次、施工绕行和无障碍变化发布服务。方案按草稿、复核、批准、发布流转；路径计算会应用停运、跳站、绕行和无障碍限制。

## 运行

```bash
python app.py --init
python app.py --port 8010
```

打开 <http://127.0.0.1:8010>。`--init` 会导入两条示例线路、六个站点和一个 23:50 发车的跨日班次。数据库默认是 `transit_disruption.db`，可用 `--db` 或 `TRANSIT_DB` 修改。

## 业务能力

- 基础数据导入会一次性检查线路、站点经纬度、连续站序、重复站点、站间行驶时间和班次时间。错误批次写入 `import_errors` 后整体拒绝，不留下半批数据。
- 中断事件可以包含 `stop_closure`、`skip_stop`、`detour`、`accessibility_change`，可以设置服务日分钟窗口。
- 路径使用 Dijkstra 算法比较基线与方案版本；跳站时车辆可继续通过，但乘客不能在跳站上下车，经过省略路段的行驶时间会计入下一段。
- 班次时间以服务日零点起算，允许超过 1440 分钟。例如 1430 分发车、21 分钟到达会显示为次日 `00:21`。
- 修改只允许发生在草稿版本；创建新版本会复制父版本变更，已发布快照继续保留。
- 同一线路可能连续出现多处施工：保存草稿不做冲突检查；送审前把本版每项变更与**其他中断事件**中已批准、已发布的版本比较，线路相同、占用站点相同（绕行按起止两端计）且生效窗口重叠即判定冲突，返回冲突版本号和重叠分钟数（边界相接不算重叠，未填窗口按全天 `[0,2880)` 处理），阻止进入复核。
- 发布时在写快照前重新做同样的检查；审批期间相同站点时段已被另一版占用时，已过期的版本会被拒绝发布并保持在已批准状态。
- 发布在一个 SQLite 事务内写入方案快照和 SHA-256，旧发布版本不会被覆盖。

## API

使用 `X-User`、`X-Role` 身份头，角色包括 `planner`、`editor`、`reviewer`、`admin`。

- `POST /api/import`：导入基础数据。
- `POST /api/disruptions`：创建中断事件及第一版草稿。
- `POST /api/disruptions/{id}/versions`：从指定父版本复制出新草稿。
- `POST /api/versions/{id}/changes`：向草稿添加停运、跳站、绕行或无障碍变化。
- `POST /api/versions/{id}/submit|approve|reject|publish`：完成复核发布流程。submit/publish 命中施工冲突时返回 409 和 `conflicts` 明细（冲突版本号、站点、重叠分钟）。
- `GET /api/versions/{id}/conflicts`：送审前预检本版与已批准/已发布版本的施工冲突。
- `GET /api/route?from=1&to=5&version_id=1&at_minute=1430&accessible=true`：查询路径、耗时和到达时间。
- `GET /api/trips/{id}`：查看跨日班次各站时间。
- `GET /api/import-errors`：查看被隔离的错误批次。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖基线/改道路径、版本复制与发布隔离、审批冲突、无障碍路径、跨日时刻、坏数据整批隔离，以及同线路施工送审拦截、发布复查和无关方案放行。
