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
参数固定为占用率阈值 0.85、缩窄前连续 2 个稳定样本、每条连续扫描最多 1 次调整；
范围只能使用项目已确认的相邻 1 mV→10 mV 或 10 mV→20 mV 档位。

推荐的显式自动策略是 XX `10 mV → 20 mV`、XY `1 mV → 10 mV`。XY 的 SINE OUT
无论量程模式如何都必须保持物理断开。自动判断、转换、读回和锁存状态会写入审计
JSON。只要任一角色启用自动模式，每个扫描点会在正式 h1/h2/h3 采样前保留一组顺序
h1 判定读数；状态跨同一条连续扫描保留，正式样本不会把判定或转换期数据混入曲线。
默认 fixed 模式不因本次功能而自动改为 autorange。

## `[lockin_sweep]` 字段

日常扫描的扫频、扫幅、安全、时序和审计设置在同一张 TOML 表中；两台仪器的量程
模式和范围策略只在上述各自的 Lock-in 表中设置：

| 字段 | 含义与当前站点默认值 |
| --- | --- |
| `frequency_points_hz` | 扫频的基频点。当前为 17.777 Hz 到 100 kHz 的 10 个对数等距点。 |
| `excitation_points_v_rms` | 扫幅的 XX SINE OUT RMS 电压。当前为 4 mV 到 400 mV 的 11 点，基频固定为 17.777 Hz。 |
| `harmonics` | 每个正式点的检测阶次。当前严格为 `[1, 2, 3]`。 |
| `skip_unsupported_harmonics` | 若 h2/h3 的检测频率超过 SR830 的 102 kHz 范围，保留可测阶次并在 JSON 写入 `skipped_harmonics`；当前为 `true`。 |
| `run_name` | 本次数据的非空短名称（最多 80 个字符）。它进入 JSON 文件名；不可使用 `\ / : * ? " < > |` 等路径或 Windows 保留字符。可使用中文。 |
| `note` | 本次运行的非空审计备注（最多 2000 个字符），例如样品、接线改动或测试目的。它写入 JSON，但不进入文件名。 |
| `settle_s`、`samples_per_point`、`sample_interval_s` | 过渡/谐波切换后的等待时间、每点正式样本数、样本间隔。当前是 1.5 s、3、0.3 s；每次实际 `SLVL` 改变等待两个 `settle_s`。 |
| `external_series_resistance_ohm`、`approximate_device_resistance_ohm` | 日常扫描激励路径的唯一可变电阻来源：外串联电阻和近似器件电阻；SR830 固有 50 Ω 输出阻抗会自动加入。当前为 100 kΩ 和 500 Ω。每次扫描会把两者、50 Ω 和总阻抗写入 JSON 的 `measurement_config.excitation_path`，供之后分析使用。 |
| `max_device_current_a_rms`、`max_device_voltage_v_rms` | 扫幅的 fail-closed 器件 RMS 上限。当前为 5 mA 和 0.5 V。 |
| `external_50_ohm_termination` | 当前线路必须为 `false`；严格加载器拒绝 `true`。 |
| `output_directory` | 记录目录，相对 `hardware.local.toml`。默认 `../run_data/commissioning` 假定 TOML 位于 `config/`，所以落在仓库根的 `run_data/commissioning/`，与两个分析 Notebook 的默认目录一致。 |

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
激励路径。实际 SR830 读回位于同文件的 `preflight`、`sensitivity_setup`、每点记录
和 `cleanup`。因此不要把 `measurement_config` 单独当作硬件读回证据。若归档写入
失败，命令以失败退出，避免把未保存的数据误报为完成。

分析电流时，Notebook 和 Python API 默认只使用该 JSON 中归档的
`measurement_config.excitation_path`，不会从当前的 `hardware.local.toml` 重新读取或
要求再填一套电阻。这样历史文件保留其实际使用的换算标尺。只有早于该字段的旧 JSON
才需在 Notebook 的 `EXCITATION_PATH_OVERRIDE` 显式填写完整路径；它是只读分析覆盖，
不改变本次或下次扫描的安全配置。若同时选择的文件归档了不同路径，Notebook 会拒绝把
它们混成同一条电流曲线。

打开 [`../notebooks/sr830_commissioning_sweeps.ipynb`](../notebooks/sr830_commissioning_sweeps.ipynb)
即可在开头使用 Browse 按钮选择文件，并按 `completed`/`rejected` 状态筛选。
默认图只使用 completed 记录和 clean 正式样本；过渡和 cleanup 记录始终保留，
但不会混入正式曲线。

## 出错后

程序会尝试将 XX 恢复为 4 mVrms、h1、17.777 Hz，并仅在扫描曾改变它们时恢复预检
时的 XX 和 XY 灵敏度。自动量程或固定量程转换中出现的失锁、错误、input/filter
overload 或未许可的 output overload 都会导致本次记录被拒绝；只有已记录的缩窄转换
可消费该角色的 `LIAS=4`，且随后验证必须干净。若 JSON 为 `rejected` 或命令异常退出，
不要仅依赖软件消息；在断开器件或继续下一轮之前，手动确认两台前面板的参考状态、
h1、XX 输出、频率和量程。通信失败时，不得推断仪器已回到安全状态。
