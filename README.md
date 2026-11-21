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

| 账 | 独立结论 | 跨账影响 |
| --- | --- | --- |
| 展商负荷契约 | 签约资格、迟到增容和设备清单 | 批准增容后要求重排原容量分配 |
| 回路容量 | 同相位叠加峰值、错峰与原子修订；相位内不同申报上限取保守下界 | 无确认分配不能取得通电许可 |
| 现场电表 | 校准、累计值交接、三点连续超载；跨回路采样须同一时刻对齐 | 可信过载阻断通电与新分配 |
| 复位责任 | 独立跳闸见证、冷却、角色分离与正常复测 | 批准后候补才可晋升 |

## 换位采录（本地模拟）

两个满载回路互换时，逐条提交替代申请只会双双候补、旧确认不释放。`fixtures/exchange-plan.json` 是可本地复跑的换位清单：登记 `exchanges` 后，模拟器以签核负荷、旧确认分配、相位版本和独立复位状态为起点，逐步保证每个展台恰有一份可用分配、每个相位合计不超过有效容量。两类可获签操作：

- 独立移位：有第三个合法暂留回路时，先把一张确认分配移到暂留位再逐步完成互换（样例最少三次）；每次导入只推进一步，可逐次观察已实证分配、未执行动作和许可。
- 成组移位：现场 `group_permits` 许可（如样例 `exclusive_mock_group`）且同一张设备原件回执完整覆盖全部成员时，整组从旧版同时切到新版；只有部分回执、或两张时间相同的单独回执，都不按同时生效处理。

搜不到合法中间态时返回形成循环的实际占用和缺少的凭据，不只签最终表；容量改签后从已见证的当前位置重算剩余路径，已到暂留位的展台不一键回到旧表。全程只用 SQLite、合成容量单位和模拟设备回执：

```powershell
python -m boothpower exchange-run --db exchange.sqlite --input fixtures/exchange-plan.json
```

输出每个换位的状态路径或阻碍环、动作原件（`exchange_action` 与设备回执）、旧分配和许可的版本归属；`report` 的 `exchange_status` 同步展示。

SQLite `evidence` 保留规范化事件 JSON 字节及 SHA-256；CLI 的原始文件字节和顺序另记 `journal`。事件同 ID 同内容重放幂等，异内容拒绝覆盖。`decision_log` 保留状态转移；`audit` 在全新内存库重放 CLI 源文件并比较四账当前状态。只调用 Python 函数而没有 CLI 日志的数据库会明确返回缺少重放来源。

本版按单机 SQLite、离线展期和确定性相位容量运行。真实电表接入、外部断路器控制、事故通知和动态电价不在首版范围。
