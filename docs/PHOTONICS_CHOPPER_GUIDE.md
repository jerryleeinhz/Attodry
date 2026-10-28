# 双 SR830、斩波器与光学扫描

这次交付支持 **XX 内参考 → XY 与斩波器；XX/XY 均测 h1**，继续使用
Combination。实现验收以离线／fake 测试为主；交付期间另经授权只读核验了MC2000B
身份和既有设置，仍未完成新配置的实机启停、频率、相位和暂态响应验收。

## 接线与配置选择

```text
XX SR830 内参考 ── 后面板 TTL OUT ──┬── XY SR830 REF IN
                                    └── 斩波器 EXT REF INPUT
XX 信号输入 ← 样品 Vxx                 XY 信号输入 ← 样品 Vxy
XX SINE OUT → 样品，或物理断开（必须在 TOML 中如实声明）
XY SINE OUT 必须物理断开样品
激光 → 已确认的斩波器刀片轨道 → 样品／功率计的已确认光路
```

两台锁相的 h1 都相对于 XX 的基频 f₀。斩波器倍频／分频必须按刀片和实际光束轨道
配置，使实际照光轨道也是 f₀；不能默认所有刀片都用1/1。`harmonic_n/d` 是斩波参考倍频／分频，
`excitation_xx_harmonics/xy_harmonics` 是锁相解调阶数，两者独立。
特别是 MC2F 双频刀片，内圈频率还包括内外槽数比。配置加载器核对刀片范围以及
检测频率与光学调制频率的关系，不猜测刀片，不自动调整谐波。

`harmonic_n`与`harmonic_d`均为1..15整数。例如MC2F47的4槽内圈若目标8Hz，
外参考也是8Hz，N=7、D=4给外圈14Hz、内圈8Hz；这是接口与计算说明，物理接法由
操作者确认。控制器原生频率读回与实际入射光束的频率必须分别解释，不把配置加载
通过当成实机频率、透光和相位锁定验收。

`single_opening_per_rotation` 可省略，默认 `false`，按原刀片槽数计算；`true` 只支持
MC2F 双频刀片的 `reference_track="inner"`，并声明**外圈保持完整、内圈已经遮挡至每转
只产生一次透光事件**。它不修改刀片编号或使软件获得遮挡状态读回，须由操作者确认。
其他刀片／轨道不接受 `true`，也不提供任意自定义槽数。

例如 MC2F47 外圈7槽完整、内圈4槽遮3留1，XX 内参考8Hz、两路均测h1，可用以下
`[chopper]` 字段组合；保留其余已确认的型号、连接、身份和保护设置：

```toml
[chopper]
blade = "MC2F47"
reference_source = "external"
reference_track = "inner"
single_opening_per_rotation = true
harmonic_n = 7
harmonic_d = 1
```

此时原生外圈频率为 `8×7/1=56Hz`，转速为 `56/7=8转/s`，未遮内圈原生频率为
`56×4/7=32Hz`，但声明的单透光内圈实际光束频率是8Hz。驱动使用完整外圈的原生
参考输出核验56Hz，再推导光束8Hz；原始`refoutfreq?`不能标成8Hz实测。快照分别保存
原生参考输出和推导光束频率，后者依赖现场遮挡声明。若相同N=7/D=1却保持`false`，
代码按未遮内圈32Hz解释，并拒绝把它当作8Hz的h1/h1扫描。前面板相位锁定确认仍独立保留。

| 使用情况 | 模板 | 关键设置 |
|---|---|---|
| NKT/VARIA＋双 SR830＋可选斩波器 | `config/photonics_chopper.example.toml` | `laser_source="nkt"`、`internal_xx_xy`、`use_pem=false` |
| CLD1010LP／CLD1015＋双 SR830＋可选斩波器 | `config/photonics_cld.example.toml` | `laser_source="cld"`、电流单位 A、标称波长 |
| 已有 PEM 参考链 | `config/photonics.example.toml` | 保留历史 PEM 模式和对应安全文件，不能仅改拓扑照搬本模板 |

将选定模板复制成 ignored 的 `config/photonics.local.toml`。补齐全部启用字段的
`CHANGE_ME`，保留同目录双 SR830 的 `lockin_safety.toml`，并审阅其中的项目安全
allowlist。真实地址、设备序列号、实验参数和数据保持本机文件，不提交。

模板中基频、斩波器型号、刀片、光束轨道、实际样品参数和激光边界都留待确认；
**不能默认使用先前 PEM 的约 50 kHz**。`[chopper].enabled=false` 表示不打开斩波器。
要控制斩波器，先确认型号／`id?`、物理刀片／`blade?`、光束轨道、可用频率及接线，
填完后才改为 true。空缺这些实机信息不妨碍代码的离线验证。

## 日常命令与运行归属

在这份 worktree 根目录使用 checkout launcher，避免另一份 editable install 的源码
混入本次进程。先只读解析和预览，确认选中的源、h1/h1、接线声明、目标数与清理策略：

```powershell
python -m attodry_control.combination_cli describe-hardware --config config/photonics.local.toml
```

完成独立实机验收并确认光路后，正式运行仍是 Combination：

```powershell
python -m attodry_control.combination_cli run --config config/photonics.local.toml
```

没有新增独立斩波扫描命令。`run` 本身授权配置所选仪器的连接、设置和发光，
不授权并发接管其他扫描。一个运行拥有对应资源；运行期间监控只读 SQLite／文件，
不另开串口或消费锁相 LIAS。命令返回后，退出码、数据库终态、真实进程是否退出和
清理证据分别查看，不能仅凭旧 PID 或最后一条日志断言仪器状态。

```powershell
python -m attodry_control.combination_cli monitor --config config/photonics.local.toml
git status --short --branch
git log -1 --oneline
```

需要中断时在拥有扫描的终端按 Ctrl+C，等正常清理完成；不 kill 进程或通过断开 USB
代替清理。通信失败意味着输出和参考状态可能未知，记录最后确认读回并人工查看仪器。

## 锁相参数与等待

`lockin_xx.sine_output_connected=false` 表示样品与 XX SINE OUT 已物理断开；仪器仍可
保留 4 mVrms 设置以维持现有硬件控制边界。数据标注该值是仪器设置，不将它推断为
样品激励。接样品时改成 true，并重新核实串联电阻和最大样品 V/I。XY 始终 false。

当前双 SR830 公共路径只开放 `a_minus_b`、`float`、`ac`、24 dB/oct、TTL rising。
不要从硬件手册其他选项推断这些枚举已被软件支持。Reserve 可选
`high_reserve / normal / low_noise`；满量程与 Reserve 是不同参数。

SR830 完整电压灵敏度档位（V）为：

```text
2e-9, 5e-9, 1e-8, 2e-8, 5e-8, 1e-7, 2e-7, 5e-7,
1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4,
0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0
```

本实验可选集合还受 `lockin_safety.toml` 本角色 allowlist 限制，不能把硬件全表当成
当前已批准项目量程。更多自动量程与单谐波覆盖写法见 `docs/LOCKIN_DAILY_OPERATION.md`。
内部 SR830 路径保存／核验当前相位，不接受 PEM profile 的 `phase_shift_deg` 字段。

`settle_time_constants` 为每路时间常数的倍数，`sample_interval_time_constants` 是同条件
后续读数间隔的倍数。等待使用最后一次相关变化后的单调时钟剩余时间；XX/XY 共同
等待并依次读取。光学稳定或 gate 延迟中已经过去的时间可抵扣响应等待；真实调档、
激励、频率、光学或环境变化会使相应等待重新计时。它不是固定等待 1 秒或 1τ。

## NKT 多波长、功率和起始电流

`nkt_run.points` 的每条行内，波长／带宽／源设置各可用 scalar、明确数组或 ranges，
每种坐标三选一。`grid_order` 将 wavelength、bandwidth、source 各列一次，最左最外层。
展开后的光学行继续保持输入顺序，再由 `target_mapping` 配对目标功率：

- `paired`：目标数组逐行对应（默认兼容旧配置）；
- `cartesian`：每个光学输入行 × 全部目标；两个波长 × 两个带宽 × 五功率即 20 点。

目标功率三选一：`target_power_w`、`target_powers_w`、`target_power_ranges`。线性区间
使用 `step` 或 `points`，对数区间使用 `points` 且两端 >0。例如以下只是语法：

```toml
target_power_ranges = [{min=50e-6,max=3e-3,scale="linear",points=6}]
```

起始策略仍属于 `power_feedback`，不会新增一个待扫轴：

| `initial_current_mode` | 起始值来源 |
|---|---|
| `point` | 当前光学输入行的 `source_level_pct`，省略时默认 |
| `per_target` | 单一展开后光学输入行；数组长度等于目标功率数 |
| `previous` | 同一输入行、连续相邻目标的上一合格实际读回；跨行／失败后重置 |
| `grid` | 展开后光学行数 × 功率目标数的二维 `initial_source_levels_pct` |

后两种显式数组模式及 previous 都要求 Cartesian；point/previous 不保留数组。
大步调节、接近目标缩步、反向缩步和局部响应斜率估计都在现有电流／功率上限及
单步范围内，不因提速提高物理限值。实机耗时改善仍需对照测试。

NKT 电流反馈使用 `control="current"`＋`actuator="source_current"`＋`source_current_*`；
功率反馈成套改为 `power`、`source_power`、`source_power_*`。默认不改变当前源模式；
`nkt_source.allow_control_mode_change` 默认 false；只有显式设为 true，才在全部模块预检
通过后的配置阶段、确认健康且 OFF 时按配置切换并读回。预检阶段不切模式。
这两种模式都保留原 ND／pulse picker。VARIA ND 闭环必须有独立实机验收和显式
`nd_control_verified=true`，不能以模板里的可选参数替代验收。

CLD 使用独立模板的 `cld1010.current_points_a` 和 `cld_current` 的 A 参数；它驱动固定
标称波长二极管，不提供 NKT 的波长／10 nm 带宽／百分数控制。标称波长只用于功率计
校正和数据标识。二极管型号、引脚、TEC、温度／电流上限及连接诊断必须保持原有保护，
不能把历史诊断格式例当作当前仪器的授权忽略模式。

### CLD1015 与 980 nm

CLD1015 继续使用 `[cld1010]` 表名和 `laser_source="cld"`，在表内显式设置
`expected_model="CLD1015"`。仅接受 CLD1010LP、CLD1015 两个准确型号；省略字段
默认 CLD1010LP，现有配置继续使用原身份规则。VISA 和 TL4000 都核对选定型号、
序列号、厂家与非空固件；运行中不自动接受另一型号。配置快照保存 expected_model，
实际身份及完整原始应答继续留在既有审计中，模拟后端也保留所选型号。

`wavelength_nm=980.0` 表示所装二极管的标称波长，传入功率计校正并保存为
`nominal_diode`；控制器不测量或扫描激光波长。功率计探头须覆盖 980 nm；固定量程
须按该波长的原生量程重新核实。电流／TEC／温度／最大光功率及步长须填写新二极管
和光路的实际批准值，旧控制器的诊断确认不得复制成新控制器的现场证据。

本次保留软件 max_current_a<=1 A（CLD1015 手册额定 1.5 A 不自动扩大实验范围）、
恒流 LD／恒温 TEC、OFF 后设置及完整热稳资格、独占通信、读回／正式守卫和原清理。
只有 CLD1010LP 沿用其既有首个 ON 后 [-0.4 mV,0) 的一次两秒复核；CLD1015
对负电压立即失败，等待操作者另行取得实机证据。连接故障／未知仍阻断开光。

命令依据：Thorlabs《CLD101x User Guide》DOC-104674 Rev. B（2026-05-18），
§4.2 共用 TL4000 驱动、§4.3.2 LD/TEC source/output/sense 命令及 §4.3.3 状态位。
当前为本地离线适配；目标部署和真实仪器验收须分别记录。

TL4000 SDK 即使以 `reset=false` 初始化，仍会隐式发送
`*CLS;*SRE 0;*ESE 0;:STAT:PRES`，getter 也可能消费错误队列。因此 SDK 初始化
不能作为只读发现；必须同时有明确连接和写入授权，并在加载 DLL／调用 init 前检查。
正常已授权的 Combination 运行接口不变。只读核查应采用独立明确授权的 USB SCPI
查询方案，避免清状态／消费错误队列；本次离线验证不调用真实 DLL 或仪器。

## 功率判稳、重测与时间成本

`target_tolerance_w/fraction` 为初始目标验收和正式偏差判据（二选一）。可选
`tuning_tolerance_fraction` 用较紧容差调近中心；省略保持旧版精度。
`max_peak_to_peak_w` 是功率窗绝对上限；可选相对限值与噪声底采用
`min(绝对上限,max(噪声底,目标功率×相对限值))`，可另限每秒相对漂移。
这些选项可能增加单点资格时间，不能保证一定提速。

三个间隔分别属于不同阶段：`power_feedback.window.sample_interval_s` 是调功窗口间隔；
`optical_scan.sample_interval_s` 是照光 dwell 守护间隔；锁相间隔来自时间常数倍数。
`nkt_run.dwell_s`／`cld1010.dwell_s` 表示初始功率合格后的完整照光保持时间，之后锁相
采样仍保持照光；该明确要求的保持时间不会因等待优化被擅自删去。

`target_deviation_policy="record_continue"` 在正式阶段记录近似目标与实际功率，不重写
目标，也不因为一般偏差主动调电流。`max_power_retries=0` 默认不自动恢复；显式设为
1–3 时，仅普通功率偏差进入共享期限内重调，并完整重测 before／所有模块／after
窗口，不能把旧 XX 与新 XY 拼接。混合通信、设置、身份、参考或物理保护故障保持
严格失败。原始失败窗口全部保存、默认合格分析排除。

## 匹配暗背景

`optical_background.enabled=true` 当前只支持 internal-XX 双 SR830。
`capture_mode="per_condition"`（省略默认）在每个条件先关闭激光、保留参考／斩波，
采 `samples` 个完整读数，再开光／调功／等待；连续 gate 模式也逐条件增加这项开销。
`capture_mode="once_before_scan"` 仅在首个条件正式照光采样前采一组，后续复用。
背景超龄或 gate／温场／激励／参考条件变化时，该差分标记无效，继续保存原始光照。
不会自动补测 dark。新 run 重新采集，不能凭一次背景保证整个扫描没有漂移。

暗背景记录 role、harmonic、频率、相位上下文、gate／T／B 条件与访问、样品连接声明、
采集时间及原始有效性。按各通道判断，单路无效不排除另一路合格差分。
计算为 `ΔX+iΔY=(Xlight+iYlight)-mean(Xdark+iYdark)`，再推导 ΔR 和 Δphase；
不会直接相减 R 或角度。原始光／暗读数独立保留。相位／参考／访问上下文变化需要
新背景，不能复用旧值遮盖漂移。

## 结束、审计和验收

正常／异常结束均先核验激光 OFF，再保护电学输出，最后处理斩波器／PEM 和关闭资源。
斩波器 `enable=0` 表示停止转动，停住的孔可能透光，不能认证关光。manual backend
只有用户声明，无实测频率／PLL 证明，其清理需要人工核验。

CLD 第一阶段仅关 LD、回零并读取实际状态；VISA 连接及资源管理器在电学保护
尝试完成后才关闭，避免共享管理器提前关闭而影响 XX／XY。关闭错误仍单独记录，
不能用已确认 LD OFF 覆盖资源关闭失败。

原始点位、PM 读数、控制器通信、失败／部分响应和最终保护保存在 `optical_audit`
一次性原始事件；样本和清理摘要用明确引用定位原文，不反复复制完整历史。导出／
搬移数据时保留完整 SQLite；独立无事件存储的调用者由返回结果携带原始 audit。
默认分析仍排除 rejected／invalid 数据，不能把 record_continue 当成合格认证。

离线验证不打开硬件：

```powershell
python -s tools/run_guarded_tests.py tests.test_chopper tests.test_internal_optical_combination tests.test_photonics_chopper_template
```

进入实机阶段前仍须确认 f0、完整型号、刀片和照光轨道；先只读身份／读回，再进行
激光 OFF 的最小启停与完整清理验收，最后才是经用户确认的小规模照光对照。
斩波器的厂家命令、刀片频率表和协议边界见 `docs/modules/CHOPPER.md`。
