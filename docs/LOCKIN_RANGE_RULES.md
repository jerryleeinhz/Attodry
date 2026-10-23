# 按外层扫描点设置锁相量程

本功能通过 `[[combination_scan.lockin_range_rules]]` 为已有外层扫描条件选择
XX、XY 和可选 AUX 的灵敏度满量程。量程是条件的附属设置，不增加扫描维度或
测量点。未配置规则时，继续使用各锁相节原有的固定量程。

这里的“量程”是检测端的 `sensitivity_full_scale_v`，单位 **V**。例如
`50e-6` 是 50 µV 满量程。它不改变激励源输出，不代表样品允许电压，也不改变
SR865A 的 `input_range_v_peak`；输入端过载不能仅通过增大检测灵敏度满量程解决。

## 适用范围

规则引用 `combination_scan.order` 中排在 `lockin` **之前**的已启用模块。温度、
磁场、光学、光路波片 rotator、positioner 和已有 SMU 坐标都使用相同的匹配机制。规则不会
启用模块、重排扫描、扩大轴点表或增加任何设备能力。rotator 只表示已有获批的
光路波片机械角度，不表示冷台或样品旋转。

positioner 的绝对模式匹配 `positioner_x_um/y_um/z_um`，相对模式匹配
`positioner_x_offset_um/y_offset_um/z_offset_um`（焦面模式只有 XY offset），
或用 positioner 点序号选择重复访问；规则只用冻结计划的请求坐标，不用运动中 actual
或噪声自动换档。焦面绝对 Z 是派生坐标，不增加扫描维度。示例和单位见
[POSITIONER](modules/POSITIONER.md)，仍须对所有外层联合条件唯一、完整覆盖。

第一版用于固定灵敏度、只扫描激励幅度的光电锁相会话：包括当前内部 XX 参考的
两台 SR830 和可选第三台 AUX SR865A，以及已有 PEM 光电会话。内部 XX 参考组合
不要求包含 optical 轴，温度、磁场或 SMU 外层加 lockin 也可在满足组会话能力时
使用规则；此时不连接光学模块。其他未转换锁相路径和频率扫描不支持本规则。
已启用通道必须使用固定灵敏度；规则不能与自动量程、
逐谐波或原有逐点/分段量程覆盖同时使用。不支持的锁相路径在连接设备之前拒绝。
各通道的型号可选离散档位和本站相邻 `lockin_safety.toml` allowlist 均继续生效。
内部 XX 参考路径还须满足已有组会话的 A−B 电压输入、AC 耦合和 24 dB/oct RC
要求；两台与三台规则会话都按这些能力检查。没有规则的原有会话保持原支持范围。

## 配置方法：完整覆盖所有条件

在原组合 TOML 中追加规则。不要重复已有的 `[combination_scan]` 表，也不要复制
或替换设备地址。下面是可直接通过 TOML 语法解析的规则片段；其前提是已有的
光学计划恰好包含 1、6、15 µW 三个目标功率，扫描顺序为 `optical -> lockin`，
且 XX、XY、AUX 三路均已启用。数值只是用法示例，必须根据实际信号和本站允许
档位选择，不能把示例当作当前样品的推荐量程。

```toml
[[combination_scan.lockin_range_rules]]
name = "power_1_and_6_uW"
match = { optical_target_power_w = [1e-6, 6e-6] }
lockin_xx_full_scale_v = 50e-6
lockin_xy_full_scale_v = 20e-6
lockin_aux_full_scale_v = 100e-6

[[combination_scan.lockin_range_rules]]
name = "power_15_uW"
match = { optical_target_power_w = [15e-6] }
lockin_xx_full_scale_v = 200e-6
lockin_xy_full_scale_v = 50e-6
lockin_aux_full_scale_v = 500e-6
```

仅启用 XX、XY 时，每条规则只填写 XX、XY 两个字段；不能填写未启用的 AUX。
启用 AUX 时，三路字段都必填。各条规则写全所有已启用通道，避免多个局部覆盖
叠加后无法判断当前完整设置。原各锁相节的基线固定灵敏度也仍须有效。

匹配语义如下：

- 同一个数组内的值是“或”：1 µW 或 6 µW 都匹配第一条规则。
- 不同坐标以及 `point_indices` 中的条件是“且”：必须同时满足。
- 规则没有填写的外层坐标不参与匹配；例如没有写温度，则匹配所有计划温度。
- 数组必须非空，值必须是有限数。坐标匹配使用计划中保存的目标值；不提供区间、
  通配符、实测值阈值或隐式容差。使用与计划相同的数值，不能依靠四舍五入猜测匹配。
- 每个外层条件必须恰好匹配一条规则；遗漏、重叠、重名、永远不匹配的规则，以及
  未启用或位于 `lockin` 内层的坐标，都在离线预检中报错。不存在“最后一条优先”。

规则会在连接仪器前按完整扫描计划展开并校验。不要用一条无选择条件的规则当作
覆盖其他规则的默认分支；如需全程同一量程，使用原有固定量程配置即可。

## 可以引用的外层坐标和单位

只接受实际存在于本次外层轴点表中的键。一个模块已启用，不表示它所有可能坐标
都存在；例如未参与当前计划的第二转台角度不能用来匹配。

| 模块 | 坐标键 | 单位与含义 |
| --- | --- | --- |
| temperature | `temperature_k` | 计划目标温度，K |
| magnetic | `field_x_t`、`field_z_t` | 计划目标 Bx、Bz，T，包含计划中显式固定的轴值 |
| optical | `optical_target_power_w` | 反馈目标光功率，W |
| optical | `optical_wavelength_nm`、`optical_bandwidth_nm` | 计划波长、带宽，nm |
| optical | `optical_source_level_pct`、`optical_nd_pct` | 计划源设置、ND 设置，% |
| optical | `optical_current_a` | 计划 CLD 起始/源电流坐标，A；不是反馈中的实时电流 |
| optical | `optical_pulse_picker_ratio` | 计划 pulse-picker 比值 |
| rotator | `rotator_1_angle_deg`、`rotator_2_angle_deg` | 计划机械角度，°；不按偏振周期自动等同不同角度 |
| smu | `smu_bias_v`、`gate_top_v`、`gate_bottom_v` | 已配置角色的计划源电压，V |
| smu | `smu_bias_a`、`gate_top_a`、`gate_bottom_a` | 已配置角色的计划源电流，A |

`lockin_excitation_v_rms` 和 `lockin_frequency_hz` 不是本功能的外层选择条件。
同一外层条件下全部锁相内层激励/谐波访问沿用同一组量程，应按其中预计最大的
信号选档。不要将本功能当作逐激励自动换档或逐谐波量程。

## 联合选择多个模块

例如顺序为 `temperature -> magnetic -> optical -> rotator -> lockin`，可以使用：

```toml
[[combination_scan.lockin_range_rules]]
name = "cold_zero_field_low_power_selected_angles"
match = { temperature_k = [1.7], field_z_t = [0.0], optical_target_power_w = [1e-6, 6e-6], rotator_1_angle_deg = [0.0, 90.0] }
lockin_xx_full_scale_v = 50e-6
lockin_xy_full_scale_v = 20e-6
lockin_aux_full_scale_v = 100e-6
```

此片段仅展示一条联合规则，完整计划中的其余条件也必须有互不重叠的规则。
它只从原有点表选取条件，不会创建 1.7 K、0 T、1/6 µW 或 0/90° 新扫描点。
长选择条件也可写成合法嵌套表 `[combination_scan.lockin_range_rules.match]`；
此时应把该条规则的 `name` 和各量程字段写在嵌套表之前，避免量程被误放入 `match`。

## 往返磁场和重复角度：使用点序号区分访问

默认相同目标坐标再次出现时使用相同规则，包括往返磁场、重复角度以及重复扫描。
需要同一磁场在上扫和下扫使用不同量程时，可以用 `point_indices` 选择模块点表的
**零起始序号**。序号对应该模块展开后的原始有序点表，不是条件编号、样本编号、
浮点磁场值或整个笛卡尔积的位置。重复扫描不会重新定义模块点序号。

假设磁场点表为 `-0.1, 0, +0.1, +0.1, 0, -0.1 T`，完整两规则示例为：

```toml
[[combination_scan.lockin_range_rules]]
name = "field_forward"
point_indices = { magnetic = [0, 1, 2] }
lockin_xx_full_scale_v = 50e-6
lockin_xy_full_scale_v = 20e-6
lockin_aux_full_scale_v = 100e-6

[[combination_scan.lockin_range_rules]]
name = "field_reverse"
point_indices = { magnetic = [3, 4, 5] }
lockin_xx_full_scale_v = 200e-6
lockin_xy_full_scale_v = 50e-6
lockin_aux_full_scale_v = 500e-6
```

`point_indices` 只能引用已经启用、位于 `lockin` 外层的模块，值必须为有效非负整数
序号数组。它也可与 `match` 同时使用，表示同时满足索引和坐标条件；这两者矛盾
而导致规则无匹配时，预检报错。插入、删除或重排磁场点后必须重新检查离线预览。

## 切换时机、质量策略与采样

组合协调层按当前完整的计划条件选择规则，在第一次准备 `optical` 或 `lockin` 轴
之前应用灵敏度，确保先于光学参考转换、锁相资格核验、匹配暗背景和正式采样。
排在此位置之前的环境轴仍先按原流程到达、判稳；排在此位置之后的其他外层轴
尚可能继续运动或准备，因此选档依据完整计划目标，不声称此时所有外层坐标均已
实际到达。各模块原有运动/光源保护和最终资格检查继续执行。
改档后读回核验，更新本条件的预期设置，并完成原有滤波稳定等待与一次新鲜状态
核验；后续已有保护不省略。换档不改变
激励、参考拓扑、相位、时间常数、滤波斜率、Reserve 或源安全边界。

选择规则只看计划目标，不看温度、磁场、光功率或角度的实际读回波动。
`temperature_deviation_policy="record_continue"`、`magnetic_deviation_policy="record_continue"`
及已有光功率质量策略仍按原契约工作，不会因为允许质量警告而反复换量程。设备故障、
未知通信、设置变化或硬安全边界仍按原流程终止。同一条件的正式窗口和窗口恢复
保持同一组量程，不在采集窗口中自动调档。

任一路设置或核验失败都不能继续本条件，部分成功的设置和原始 I/O 仍保留，随后
执行已有独立清理。新进程按新启动时获准使用的计划重新选择、应用并核验，不能
假设上一个进程留下的仪器量程仍然正确。本功能没有新增硬件扫描断点恢复能力。
规则和生效设置属于本次归档；不能拿今天的 TOML 重新解释旧样本。

## 暗背景和审计

开启匹配暗背景时，推荐 `[optical_background]` 原有配置中的
`capture_mode="per_condition"`。匹配上下文包含当前生效灵敏度；不同量程下的暗
背景不能互相替代。已有背景配置节名称及其余必填参数请沿用当前本机 TOML。

`capture_mode="once_before_scan"` 仅在所有规则解析出的有效量程始终相同时才可能
兼容；只要本次计划确实会改变量程，启动前拒绝，要求改用逐条件背景。其他已有的
角度、相位、参考或背景有效性限制仍可更严格，不能通过规则放宽。

`illumination_policy="continuous_gate_scan"` 要求同一光学组内所有内层 gate/lockin
访问的物理量程保持相同。组内不同 SMU 点若需要不同量程，离线预检拒绝；应调整
规则或采用已有的逐条件关光模式。只有规则名不同、实际 XX/XY/AUX 量程完全相同
时，不会为了更换规则名在组内关光，原连续照光行为保持。

量程规则、计划条件的解析结果、请求量程、实际核验量程和换档事件随扫描归档。
正式样本和背景都应可追溯到当时的有效量程。监控只读数据库和记录，不为显示量程
另开锁相会话。规则本身不调整背景采样数、正式样本数或失败窗口重采策略。

## 离线预览与接管检查

先在项目根目录运行：

```powershell
python -m attodry_control.combination_cli describe-hardware --config config/photonics.local.toml
```

将路径替换为实际本机 TOML。该命令只解析计划和安全配置，不连接仪器。检查每个
外层条件的目标坐标、模块点序号、唯一规则及 XX/XY/AUX 量程；特别检查返回相同磁场
或角度的访问。覆盖检查失败时先修正规则，不启动真实扫描。

结构化计划快照中的 `lockin_range_rules` 保存原规则及 `preview`。每一行预览包含
外层模块点序号 `outer_indices`、请求坐标 `requested`、规则名 `rule_name` 和各角色满量程
`full_scales_v`。锁相内层激励点不增加规则预览行；完整测量条件继续保留原顺序。

软件离线测试、提交、文件部署和实机验收是不同阶段。部署文件不会自动启动扫描；
本指南中的示例也不是开光、接线变更或真实扫描授权。实机接管应使用已确认的量程表，
并检查换档、等待、暗背景和采集的实际记录。离线测试通过不证明样品信号未过载。

相关契约：[光电组合扫描](PHOTONICS_COMBINATION_SCAN_GUIDE.md)、
[三路锁相](THREE_LOCKIN_GUIDE.md)、[测量时序与验证](MEASUREMENT_PERFORMANCE.md)。
