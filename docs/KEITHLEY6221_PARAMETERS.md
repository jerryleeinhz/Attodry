# 6221 正弦交流激励参数与可选值

适用版本：幅值量化协议 `electrical-excitation-detectors-v4`；旧v1～v3记录仍可读取。
本说明区分**软件已支持的参数**和仪器本身的功能；未列为支持的 WAVE 模式不能直接写入 TOML。
数值均须有限，不能用布尔值代替数字。以下示例值不是器件允许上限的实测证明。

## `[excitation_6221]`：连接设置必填，每个坐标选择一种形式

| 字段 | 当前可选值／范围 | 25 Hz 复查配置 |
|---|---|---|
| `resource` | 非空单行 VISA 地址；拒绝 `CHANGE_ME` 占位符；不能与探测器／SMU 地址重复 | 待填写真实6221地址 |
| `expected_idn` | 非空、包含 `6221` 的身份匹配串；实际仍独立核验厂家和精确型号 | `"MODEL 6221"` |
| `inner_shield` | `"output_low"` 或 `"guard"`；分别对应三同轴内屏蔽连接 Output Low 或 Cable Guard | `"output_low"` |
| `output_low_grounding` | `"floating"` 或 `"earth"`；分别断开或连接输出低端与大地 | `"floating"` |
| `phase_marker_enabled` | 本外部TTL双探测器模式只接受 `true`；`false` 启动前报错 | `true` |
| `max_current_peak_a` | 2e-12～0.105 A；必须覆盖 `abs(offset_a) + sqrt(2)*RMS` | `0.000015`（15 μA峰值） |
| `compliance_voltage_v` | 0.1～105 V，步长0.01 V | `1.00`，沿用模板 |
| `offset_a` | −0.105～+0.105 A，且满足上面的总峰值限制 | `0.0` |
| `phase_marker_line` | 整数1～6；不得与相关触发输出／输入线路冲突 | `3`，即Phase Marker Link #3 |
| `phase_marker_phase_deg` | 0～360° | `0.0`；这是软件模板值，非厂家180°默认值 |
| `current_rms_points_a` | 非空有序列表，每点在 `2e-12/sqrt(2)`～`0.105/sqrt(2)` A RMS；另受配置峰值上限限制 | `[0.5, 2.5, 4.5, 6.5, 8.5, 10.5]` μA RMS，实际TOML使用A |
| `current_rms_ranges_a` | 与上行二选一；非空范围表数组，`min`／`max` 使用同样的A RMS边界；linear 段用 `points` 或 `step`，log 段用 `points` | 0.5～10.5 μA RMS，等距6点 |
| `frequency_points_hz` | 非空有序列表，每点0.001～100000 Hz；另校验各检测谐波的型号频率能力 | `[25.0]` |
| `frequency_ranges_hz` | 与上行二选一；非空范围表数组，`min`／`max` 使用同样的Hz边界；linear 段用 `points` 或 `step`，log 段用 `points` | 此次单点频率继续用列表 |

频率外循环、电流内循环；保留重复访问，不补点。源网格和整个组合计划均受已有数量上限约束。
10.5 μA RMS 对应约14.8492424 μA峰值。设置读回不等于通过样品的实测电流。
`inner_shield` 与 `output_low_grounding` 是两个独立的输出连接选项；与 Lock-in 的 `shield_grounding` 无关。

### 列表与范围

```toml
current_rms_ranges_a = [
  { min = 0.5e-6, max = 10.5e-6, scale = "linear", points = 6 },
]
frequency_points_hz = [25.0]
```

每个坐标必须恰好提供对应的列表或范围之一；同时提供、两者都缺少、未知字段均在连接前报错。
`current_rms_points_a`／`frequency_points_hz` 只接受具体数值列表，范围表放在对应的 `*_ranges_*` 中。
每段的 `min`／`max` 必须有限、在仪器范围内且 `max > min`。
`scale="linear"` 必须且只能选择 `points` 或 `step`：`points` 是至少2的整数（含两端点），
`step` 是同一坐标单位的有限正数，按十进制精确整除 `max-min`；不静默截断或补端点。
`scale="log"` 只接受 `points`。布尔、浮点计数和 `point` 拼写均拒绝。
单点或任意往返顺序继续使用显式列表。多个范围按声明顺序拼接，共用端点保留重复访问，不排序、不去重。
电流与频率可以分别选择不同形式；每个坐标累计点数及源网格乘积最多100000，展开前先检查数量。

上述例子精确展开为 `[0.5e-6, 2.5e-6, 4.5e-6, 6.5e-6, 8.5e-6, 10.5e-6]`，
在启动离线解析阶段归一化，运行、归档及分析继续使用原有点列表，采集中不临时生成点。
以下 step 写法得到11个电流点和9个频率点，共99个激励条件；相同网格也可分别写 `points=11`／`points=9`：

```toml
current_rms_ranges_a = [
  { min = 1e-7, max = 11e-7, scale = "linear", step = 1e-7 },
]
frequency_ranges_hz = [
  { min = 100, max = 500, scale = "linear", step = 50 },
]
```

恢复按展开后的精确点列比较；等价列表与范围可以恢复同run，其他原有源码、设置、故障及cleanup审查仍须通过。
实际点数、点值或顺序变化仍须新运行。原TOML保留配置形式及文件哈希供审计。
此功能不增加仪器命令、串行查询、等待或自动重试。

## WAVE功能覆盖范围

| 项目 | 本版本行为 | 是否自由选择 |
|---|---|---|
| Function | `SIN` 正弦波 | 固定；不接受方波、Ramp或ARB参数 |
| Amplitude／Frequency／Offset | 来自上述配置；RMS幅值转换成峰值命令 | 按上表 |
| Phase Marker State／Point／Line | ON／配置相位／配置Link线 | State必须ON，Point与Line可配置 |
| Ranging | `BEST`，按波形幅值选择合适范围 | 固定，不接受FIXed模式配置 |
| Duration | `INFinite`，由扫描结束／失败清理停止 | 固定，不接受有限时间或周期数 |
| Trigger mode | 软件 `ARM`→`INIT`；外部波形触发OFF，输入线0 | 固定，不接受外部触发 |
| Duty cycle／ARB data／retrigger／inactive value | 本正弦、无限、软件触发模式不启用这些功能 | 未支持；不得作为配置键传入 |

这些固定设置写入预览与归档，启动时设置并读回；不是依赖上次前面板的状态。
Phase Marker Link #3 是同步**输出**，不是波形外部触发输入。保持原有触发线路冲突检查。
不使用 `*RST` 重置整台仪器。输出响应速度、模拟滤波等不在本次新增配置接口内，不声称覆盖全部厂家菜单。
无限持续时间接受文字INF及精确SCPI数值9.9e37：D04 /700x已实际验证成功设置
`SOUR:WAVE:DUR:TIME INF`、错误0后读回`+9.9e37`。该解码只用于持续时间设置，
不适用于电流、电压、频率或科学测量值；近似数值、NaN及负数仍拒绝。

## `[lockin_acquisition]`

| 字段 | 可选值／范围 | 此次值 |
|---|---|---|
| `xx_harmonics` | 从1、2、3中选择，不重复；可为空但两角色不能同时为空 | `[1]` |
| `xy_harmonics` | 同上 | `[2]` |
| `settle_time_constants` | 有限数值，至少5 TC | `15.0` |
| `sample_interval_time_constants` | 有限非负数 | `1.0` |
| `overload_policy` | `"abort"`、`"continue_unselected"`、`"record_continue"`；保留已有策略含义 | `"record_continue"` |
| `skip_unsupported_harmonics` | `true`／`false` | `false` |

时间常数1 s、15 TC表示最终相关设置改变后的稳定要求；复用共享剩余等待，XX/XY串行读取，不各自重复完整等待。

## 此次探测器与组合参数

- `combination_scan.excitation_source="keithley6221"`、`lockin_mode="detector"`；`order` 必须让 `excitation` 位于 `lockin` 前。可组合已有 temperature/magnetic/smu，不能混入旧 `lockin_sweep`。
- XX型号只接受SR830，XY在此拓扑只接受SR865A；两个 `address` 必须真实且不同。
- `channel_label` 是可选文字标签；此次分别为 `Vxx/b axis 04/06` 和 `Vxy/a axis 05/09`。
- 本项目当前输入枚举：`reference_source="external_ttl"`、`external_reference_edge="rising"`、`sine_output_connected=false`、`input_mode="a_minus_b"`、`shield_grounding="float"`、`input_coupling="ac"`。
- 此次 `sensitivity_mode="fixed"`、`sensitivity_full_scale_v=0.5`、`time_constant_s=1.0`、`filter_slope_db_oct=24`。其他档位必须同时属于对应型号支持档位和本站 `lockin_safety.toml` 允许范围；原有bounded_auto需其完整配置，不能只改一个模式字段。
- XX `reserve_mode` 可为 `high_reserve`、`normal`、`low_noise`，此次保留 `normal`；XY SR865A不用SR830 Reserve。
- SR865A输入范围保留 `input_range_v_peak=1.0`，参考输入阻抗 `reference_input_impedance_ohm=1000000.0`，`sync_output_mode="preserve"`。型号完整可选档位见 [SR865A说明](LOCKIN_DAILY_OPERATION.md#sr865a-作为-xy-接收机)。
- `current_status_supported` 是固件能力声明，只有确认后才能设 `true`；模板保留 `false`，不能因需要通过预检就改为true。
- 角色中旧 `frequency_hz`／`source_voltage_v` 为兼容字段，本模式不写入探测器输出；实际频率由6221网格决定。
- `samples_per_condition`、`repeats` 是1～1000整数，当前均1；名称及note为字符串。`project.mode`／`backend` 使用 `hardware`；数据库路径相对TOML目录解析。VISA超时沿用5000 ms。

## 执行、记录与恢复

先确认OFF／DC零／波形停止，再设置并核验 `OUTP:ISH`、`OUTP:LTE`，然后设置WAVE并启动。设置失败不继续采集。
采集前后状态包含原始内屏蔽响应及接地布尔值，独立归档验证会核对解码与请求值。采集中变化不自动修复。
清理只ABORT、OFF并核验，保留最后实际连接状态，不自动改接地；部分设置失败仍独立尝试关闭源。

新记录使用v4协议，采集前后必须保存专用联锁查询和幅值量化证据。旧v1/v2档案
按原证据及原规则读取，不补造联锁证明；v1缺少的接线信息仍未知。
修改输出接线配置或迁移测量协议需新运行，不能无声改变同run的协议。原SR830激励模式不变。

### 联锁、当前条件与历史事件

D04的AMPL?以七位有效数字返回峰值；这不是硬件编程步长。
旧v3档案保留原来的七位舍入判据，新v4使用下面的统一量化规则。

D04的FREQ?也已观察到七位有效数字读回：例如请求
20.7780071796435 Hz，原始回包 `2.077801E+01`（20.77801 Hz）。
实时和归档校验接受目标准确舍入至七位后的值，原有窄容差仍保留；
不扩大所有频率的相对容差，也不把显示精度推断成DDS硬件编程步长。
请求频率及条件编号保留，实际频率坐标使用原始源设置读回。
错误数量级、未执行设置、未知回包、通信及保护故障仍拒绝。

### 幅值量化、设置读回与真实电流

厂家[Reference Manual 第7-29页](https://download.tek.com/manual/622x-901-01(B-Jun2005)(Ref).pdf)
规定 `SOUR:WAVE:AMPL` 使用峰值，BEST在ARM时按幅值和offset选范围。
[SPEC-6221 Rev C 第1页](https://download.tek.com/document/SPEC-6221C_October2012_Customer.pdf)
列出了下表的电流源编程分辨率；第3页的16位是任意波形幅度分辨率。
这些资料**没有单独规定 WAVE:AMPL 参数存储的最小LSB或内部取整算法**。
因此软件明确采用下表作为保守的源设置网格，不声称它等于固件内部LSB，
也不从AMPL?显示位数或16位波形DAC反推一个未经证明的SCPI步长。

| 名义电流源范围 | 软件采用的厂家编程网格 |
|---|---|
| 2 nA | 100 fA |
| 20 nA | 1 pA |
| 200 nA | 10 pA |
| 2 μA | 100 pA |
| 20 μA | 1 nA |
| 200 μA | 10 nA |
| 2 mA | 100 nA |
| 20 mA | 1 μA |
| 100 mA | 10 μA |

`6221-source-range-grid-v1` 在连接前对全部原始RMS点展开：换算峰值，
按幅值加绝对offset和各范围5%扩展能力选择网格，取允许范围内最近值，
等距时取较低值。`planning_source_range_a` 是离线规划范围，不冒充硬件范围读回。
原请求超出获准上限仍拒绝；只有正常网格舍入可能越界时才选择范围内邻点。
不修改offset、5 V等已有限压或 `max_current_peak_a`，不自动量程重试。

幅值命令使用精确十进制网格文本。每个条件的 `amplitude_quantization`
保存原RMS、原峰值、下发峰值及SCPI文本、有效RMS、网格、规则、offset和上限。
`current_peak_a` 与 `actual.excitation_current_rms_a` 始终来自实际AMPL?读回；
它们是**源设置读回，不是通过样品的实测电流**。实际峰值加实际offset仍独立检查上限。
条件ID及目标坐标不变；不同目标量化到同一值也保留各条件。
`describe-hardware --config <TOML>` 的离线 JSON 字段 `excitation_amplitude_programming`
及 `run` 启动摘要表列出全部源点（`run` 会实际运行，离线预览请用前者），
`same_effective_source_point_indices` 标明相同频率下的重复有效值。

读回校验以量化后的设置为基准，使用七位十进制半单位加binary32半ULP的窄界，
不使用电流源准确度或整个编程步长作为容差。该数值界涵盖D04的已记录21 nA
案例（原命令峰值2.9698484809834997e-8 A，读回2.969849e-8 A），
不是对固件内部浮点实现的断言。新网格下该点下发2.970e-8 A峰值。
命令未生效、错误数量级、未知状态、通信、限压与真实失锁仍停止。
正常量化只增加审计记录，不增加查询、重写、重试或稳定等待。

采集、accepted归档/恢复校验和正常分析共用同一纯函数；v4缺少或伪造量化证据即拒绝。
分析表保留 `excitation_programming.*` 字段。旧v1～v3按原合同读取，不补造量化证据。
跨v3/v4协议迁移需新run；同一v4扫描的whole-condition恢复重新计算本条件设置，
保留accepted前缀及原有源码、配置和清理审查。

厂家手册表14-2（印刷页14-6）规定：`OUTP:INT:TRIP?` 返回 `1` 表示联锁闭合，
`0` 表示联锁打开。它是查询，不是TOML中需要开启的参数。新驱动每次核心状态
读取使用该查询判定联锁；非0/1回包或通信失败拒绝，不能回退到猜测。

2026-11-13同机只读核对固件 `D04 /700x`：专用查询1、MEAS:COND为0、
OPER:COND为128、QUES:COND为0、输出OFF、DC设定0。保留两种状态的原始值，
不再要求COND bit1必须置位。D04发布说明没有说明这个差异的原因，不能宣称已证明是固件缺陷。

`STAT:MEAS:EVEN?` 是读取后清除的历史锁存，`10=2+8` 包含联锁事件和compliance事件；
不等于此刻仍compliance。驱动仍保留并拒绝compliance等异常事件，不静默清除、
忽略或循环重试。当前限压由COND bit3独立记录。此前失败预检的读取记录保持不变。
本修正不升级固件、不改变电流/电压上限或联锁硬件。

固件依据：[厂家D04发布说明](https://download.tek.com/software/supporting_files/6220FirmwareReleaseNotesD04_6221-801D04.pdf)。

厂家依据：[622x Reference Manual](https://download.tek.com/manual/622x-901-01(B-Jun2005)(Ref).pdf)，第2-7～2-8页及第7章。
示例：[25 Hz配置](../config/hardware.6221_25Hz.example.toml)。地址及固件声明未确认前仍是复查模板，不是已完成实机验证。
