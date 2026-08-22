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

## 开始前

先在断开扫描的状态下核对本机 `hardware.local.toml`，再核对实物接线：

- `lockin_xx` 是内部参考和唯一的激励源；`lockin_xy` 是外部 TTL 参考。
- `lockin_xy.sine_output_connected = false` 代表 XY 的 SINE OUT **实际物理断开**。
  TOML 记录这项站点事实，但不能替代目视/接线检查。
- 外部串联电阻、器件近似电阻、器件 RMS 电流/电压上限，以及“没有外部 50 Ω
  端接”的事实应与当天线路一致。
- 两台 SR830 应先回到 h1、17.777 Hz、4 mVrms 基线。扫描的预检会读取并检查
  参考角色、锁定、过载、错误状态、频率、h1 和最小 SINE OUT；失败时不会开始扫描。

这里不再要求每次输入 `--authorize-writes`、
`--confirm-xy-sine-disconnected` 或
`--confirm-no-50ohm-termination`。这不改变上述物理检查的必要性：两条 sweep
命令本身会写入允许的 SR830 扫描设置，并在每次写入后读取确认；其他独立的
commissioning 命令仍保留各自的显式授权门。

## `[lockin_sweep]` 字段

所有日常扫描设置都在同一张 TOML 表中：

| 字段 | 含义与当前站点默认值 |
| --- | --- |
| `frequency_points_hz` | 扫频的基频点。当前为 17.777 Hz 到 100 kHz 的 10 个对数等距点。 |
| `excitation_points_v_rms` | 扫幅的 XX SINE OUT RMS 电压。当前为 4 mV 到 400 mV 的 11 点，基频固定为 17.777 Hz。 |
| `harmonics` | 每个正式点的检测阶次。当前严格为 `[1, 2, 3]`。 |
| `skip_unsupported_harmonics` | 若 h2/h3 的检测频率超过 SR830 的 102 kHz 范围，保留可测阶次并在 JSON 写入 `skipped_harmonics`；当前为 `true`。 |
| `temporary_xx_sensitivity_full_scale_v` | 扫描期间 XX 临时量程，当前 20 mV；结束时恢复预检读到的 XX 量程。XY 始终使用其 `lockin_xy` 中固定的 1 mV。 |
| `settle_s`、`samples_per_point`、`sample_interval_s` | 过渡/谐波切换后的等待时间、每点正式样本数、样本间隔。当前是 1.5 s、3、0.3 s；每次实际 `SLVL` 改变等待两个 `settle_s`。 |
| `external_series_resistance_ohm`、`approximate_device_resistance_ohm` | 电流换算使用的外串联电阻和近似器件电阻；SR830 固有 50 Ω 输出阻抗会自动加入。当前为 100 kΩ 和 500 Ω。 |
| `max_device_current_a_rms`、`max_device_voltage_v_rms` | 扫幅的 fail-closed 器件 RMS 上限。当前为 5 mA 和 0.5 V。 |
| `external_50_ohm_termination` | 当前线路必须为 `false`；严格加载器拒绝 `true`。 |
| `output_directory` | 记录目录，相对 `hardware.local.toml`。默认 `../run_data/commissioning` 假定 TOML 位于 `config/`，所以落在仓库根的 `run_data/commissioning/`，与两个分析 Notebook 的默认目录一致。 |

`lockin_xx.source_voltage_v` 与 `lockin_xy.source_voltage_v` 仍属于各自的
Lock-in 配置。日常扫描要求它们均为 SR830 的 4 mVrms 最小输出；扫频名义电流和
扫幅横坐标使用 XX SINE OUT 的已解析/读回电压，而不是手工输入的电流值。

## 记录、状态和分析

在打开 VISA 资源前，程序会先创建并检查记录目录。每次已开始的扫描都会以 UTC
时间、扫描种类和结果状态原子写入一个 JSON，例如：

```text
run_data/commissioning/20260822T123456123456Z_frequency_completed.json
run_data/commissioning/20260822T123456123456Z_excitation_rejected.json
```

状态含义如下：

- `completed`：全部正式样本完成，且 cleanup 最终读回通过。
- `rejected`：预检、正式样本或 cleanup 出现不安全/不匹配/通信错误。
- `interrupted`：收到中断后已尝试 cleanup。

每个 JSON 都带有地址无关的 `measurement_config`：其中是本次解析后的 TOML
请求配置、扫描点、量程、时序和激励路径；实际 SR830 读回位于同文件的
`preflight`、每点记录和 `cleanup`。因此不要把 `measurement_config` 单独当作
硬件读回证据。若归档写入失败，命令以失败退出，避免把未保存的数据误报为完成。

打开 [`../notebooks/sr830_commissioning_sweeps.ipynb`](../notebooks/sr830_commissioning_sweeps.ipynb)
即可在开头使用 Browse 按钮选择文件，并按 `completed`/`rejected` 状态筛选。
默认图只使用 completed 记录和 clean 正式样本；过渡和 cleanup 记录始终保留，
但不会混入正式曲线。

## 出错后

程序会尝试将 XX 恢复为 4 mVrms、h1、17.777 Hz，并恢复预检时的 XX 灵敏度。
如果 JSON 为 `rejected` 或命令异常退出，不要仅依赖软件消息；在断开器件或继续
下一轮之前，手动确认两台前面板的参考状态、h1、XX 输出、频率和量程。通信失败时，
不得推断仪器已回到安全状态。
