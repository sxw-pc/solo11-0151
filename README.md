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
python -m boothpower report --db state.sqlite
```

对应的 `declaration_changed.json`、`circuit_overbook.json`、`meter_overload.json`、`safety_unverified.json` 会产生增容待核、峰值候补、连续超载或未见证复位。先执行正常链再导入失败证据，可观察通电许可和候补状态的跨账变化；单独运行各本账也可看到自身决定。

## 量台复验与量表证书

`bench-import` 保留历史单点结论（标桩 `nominal/display=3/3` 为 `calibrated`，`3/4` 为 `held`，重放幂等）。范围证书走新增入口，按同一开封轮次固定原件：

```powershell
python -m boothpower bench-certify --db state.sqlite --input fixtures/bench_certify_initial.json
python -m boothpower bench-plan --db state.sqlite --input fixtures/bench_certify_initial.json
```

仪器在一个轮次内只能是一条固定曲线 `display=clamp(floor(slope*load+offset),0,9)`，不允许低段、高段各选一组参数。证书只对每条仍相容曲线都成立的显示值签发纠正值（默认阈值 0.5）；饱和显示 9 在真值 5..8 间无法定值时作为反例和未覆盖区段保留，不从归档读数做线性插值。起始原件（负载 2、显示 3）下恰有三条相容曲线（斜率 1/1.5/2，零点 1/0/-1），可签显示 0、2、3、4、8，显示 9 保持 `partial`。

后续标定只消耗一次工位时段，规划器从现场可见的台架标准负载和现场阶跃中选期望覆盖最大的动作，并对该负载下每种可能显示给出可履约结论，不假定最顺利读数；台架负载 5 读到显示 6 可限定斜率 1、零点 1 并对显示 9 签负载 8；现场阶跃到真实 4、显示 6 只支持斜率 1.5、零点 0，可签低端、高端保留。无完整作证动作时返回工位证据缺口（如所有曲线在该负载同显示，不能当作覆盖高端）。现场联测必须绑定两份原始序列与一个共享激励，参照表偏差仅留存、不作绝对真值；重新开封后旧轮次原件不得支撑新一轮。

| 账 | 独立结论 | 跨账影响 |
| --- | --- | --- |
| 展商负荷契约 | 签约资格、迟到增容和设备清单 | 批准增容后要求重排原容量分配 |
| 回路容量 | 同相位叠加峰值、错峰与原子修订；相位内不同申报上限取保守下界 | 无确认分配不能取得通电许可 |
| 现场电表 | 校准、累计值交接、三点连续超载；跨回路采样须同一时刻对齐 | 可信过载阻断通电与新分配 |
| 复位责任 | 独立跳闸见证、冷却、角色分离与正常复测 | 批准后候补才可晋升 |

SQLite `evidence` 保留规范化事件 JSON 字节及 SHA-256；CLI 的原始文件字节和顺序另记 `journal`。事件同 ID 同内容重放幂等，异内容拒绝覆盖。`decision_log` 保留状态转移；`audit` 在全新内存库重放 CLI 源文件并比较四账当前状态。只调用 Python 函数而没有 CLI 日志的数据库会明确返回缺少重放来源。

本版按单机 SQLite、离线展期和确定性相位容量运行。真实电表接入、外部断路器控制、事故通知和动态电价不在首版范围。
