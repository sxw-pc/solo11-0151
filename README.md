# 展台供电安全采录台

离线的展会供电证据工作台。展台负荷契约、共享相位容量、电表累计值与连续峰值、跳闸复位责任分别有独立入口和持久化状态。所有输入是虚构 fixture，不访问外网或真实展馆系统。

## 快速运行

在仓库根目录：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m boothpower demo --db demo.sqlite
python -m boothpower audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独采录四本账：

```powershell
python -m boothpower declaration-import --db state.sqlite --input fixtures/declaration_valid.json
python -m boothpower circuit-plan --db state.sqlite --input fixtures/circuit_valid.json
python -m boothpower meter-import --db state.sqlite --input fixtures/meter_valid.json
python -m boothpower safety-decide --db state.sqlite --input fixtures/safety_restore.json
python -m boothpower relay-import --db state.sqlite --input fixtures/relay-root-bench.json
python -m boothpower report --db state.sqlite
```

对应的 `declaration_changed.json`、`circuit_overbook.json`、`meter_overload.json`、`safety_unverified.json` 会产生增容待核、峰值候补、连续超载或未见证复位。先执行正常链再导入失败证据，可观察通电许可和候补状态的跨账变化；单独运行各本账也可看到自身决定。

| 账 | 独立结论 | 跨账影响 |
| --- | --- | --- |
| 展商负荷契约 | 签约资格、迟到增容和设备清单 | 批准增容后要求重排原容量分配 |
| 回路容量 | 同相位叠加峰值、错峰与原子修订；相位内不同申报上限取保守下界 | 无确认分配不能取得通电许可 |
| 现场电表 | 校准、累计值交接、三点连续超载；跨回路采样须同一时刻对齐 | 可信过载阻断通电与新分配 |
| 复位责任 | 独立跳闸见证、冷却、角色分离与正常复测 | 批准后候补才可晋升 |
| 继电器根源核证 | 双端掉闸简表不派单；按每轮单表偏移 [-2,2] 与 2-4 刻传播带枚举相容方向；共时采集片与发出/接收接点链两条独立取证路线 | 只离线采录，不发设备命令，不触碰通电、复位与回路分配 |

## 继电器根源核证

双端 U、V 同时掉闸的简表可能对应两种物理历史（U 本地 9 刻/V 11 刻，或 U 11 刻/V 9 刻），简表上的签认 `root_hint` 只作为声明保留，不能单独签维修单。核证只承认两类现场原件：

- **共时采集片（common-edge-recorder）**：两侧签认的边沿与物理接点、完整观测窗口、全程同一刻度，并对照本轮已双侧签认的布线位图（local-send 与 device-echo 线位）检查 2-4 刻传播序列；旧触点已清过时，窗口后的灭灯不能归因。
- **发出/接收接点链（drive-contact-custody）**：两侧签认把同一驱动的首发锁存与接收原件绑定，不比较两边时钟，共采片烧坏也有效；共享驱动的镜像不算另一次发起，局部损毁的发送接点不能签认。

每块表每轮只能用一个偏移；空白读数（可能出现在复归后）不否决任何方向，文件抵达先后也不作时序。两类原件若支持不同物理历史，案件停在 `evidence_conflict` 并指出冲突的接点或时间约束；双侧签认的撤证原件可让案件重裁，但旧方向工单只标记 `superseded`、永不抹除。任一时刻至多建议一次新的取证动作（布线签认、共时补采或接点封存）；若所有可能返回都不能区分方向，案件保持 `awaiting_source`，不向任何一方派单。拓扑限定为两个端点、一个中继、两个布线版本。

SQLite `evidence` 保留规范化事件 JSON 字节及 SHA-256；CLI 的原始文件字节和顺序另记 `journal`。事件同 ID 同内容重放幂等，异内容拒绝覆盖。`decision_log` 保留状态转移；`audit` 在全新内存库重放 CLI 源文件并比较各账当前状态（含继电器案件与工单）。只调用 Python 函数而没有 CLI 日志的数据库会明确返回缺少重放来源。

本版按单机 SQLite、离线展期和确定性相位容量运行。真实电表接入、外部断路器控制、事故通知和动态电价不在首版范围。
