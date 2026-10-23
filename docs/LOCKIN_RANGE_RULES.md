# 按组合扫描外层坐标设置锁相量程

`[[combination_scan.lockin_range_rules]]` 根据已有外层扫描点选择 XX、XY 的
`sensitivity_full_scale_v`，单位 **V**。它不增加扫描轴、点、样本或 repeats，
不改变扫描顺序。未配置规则时，保留原固定、自动或逐谐波量程行为。

规则编译器复用已发布的 `integration-photonics-nonlinear-hall` 分支 `ff3738f`
实现；本指南描述电学分支的接入。当前硬件仍只有 XX/XY，外层坐标仅来自已有
temperature、magnetic、SMU。没有新增 optical、rotator 或 AUX 硬件路径。
纯编译器保留可选 AUX 接口，不能据此在本分支配置 AUX 或光学坐标。

## 适用范围与互斥

- 支持现有组合 `lockin_mode = "excitation"`、`"frequency"`、
  `"frequency_excitation"` 的计划和采样路径。
- 只能引用 `combination_scan.order` 中位于 `lockin` **之前**、本次实际启用的
  模块坐标。内层模块不能参与选择；离线报错，不重排扫描。
- 每条规则必须同时填写 `lockin_xx_full_scale_v` 和 `lockin_xy_full_scale_v`，
  即使某角色未选正式曲线，也不省略该已连接角色。未启用的 AUX 字段拒绝。
- 两角色基础 `sensitivity_mode` 必须是 `"fixed"`。规则不能与自动量程、任何
  `harmonic_settings` 表或原逐点/分段 `xx_full_scale_v`、`xy_full_scale_v` 覆盖
  同时使用；冲突在连接前拒绝。
- 同一条件的各谐波、全部样本及完整窗口重试使用同一组量程；没有自动量程或
  逐谐波规则。不以实测值、过载或 `record_continue` 触发切换。

这里的量程是检测灵敏度满量程，不是激励输出、样品允许电压或输入前端量程。
SR865A 的 SCAL 和 IRNG 仍独立；规则不改变 `input_range_v_peak`、Reserve、
时间常数、滤波、参考、相位或任何磁场/温度/电学安全边界。每个满量程必须同时
属于该型号支持档位和本站相邻 `lockin_safety.toml` 允许的固定档位。

## 完整配置示例

在原本机 TOML 后追加以下规则，**不要重复创建**已有的 `[combination_scan]`
表，也不要替换设备地址。此例假定顺序为 `temperature -> lockin`，温度点表
恰好为 `[2.0, 4.0] K`。数值只说明格式，不是当前样品的推荐设置；实际档位仍
需按信号与本站安全配置确认。两路原基础量程仍须有效，供初始化和 cleanup 使用。

```toml
[[combination_scan.lockin_range_rules]]
name = "temperature_2K"
match = { temperature_k = [2.0] }
lockin_xx_full_scale_v = 0.010
lockin_xy_full_scale_v = 0.001

[[combination_scan.lockin_range_rules]]
name = "temperature_4K"
match = { temperature_k = [4.0] }
lockin_xx_full_scale_v = 0.050
lockin_xy_full_scale_v = 0.010
```

长 `match` 可以使用合法嵌套表 `[combination_scan.lockin_range_rules.match]`；
此时应把该条规则的 `name`、`point_indices` 和两个满量程字段写在嵌套表之前，
避免误把它们放入 `match`。TOML 内联表请保持合法单行语法。

### 匹配语义

- 同一坐标数组中的值是“或”；不同坐标和 `point_indices` 之间是“且”。
- 未填写的外层坐标不限制匹配。例如只填温度，则该温度下所有外层 SMU 点
  都选择同一规则，仍须保证不存在另一条与之重叠的规则。
- 使用原始计划目标值**精确匹配**，不使用实测温度、实际场、测得电流、区间、
  隐含容差、通配符或优先级。值必须有限、数组非空且无重复。
- 每个现有外层访问必须恰好匹配一条规则。遗漏、重叠、重名、无匹配规则、
  未启用/内层坐标和不在原计划中的目标值，在打开仪器前报错。
- 没有无条件覆盖或“后写优先”。全程同一量程可直接使用原固定设置。

可用于选择的键：

| 模块 | 本次点表中实际存在的键 | 单位 |
| --- | --- | --- |
| temperature | `temperature_k` | K |
| magnetic | `field_x_t`、`field_z_t`，包括点表中显式固定的分量 | T |
| smu | `smu_bias_v`、`gate_top_v`、`gate_bottom_v` | V |
| smu | `smu_bias_a`、`gate_top_a`、`gate_bottom_a` | A |

SMU 只接受当前各角色实际 source mode 对应的坐标。模块已启用不表示所有可能
的键都存在。`lockin_excitation_v_rms`、`lockin_frequency_hz` 属于锁相自身，
不能参与外层选择。同一规则下全部激励和谐波沿用该通道一个满量程，应根据本组
预计最大信号选档。

### 联合匹配

若顺序为 `temperature -> magnetic -> smu -> lockin`，以下单条规则选择既有
计划中同时满足三个模块坐标的条件；其他条件仍须用不重叠规则完整覆盖：

```toml
[[combination_scan.lockin_range_rules]]
name = "cold_zero_field_selected_gate"
match = { temperature_k = [2.0], field_z_t = [0.0], gate_bottom_v = [0.0, 1.0] }
lockin_xx_full_scale_v = 0.010
lockin_xy_full_scale_v = 0.001
```

这条规则不会新建 2 K、0 T、0 V 或 1 V 扫描点。

## 往返和重复点

默认相同目标坐标再次访问使用同一量程，包括往返和重复点。若需要区分两次
相同 Bz，可用模块展开后点表的**零起始序号**，不用实测趋势猜方向。

假设 magnetic 原点表为 `[-0.1, 0, +0.1, +0.1, 0, -0.1] T`，两条规则为：

```toml
[[combination_scan.lockin_range_rules]]
name = "forward"
point_indices = { magnetic = [0, 1, 2] }
lockin_xx_full_scale_v = 0.010
lockin_xy_full_scale_v = 0.001

[[combination_scan.lockin_range_rules]]
name = "return"
point_indices = { magnetic = [3, 4, 5] }
lockin_xx_full_scale_v = 0.050
lockin_xy_full_scale_v = 0.010
```

`point_indices` 也可与 `match` 联用。索引不是整个条件笛卡尔积的序号、样本号或
condition ID；repeats 不重新定义模块点号。改变、插入或重排点表后须重新检查
预览。索引和坐标矛盾导致无匹配时，离线拒绝。

## 切换位置、等待与清理

协调层用完整计划条件选规则，在该条件进入 `lockin` 轴、锁相资格检查和正式
采集之前交给现有锁相会话执行；温度、磁场和 SMU 驱动不负责设置锁相。
原外层动作、判稳与保护仍按原顺序运行。锁相内层模块尚未实际到达的目标，不能
当作已测得状态；它们也不能用于匹配。

```text
按条件选定规则 → 新鲜读取各角色当前量程
→ 仅不同档位发送设置 → 读回核验并更新当前预期设置
→ 复用已有剩余 settle → 锁相资格检查 → 正式采集
```

同一有效量程不重复写入，但仍取得新鲜读回，记录规则和条件归属。实际设置
变化计入现有单调时钟截止时间；XX/XY 共用等待，按串行响应所有权依次读回，
没有新增固定 sleep。换档会增加必要的设置/核验和剩余等待，不承诺实机提速。

部分通道写入或核验失败立即停止该条件，保存已完成/尝试的动作及最后确认值，
不继续采集混合设置、不自动回滚后冒称成功。随后执行原 cleanup。原始基础
配置保持不可变，正常/异常清理仍恢复本次启动的基础量程、h1、Reserve、最小
XX 激励及原有频率策略；规则不能把最后条件量程变成新的清理基线。

## 离线预览、记录和分析

在对应 worktree 根目录检查：

```powershell
$env:PYTHONPATH = (Resolve-Path ./src).Path
python -m attodry_control.combination_cli describe-hardware --config config/hardware.local.toml
```

这是离线计划解析，不连接仪器或消费状态锁存。输出中的
`plan.lockin_range_rules` 保存：

- `contract = "outer-condition-lockin-range-v1"`、原始 `rules`；
- `outer_modules` 和每个外层访问的 `preview`；
- `condition_bindings`：**每个完整条件**的 `condition_id`、`sequence_index`、
  `repeat_index`、`outer_indices`、完整目标 `requested`、`rule_name`、
  `full_scales_v`。内层激励和 repeats 保持原条件号与顺序。

先核对条件号、目标坐标、点索引、唯一规则与 XX/XY 满量程，再按既有授权步骤
运行。请求、实际核验满量程和逐路切换事件随条件归档；正式样本保留有效配置。
文件监控显示归档中的当前规则和量程，不另开仪器查询。分析/导出保留规则与
`lockin_range_configuration`，按有效量程配置区分曲线和统计，不能用今天的
TOML 重新解释旧样本。原始 LIAS、invalid/rejected 和默认无效排除策略不变。

## 整条件恢复

第一版要求归档规则及展开结果一致，不能在 `resume` 时增加、删除或修改规则
或其量程；需要改变规则应新建 run。无规则运行原本允许的恢复配置修改保持。
`resume --check` 离线核对规则快照、完整条件绑定和源码兼容性；它不能授权任意
跨版本恢复。恢复后的第一个未完成条件从计划重新选择、应用并核验量程，不继承
上个进程留下的内存状态；已 accepted 条件及历史失败证据不修改。

见 [组合恢复](COMBINATION_RECOVERY.md)。本分支没有新建匹配暗背景、
`once_before_scan` 缓存或连续照明系统；这些兼容限制属于光学参考分支，不能把
本分支的规则配置当成光学硬件支持。

软件离线验证、目标机交付与实机验收须分别记录。本文示例不代表当前信号未过载，
也不授权连接、改接线或启动新扫描。
