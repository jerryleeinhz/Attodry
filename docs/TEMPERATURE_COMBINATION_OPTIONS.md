# 温度组合扫描配置选项

适用入口：`python -m attodry_control.combination_cli`，例如
`temperature → optical → lockin`。说明对应2026-10-20当前代码。
独立温控命令的差异见本文末尾，不要混用不同入口的恢复规则。

## 最常用的两个选择

| 配置位置 | 字段 | 所有可选值 | 省略时 |
| --- | --- | --- | --- |
| `[temperature_stability]` | `acceptance_mode` | `"target"`、`"stable-readback"` | `"target"`；此时仍必须提供 `tolerance_k` |
| `[combination_scan]` | `temperature_deviation_policy` | `"abort"`、`"record_continue"` | `"abort"` |

**`temperature_deviation_policy`可以不写，其他配置完整合法时仍能运行。**
但省略表示严格处理，采集期间普通温漂超限会中断。要使用“记温度警告并继续”，
必须明确写 `temperature_deviation_policy = "record_continue"`。

这两个字段控制不同阶段：`acceptance_mode`决定新温度点何时可以进入采集；
`temperature_deviation_policy`决定判稳通过后的温度质量偏移如何处理。
当前没有 `ignore`、`off`、`retry`或自动重新判稳选项。
拼写、大小写以及 `stable-readback`中的连字符、`record_continue`中的下划线须保持一致。

## 1. 入场判稳：acceptance_mode

| 模式 | 距目标的要求 | 连续窗口要求 | 实际含义 |
| --- | --- | --- | --- |
| `"target"` | 窗口内每个温度读数都满足 `abs(T − target) <= tolerance_k` | 满足完整 `stable_dwell_s`，且峰峰差不超过 `stable_range_k` | 到达目标附近并保持稳定 |
| `"stable-readback"` | 不要求命中 `tolerance_k`；该值即使保留，也不作为此模式的目标到达判据 | 同样要求完整窗口和峰峰差；目标改变时还可要求实际响应 | 接受稳定的实际温度平台，可能明显偏离目标 |

`target`是模式字符串；`target_k`是数值设定点字段，两者不是同一项。

例如目标4.2 K，实际稳定在3.5 K：

- `target`配 `tolerance_k = 0.10`：不能通过。
- `stable-readback`：若平台窗口和其他守卫通过，可以按约3.5 K采集；
  数据不能称作实际4.2 K。

两种模式都要确认温控开启、目标寄存器正确、状态正常和硬边界安全；
`stable-readback`不会跳过这些检查。

### [temperature_stability] 字段表

| 字段 | 单位 | 必填／默认 | 作用 |
| --- | --- | --- | --- |
| `acceptance_mode` | 字符串 | 可省略，默认 `"target"` | 选择上表的入场判稳模式 |
| `tolerance_k` | K | `target`必填，必须大于0；`stable-readback`可省略 | 与目标的允许绝对误差，不是峰峰差 |
| `stable_range_k` | K | 必填，非负 | 窗口峰峰差 `max(T) − min(T)`；也用于判稳后的基线漂移及正式窗范围质量检查 |
| `stable_dwell_s` | 秒 | 必填，大于0 | 连续判稳窗口时长，不是固定等这么久就自动通过 |
| `poll_interval_s` | 秒 | 必填，大于0 | 判稳轮询间隔；实际采样间隔还包含设备通信耗时 |
| `wait_timeout_s` | 秒 | 必填，至少覆盖 `stable_dwell_s` | 此次等待判稳的总超时；超时仍失败 |
| `min_response_k` | K | 可省略，默认0 | 组合扫描中仅用于 `stable-readback`且目标发生改变的后续温度入口：要求相对该点开始的实际温度有足够响应，避免重复接受旧平台 |

例：`tolerance_k = 0.10`与`stable_range_k = 0.05`意味着窗口内读数既要在目标
±0.10 K内，又要彼此相差不超过0.05 K。将range改为2 K不会自动取消±0.10 K条件。

## 2. 判稳后的处理：temperature_deviation_policy

位置必须是 `[combination_scan]`，不能放在 `[temperature_stability]`。

| 情况 | `"abort"`（含省略） | `"record_continue"` |
| --- | --- | --- |
| 相对已合格基线的偏移超过 `stable_range_k` | 中断 | 保存温度质量警告并继续 |
| `target`模式下，实际温度距目标超过 `tolerance_k` | 中断 | 保存温度质量警告并继续 |
| 正式采样窗口温度峰峰差超过 `stable_range_k` | 中断 | 保存温度质量警告并继续 |
| 初次入场一直未判稳，直到 `wait_timeout_s` | 失败 | 同样失败 |
| 通信失败、非有限温度、温控意外关闭或设定被改 | 停止并清理 | 同样停止并清理 |
| 温度硬范围、过冲、磁场或其他仪器保护触发 | 停止并清理 | 同样停止并清理 |

`record_continue`保留实际读数和警告，不自动改设定、不重新判稳、不补采、不重启。
它没有另设一套更大的数值容差；原阈值继续产生警告，只改变超出这些温度质量阈值后的动作。
它也不使光功率、锁相、激光、身份、审计或清理故障变成可忽略警告。

非默认值 `record_continue`要求 `order`中选中了 `"temperature"`，否则离线解析拒绝。

### 四种组合的区别

| 入场模式 | 采集中策略 | 行为 |
| --- | --- | --- |
| `target` | `abort` | 接近目标且稳定后采集；后续温度质量超标中断 |
| `target` | `record_continue` | 接近目标且稳定后采集；后续普通温漂留警告并继续 |
| `stable-readback` | `abort` | 稳定平台后采集；后续基线漂移／正式窗范围超标中断 |
| `stable-readback` | `record_continue` | 稳定平台后采集；后续普通温漂留警告并继续 |

## 3. 配置示例

以下是合并到现有完整TOML的**片段**，不能单独运行。已有同名表时修改原表，
不要再创建第二个同名表；保留其他光源、功率计、锁相、安全、接线和数据库配置。

最近使用的“严格入场，采集中记温漂继续”配置：

```toml
[combination_scan]
order = ["temperature", "optical", "lockin"]
temperature_deviation_policy = "record_continue"
# 保留原表的backend、samples_per_condition、repeats、run_name、note等其他字段。

[temperature_stability]
acceptance_mode = "target"
tolerance_k = 0.10
stable_range_k = 0.05
stable_dwell_s = 120.0
poll_interval_s = 1.5
wait_timeout_s = 3600.0
```

如果改用稳定实际读回平台，替换上面的判稳表，例如：

```toml
[temperature_stability]
acceptance_mode = "stable-readback"
stable_range_k = 0.05
stable_dwell_s = 120.0
poll_interval_s = 1.5
wait_timeout_s = 3600.0
min_response_k = 0.02
```

这里的0.02 K是示例响应门槛，不是软件默认值，也不是温度传感器精度。
省略 `tolerance_k`后再把模式改回 `target`，必须补回该字段。

## 4. 温度网格：[temperature_scan]

组合扫描选中温度轴时：有此表就使用其中网格；没有此表则使用
`[temperature_run].target_k`作为单一温度点。

```toml
[temperature_scan]
temperature_ranges = [
  { min = 1.7, max = 251.7, scale = "linear", points = 101 },
]
run_name = "temperature_1p7_to_251p7K"
note = "Temperature grid; use measured temperature for analysis."
output_directory = "../run_data/temperature"
```

| 写法 | 支持值／约束 |
| --- | --- |
| `temperature_ranges` | 一个或多个区间；端点包含在内，展开后必须升序、不能重叠或共享端点 |
| `scale = "linear"` | 每段在 `points`与`step`中选一个；温度及step的单位为K |
| `scale = "log"` | 每段使用 `points`；温度必须为正 |
| 旧格式 | `start_k`、`stop_k`、`step_k`三项一起写；不能与 `temperature_ranges`混用 |
| 元数据 | 此表的 `run_name`、`note`、`output_directory`仍须提供；组合结果保存遵循Combination自己的记录配置 |

1.7–251.7 K含两端、101点，步长为2.5 K。若每温度有3个功率点，则为303条件；
`samples_per_condition = 3`是每条件3个正式样本，不是温度网格3次重复。

## 5. [temperature_run] 与硬保护

这些字段仍会被解析，不能因本次是组合扫描就任意删除。

| 字段 | 在温度组合扫描中的作用 |
| --- | --- |
| `target_k` | 没有 `temperature_scan`表时的单一目标；有网格时不替代网格，保留值也须通过配置检查 |
| `max_delta_k` | 限制入口目标相对当时实际温度的变化；离线也检查网格相邻点及重复／内层轴回跳 |
| `max_overshoot_k` | 独立的硬过冲保护，不是温度质量容差；扩大range或选择record_continue不会取消它 |
| `pre_measure_wait_s` | 字段仍必填并校验，但Combination不按它额外等一段固定时间；入场使用temperature_stability |
| `poll_interval_s` | 此表仍必填并校验；组合判稳使用temperature_stability里的poll_interval_s |
| `interrupt_policy` | 可选 `"abort"`／`"continue"`／`"wait-confirmation"`，省略默认abort；**Combination有效策略固定abort**，写continue也不获得自动恢复 |
| `resume_recheck_s` | 可省略，默认30秒；供独立温控恢复使用，不给Combination增加断点恢复 |

`target_k`、`max_delta_k`、`max_overshoot_k`、
`pre_measure_wait_s`和本表的`poll_interval_s`均要求正数。
网格上限加过冲限值还须在 `[cryostat].temperature_max_k`内。

温度硬范围由 `[cryostat].temperature_min_k`／`temperature_max_k`限定。
Combination过渡期间会检查目标与当前实际温度形成的过冲上限，入场完成也受
目标加过冲限值约束；这些检查与普通温漂警告分开。光学分支仍严格要求实际及命令
合场 `sqrt(Bx² + Bz²) <= 3 T`，通信失败不推断零场。

正常完成时温控保持最后目标；失败／中断时尝试关闭温控并记录读回。
“温控关闭”不等于已经回到基温；清理未确认时保留不确定状态。

## 6. 执行与分析时容易混淆的地方

- `temperature → optical → lockin`先完成当前温度入场，再做该温度的各光学条件。
  同一温度内换功率通常复用已完成的温度资格；重新进入温度轴或重复扫描时重新判稳，
  不按“这个数值以前测过”永久免除等待。
- `stable_dwell_s = 120`不等于总共只等120秒；升温、接近目标以及窗口反复不成立
  都会增加时间，直到成功或总等待超时。
- 正式样本的 `actual.temperature_k`使用已有同步环境读数计算的正式窗口均温；
  另有 `actual.temperature_mean_k`、`actual.temperature_min_k`、
  `actual.temperature_max_k`与 `temperature_quality`警告供分析。
  这是同步抽样统计，不是连续温度监测。
- dark的实际温度是已有采集前读回，标记 `pre_read_not_window_mean`，
  不能当成暗态完整窗口均温，也不能由此保证暗亮同温。
- 修改TOML用于下次启动；不要认为改文件会热更新一个已启动的run。
  中断、被拒尝试及清理记录保留，当前硬件Combination不支持原进程暂停续跑／原生断点恢复。

## 7. 离线检查与入口区别

仅检查完整配置，不连接仪器：

```powershell
python -m attodry_control.combination_cli describe-hardware --config <完整配置.toml>
```

本页没有改变TOML、启动扫描或部署代码。相关入口：

- [独立单点日常温控](TEMPERATURE_RUN_GUIDE.md)：固定等待与自己的中断策略。
- [独立温度稳定性扫描](TEMPERATURE_SCAN_GUIDE.md)：其进度文件恢复规则不适用于硬件Combination。
- [光学组合扫描](PHOTONICS_COMBINATION_SCAN_GUIDE.md)。
- [温度模块实现记录](modules/TEMPERATURE.md)。

实现依据：[模式与判稳解析](../src/attodry_control/config.py)、
[Combination默认值与轴选择](../src/attodry_control/combination_hardware.py)、
[入场／采集温漂／清理](../src/attodry_control/cryostat_points.py)。
