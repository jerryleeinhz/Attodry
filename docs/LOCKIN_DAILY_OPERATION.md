# Dual-SR830 日常扫频与扫幅

本页是 Lock-in 日常数据采集入口。它只覆盖两条独立的设备扫描命令；不包含
attoDRY、PPMS、SMU 或旋转台控制。

日常扫描的单一参数来源是本机、被 Git 忽略的
`config/hardware.local.toml`。从仓库根目录运行：

```powershell
conda activate lyr
python -m attodry_control.lockin_test sweep-frequency
python -m attodry_control.lockin_test sweep-excitation
```

### 本机地址与版本更新

`hardware.example.toml` 刻意只保留 XX/XY 地址占位符。请只在本机、被 Git 忽略的
`config/hardware.local.toml` 填写实际 VISA 地址；后续 `git pull` 不会覆盖本机值。
日常命令会拒绝空地址、`CHANGE_ME` 地址或 XX/XY 相同的地址。

如果终端错误地显示旧版 sweep 的必填参数（例如
`--series-resistance-ohm` 或 `--authorize-writes`），该终端没有导入当前 checkout。先在
**同一终端**执行以下只读检查；它只改变当前终端的 `PYTHONPATH`，不会连接仪器：

```powershell
conda deactivate
conda activate lyr
Set-Location -LiteralPath "C:\Users\LK_Setup\Yuanrong Li\Attodry_control"
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
python -c "import sys, attodry_control.lockin_test as m; print(sys.executable); print(m.__file__)"
python -B -m attodry_control.lockin_test sweep-excitation --help
```

第二条输出必须指向当前 checkout 的
`src\attodry_control\lockin_test.py`；最后一条帮助只应显示 `--config`。验证后，在
同一个终端运行日常 sweep。新开 terminal 通常只需重新 `conda activate lyr`，无需重填
本机地址。

命令默认读取 `config/hardware.local.toml`。仅当配置文件确实位于其他位置时，
才使用 `--config <path>`；日常运行不需要填写电阻、量程、采样时间、扫描点、
阶次或确认旗标。

每次运行前，在同一份 `[lockin_sweep]` 表中修改 `run_name` 和 `note`。前者是
本次数据的简短名称并进入 JSON 文件名，后者记录样品、接线状态或本次测试目的，
只保存在 JSON 审计记录中。

若需要在没有 sweep 占用两台仪器时实时查看 XX/XY 的电压、相位、频率、量程和锁定
状态，运行：

```powershell
python -m attodry_control.lockin_test monitor-live --consume-status-latches
```

这是独立的只读面板；`--consume-status-latches` 会清除 `LIAS?`/`ERRS?` 锁存位，不能
与扫描并行运行。字段说明、停止方式和无锁存读取的含义见
[`LOCKIN_LIVE_MONITOR.md`](LOCKIN_LIVE_MONITOR.md)。

## 开始前

先在断开扫描的状态下核对本机 `hardware.local.toml`，再核对实物接线：

- `lockin_xx` 是内部参考和唯一的激励源；`lockin_xy` 是外部 TTL 参考。
- `lockin_xy.sine_output_connected = false` 代表 XY 的 SINE OUT **实际物理断开**。
  TOML 记录这项站点事实，但不能替代目视/接线检查。
- 外部串联电阻、器件近似电阻、器件 RMS 电流/电压上限，以及“没有外部 50 Ω
  端接”的事实应与当天线路一致。
- 每次运行前填写非空的 `run_name` 和 `note`；不要沿用示例中的
  `replace_before_run`。
- 两台 SR830 应先回到 h1、17.777 Hz、4 mVrms 基线。扫描的预检会读取并检查
  参考角色、锁定、过载、错误状态、频率、h1 和最小 SINE OUT；失败时不会开始扫描。

这里不再要求每次输入 `--authorize-writes`、
`--confirm-xy-sine-disconnected` 或
`--confirm-no-50ohm-termination`。这不改变上述物理检查的必要性：两条 sweep
命令本身会写入允许的 SR830 扫描设置，并在每次写入后读取确认；其他独立的
commissioning 命令仍保留各自的显式授权门。

## 量程策略：`[lockin_xx]` 与 `[lockin_xy]`

每台 SR830 都有独立的 `sensitivity_mode`。默认配置是 **fixed**：XX 为 20 mV，
XY 为 1 mV。固定模式在扫描开始时确认目标量程，只有预检读回不同才写入；若扫描
改变过该角色的量程，cleanup 会恢复预检时的原量程。

只有明确把某一台的 `sensitivity_mode` 改为 `"bounded_auto"` 时，才启用该角色的
受限自动判断。自动模式必须同时填写五个 `autorange_*` 字段，且
`sensitivity_full_scale_v` 必须等于 `autorange_min_full_scale_v`。当前确认的控制
参数固定为占用率阈值 0.85、缩窄前连续 2 个稳定样本。XX 可使用受控三档阶梯
10 mV→20 mV→50 mV，故每条连续扫描最多 2 次转换；XY 仍只能使用 1 mV→10 mV，
最多 1 次转换。

推荐的显式自动策略是 XX `10 mV → 20 mV → 50 mV` 或 XY `1 mV → 10 mV`。XY 的
SINE OUT 无论量程模式如何都必须保持物理断开。自动判断、转换、读回和锁存状态会写入
审计 JSON。只要任一角色启用自动模式，每个扫描点会在正式 h1/h2/h3 采样前保留一组
顺序 h1 判定读数；状态跨同一条连续扫描保留，正式样本不会把判定或转换期数据混入曲线。
默认 fixed 模式不因本次功能而自动改为 autorange。

## 严格 Lock-in 配置参考

以下字段位于 `[lockin_xx]` 与 `[lockin_xy]`。这是当前日常 sweep 支持的**完整**
取值范围，不应按 SR830 前面板的其他可选值自行扩展；严格加载器会在连接 VISA 前拒绝
未知或不受支持的值。

| 字段 | 可填写值与角色约束 |
| --- | --- |
| `model` | 必须为 `"SR830"`。 |
| `address` | 非空 VISA 字符串；XX 与 XY 必须不同，且不能含 `CHANGE_ME`。示例只给占位符；实际地址只写入忽略的本机 `hardware.local.toml`。 |
| `reference_source` | XX 严格为 `"internal"`；XY 严格为 `"external_ttl"`。 |
| `external_reference_edge` | 仅 XY 必填，且只能是 `"rising"`；XX 不可出现此字段。 |
| `sine_output_connected` | XX 必须 `true`；XY 必须 `false`，表示 XY SINE OUT 已**物理断开**。 |
| `source_voltage_v` | 配置层允许 0.004--5.0 Vrms；日常频率/幅值扫描均要求两台基线为 `0.004`（4 mVrms）。 |
| `frequency_hz` | 正数且不超过 102000 Hz；XX 与 XY 必须相同。日常基线为 17.777 Hz。 |
| `input_mode` | 只能是 `"a_minus_b"`。 |
| `shield_grounding` | 只能是 `"float"`。 |
| `input_coupling` | 只能是 `"ac"`。 |
| `time_constant_s` | 当前项目只能是 `0.3` s。 |
| `filter_slope_db_oct` | 当前项目只能是 `24`。 |
| `sensitivity_mode` | 只能是 `"fixed"` 或 `"bounded_auto"`（拼写必须完全一致）。 |
| `sensitivity_full_scale_v` | 可填 `0.001`、`0.010`、`0.020` 或 `0.050` V。日常 fixed 默认：XX `0.020`、XY `0.001`。在 `bounded_auto` 中它必须等于最小量程。 |
| `settle_time_constants` | 有限数且至少 `5.0`。每次设置转换的 `settle_s` 必须不小于两台仪器中最大的 `time_constant_s × settle_time_constants`；当前 `0.3 × 5.0 = 1.5` s。该下限会在打开 VISA 前检查并归档。 |

`fixed` 模式只保留 `sensitivity_mode` 和 `sensitivity_full_scale_v`；所有
`autorange_*` 字段必须完全不存在（继续注释），并非“被忽略”。例如：

```toml
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.020  # XX daily default; XY daily default is 0.001
```

`bounded_auto` 模式必须同时提供以下五个字段。XX 可填 10--20 mV、20--50 mV，或
三档 10--20--50 mV；XY 唯一允许 1--10 mV。占用率和连续样本数是固定安全策略，
`autorange_max_steps` 则必须恰好等于该阶梯的实际转换数，不能任意修改。

XX 的日常三档策略如下。`WIDEN` 每次只上移一个项目确认档位：第一次 10→20 mV，
第二次 20→50 mV。两次转换用尽后，即使信号继续增大也不会再换档；50 mV 仍过载时
扫描失败并执行安全 cleanup。`NARROW` 同样一次只下移一档，并计入同一个两次总额度，
所以一次扫描不会来回追逐量程。

```toml
# XX bounded_auto：三档 10 mV -> 20 mV -> 50 mV。
sensitivity_mode = "bounded_auto"
sensitivity_full_scale_v = 0.010
autorange_min_full_scale_v = 0.010
autorange_max_full_scale_v = 0.050
autorange_target_occupancy = 0.85
autorange_stable_samples = 2
autorange_max_steps = 2
```

```toml
# XY bounded_auto
sensitivity_mode = "bounded_auto"
sensitivity_full_scale_v = 0.001
autorange_min_full_scale_v = 0.001
autorange_max_full_scale_v = 0.010
autorange_target_occupancy = 0.85
autorange_stable_samples = 2
autorange_max_steps = 1
```

`settle_time_constants = 5.0` 的含义是每次改变频率、谐波、SINE OUT 或量程后，至少等待
五个锁相时间常数再继续；它不是另一个毫秒数。`[lockin_sweep].settle_s` 是实际使用的
单个等待 interval，必须大于或等于上述下限。幅值扫描中每个实际 SINE OUT 改变等待两个
interval，因此当前是 `2 × 1.5 = 3.0` s。

## SR830 全部硬件量程与项目安全白名单

下表是 SR830 在**电压输入**下的全部 `SENS` 档位；本项目的 A-B 电压测量使用这一列。
它与 SINE OUT 输出幅值、器件允许电流/电压是三个不同概念。厂家规定的编号与量程见
[SRS SR830 手册的 `SENS` 命令表](https://www.thinksrs.com/downloads/PDFs/Manuals/SR830m.pdf)。

| `SENS` | 电压 full scale | 当前日常 sweep 项目白名单 |
| ---: | ---: | --- |
| 0 | 2 nV | 否 |
| 1 | 5 nV | 否 |
| 2 | 10 nV | 否 |
| 3 | 20 nV | 否 |
| 4 | 50 nV | 否 |
| 5 | 100 nV | 否 |
| 6 | 200 nV | 否 |
| 7 | 500 nV | 否 |
| 8 | 1 µV | 否 |
| 9 | 2 µV | 否 |
| 10 | 5 µV | 否 |
| 11 | 10 µV | 否 |
| 12 | 20 µV | 否 |
| 13 | 50 µV | 否 |
| 14 | 100 µV | 否 |
| 15 | 200 µV | 否 |
| 16 | 500 µV | 否 |
| 17 | 1 mV | 是：XX/XY fixed；XY auto 下界 |
| 18 | 2 mV | 否 |
| 19 | 5 mV | 否 |
| 20 | 10 mV | 是：XX/XY fixed；XX auto 下界；XY auto 上界 |
| 21 | 20 mV | 是：XX/XY fixed；XX auto 上/下界 |
| 22 | 50 mV | 是：XX/XY fixed；XX auto 上界（通常仅建议 XX） |
| 23 | 100 mV | 否 |
| 24 | 200 mV | 否 |
| 25 | 500 mV | 否 |
| 26 | 1 V | 否 |

“否”并不表示 SR830 前面板不能使用该档，而是日常 sweep 的严格 TOML 和驱动目前不会
写入它。这样可以避免一次本机配置修改无意中扩大已验证的自动范围。

### 50 mV 的正确写法

50 mV 必须写成 `0.050`，单位是 V full scale，不是 `50`。若只需要固定的额外 h1
余量，在 `[lockin_xx]` 使用：

```toml
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.050
```

若希望从较小信号开始并在同一扫描中处理两次量程压力，在 `[lockin_xx]` 使用上一节的
10 mV→20 mV→50 mV 完整 `bounded_auto` 区块。它不会从 10 mV 直接跳到 50 mV；每个
`WIDEN` 只移动一个项目确认档位。

50 mV 仅增加**输入测量 full scale**，不会提高 5 V SINE OUT 上限，也不会放宽
`[lockin_sweep]` 中的器件电流、电压或串联电阻保护。对于当前 2 V 扫幅记录，XX h1
约为 17 mV；20 mV 已接近 0.85 阈值但未过载。固定使用 50 mV 会留出余量，却会降低
二、三阶的量程占用率，不能改善弱谐波相位。

### 将新的硬件档位纳入项目安全协议

日常操作者只应修改已列为“是”的 `hardware.local.toml` 值；不能通过新增 TOML 字段
绕过白名单。若将来确实需要把另一 SR830 硬件档位纳入日常 sweep，维护代码时必须同时：

1. 依据厂家手册，把“电压 full scale → `SENS` 代码”加入
   `src/attodry_control/sr830_settings.py` 的项目映射；
2. 若用于 `bounded_auto`，只增加一个角色适用的有序自动量程阶梯，并维持 0.85、2 个
   稳定样本；`autorange_max_steps` 必须等于该阶梯中允许的相邻转换数；
3. 更新严格配置验证、硬件/模拟模板、此表和阶段交接记录；
4. 先运行映射、配置和 fake-VISA 离线测试；实际仪器首次写入新档位前，再单独取得
   硬件写入授权并检查过载/读回/cleanup。

这四步是“修改项目允许最大量程”的安全协议；它不会由普通运行或一次 TOML 编辑自动
触发。

## `[lockin_sweep]` 字段

日常扫描的扫频、扫幅、安全、时序和审计设置在同一张 TOML 表中；两台仪器的量程
模式和范围策略只在上述各自的 Lock-in 表中设置：

| 字段 | 可填写值与约束 |
| --- | --- |
| `frequency_points_hz` | 非空、严格递增的数列；每项必须在 0.001--102000 Hz。版本库示例为 17.777 Hz 到 100 kHz 的 10 个对数点。 |
| `excitation_points_v_rms` | 非空、严格递增的数列；每项必须在 0.004--5.0 Vrms。版本库示例为 4--400 mVrms 的 11 点，基频固定为 17.777 Hz。 |
| `frequency_xx_harmonics` / `frequency_xy_harmonics` | 分别选择扫频正式曲线中的 XX/XY 谐波。每项为升序组合，只能含 1、2、3；`[]` 表示该角色不进入正式曲线。两项合起来至少选一个。 |
| `excitation_xx_harmonics` / `excitation_xy_harmonics` | 分别选择扫幅正式曲线中的 XX/XY 谐波；规则同上，且可与扫频不同。例：`excitation_xx_harmonics = []`、`excitation_xy_harmonics = [2]` 只输出 XY h2 曲线。 |
| `frequency_harmonics` / `excitation_harmonics` | 旧版兼容字段；每个列表会同样应用到 XX 和 XY。二者都必须是非空升序组合，且不可与四个角色专用字段混用。 |
| `harmonics` | 更旧的兼容字段；一个非空升序组合同时应用到两类扫描和两台仪器，且不可与任何新字段混用。新建或修改日常配置应使用四个角色专用字段。 |
| `skip_unsupported_harmonics` | 布尔值 `true` 或 `false`。为 `true` 时，超过 102 kHz 的 h2/h3 不写入仪器，而是在 JSON 写入 `skipped_harmonics`；日常高频扫描推荐 `true`。 |
| `run_name` | 非空、最多 80 个字符；不可含控制字符或 `\ / : * ? " < > |`。可使用中文，且进入 JSON 文件名。 |
| `note` | 非空、最多 2000 个字符且不可含 NUL；记录样品、接线改动或测试目的，写入 JSON 但不进入文件名。 |
| `settle_s` | 有限正数，至少 1.5 s，且必须不小于上节的 time-constant 下限。它是每个普通转换实际等待的 interval；每次实际 `SLVL` 改变等待两倍。 |
| `samples_per_point` | 整数，至少 1；版本库示例为 3。 |
| `sample_interval_s` | 有限数，至少 0 s；版本库示例为 0.3 s。 |
| `external_series_resistance_ohm` | 正数，单位 Ω；外部串联电阻。SR830 固有 50 Ω 输出阻抗会自动加入。 |
| `approximate_device_resistance_ohm` | 非负数，单位 Ω；允许为 `0`，但应填写当前可得的器件近似值。 |
| `maximum_device_resistance_ohm` | 非负数，单位 Ω，且不能小于 `approximate_device_resistance_ohm`；这是操作者确认的器件电阻上界，而不是平均值。扫幅的器件端电压上界按 `Vsine × Rdevice,max / (Rseries + 50 Ω + Rdevice,max)` 计算。高阻、断线或接触异常可能超过这个上界时，必须先更新该值；缺失或不满足约束会在打开 VISA 前失败。 |
| `max_device_current_a_rms` | 正数，单位 Arms；扫幅的 fail-closed 器件电流上限。 |
| `max_device_voltage_v_rms` | 正数，单位 Vrms；扫幅的 fail-closed 器件电压上限。 |
| `external_50_ohm_termination` | 当前接线严格只能是 `false`；加载器拒绝 `true`。 |
| `output_directory` | 非空相对目录，不能是 `.` 或绝对路径；解析后也必须位于允许的项目目录内。默认 `../run_data/commissioning` 假定 TOML 位于 `config/`，所以落在仓库根的 `run_data/commissioning/`。 |

`lockin_xx.source_voltage_v` 与 `lockin_xy.source_voltage_v` 仍属于各自的
Lock-in 配置。日常扫描要求它们均为 SR830 的 4 mVrms 最小输出；扫频名义电流和
扫幅横坐标使用 XX SINE OUT 的已解析/读回电压，而不是手工输入的电流值。
因此，扫频中的**激励幅值**是 `lockin_xx.source_voltage_v = 0.004`（4 mVrms），
扫描只改变 XX 内部参考频率，不改变 `SLVL`；它与两台仪器在各自 Lock-in 表中声明
的 **测量量程策略** 是不同概念。

## 记录、状态和分析

在打开 VISA 资源前，程序会先创建并检查记录目录。每次已开始的扫描都会以 UTC
时间、扫描种类和结果状态原子写入一个 JSON，例如：

```text
run_data/commissioning/20260822T123456123456Z_sample_A_frequency_completed.json
run_data/commissioning/20260822T123456123456Z_sample_A_excitation_rejected.json
```

状态含义如下：

- `completed`：全部正式样本完成，且 cleanup 最终读回通过。
- `rejected`：预检、正式样本或 cleanup 出现不安全/不匹配/通信错误。
- `interrupted`：收到中断后已尝试 cleanup。

每个 JSON 的根部都有 `run_metadata.name` 和 `run_metadata.note`，并在地址无关的
`measurement_config` 中保留同一份已解析 TOML：请求配置、扫描点、量程、时序和
激励路径。每个正式样本的 `selected_roles` 标出该阶数实际纳入曲线的 XX/XY；另一台
SR830 的同时读回仍完整保留并参与安全判决。实际 SR830 读回位于同文件的 `preflight`、`sensitivity_setup`、每点记录
和 `cleanup`。因此不要把 `measurement_config` 单独当作硬件读回证据。若归档写入
失败，命令以失败退出，避免把未保存的数据误报为完成。

分析电流时，Notebook 和 Python API 默认只使用该 JSON 中归档的
`measurement_config.excitation_path`，不会从当前的 `hardware.local.toml` 重新读取或
要求再填一套电阻。这样历史文件保留其实际使用的换算标尺。只有早于该字段的旧 JSON
才需在 Notebook 的 `EXCITATION_PATH_OVERRIDE` 显式填写完整路径；它是只读分析覆盖，
不改变本次或下次扫描的安全配置。若同时选择的文件归档了不同路径，Notebook 会拒绝把
它们混成同一条电流曲线。

打开 [`../notebooks/sr830_commissioning_sweeps.ipynb`](../notebooks/sr830_commissioning_sweeps.ipynb)
即可在开头设置一次数据目录、刷新远程记录列表并选择扫频、扫幅或两者，再按
`completed`/`rejected` 状态筛选。只选一种时只生成该种扫描的已选择 XX/XY×谐波图；未
选择的组合不会生成空图。加载后，扫描点
多选框会列出自动保留的点；选中可疑点并应用排除即可重画，不会改写原始 JSON。
默认图只使用 completed 记录和 clean 正式样本；过渡和 cleanup 记录始终保留，
但不会混入正式曲线。若开启可选导出，`selection_manifest.json` 会记录文件、筛选和
手动排除点，供复现同一张图。

## 出错后

程序会尝试将 XX 恢复为 4 mVrms、h1、17.777 Hz，并仅在扫描曾改变它们时恢复预检
时的 XX 和 XY 灵敏度。自动量程或固定量程转换中出现的失锁、错误、input/filter
overload 或未许可的 output overload 都会导致本次记录被拒绝；只有已记录的缩窄转换
可消费该角色的 `LIAS=4`，且随后验证必须干净。若 JSON 为 `rejected` 或命令异常退出，
不要仅依赖软件消息；在断开器件或继续下一轮之前，手动确认两台前面板的参考状态、
h1、XX 输出、频率和量程。通信失败时，不得推断仪器已回到安全状态。
