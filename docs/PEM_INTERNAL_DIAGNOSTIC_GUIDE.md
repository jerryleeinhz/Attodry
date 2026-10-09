# PEM 内参考近似解调诊断

本流程使用 XY SR865A 的内部参考，观察 PEM 调制信号与内部参考的差频。
它提供单台 XY 的暗态/照光缓冲数据，用于判断是否存在可辨认的 PEM 调制响应。
现有 `combination_cli` 的正式光电扫描、参考检查和原始失败记录保留原有行为。

本次开发目标是代码与 fake/模拟验证；真实设置写入、照光采集和实机数据资格需分别记录，
不能由模拟通过推断。仪器命令依据
[SR865A 官方手册](https://www.thinksrs.com/downloads/pdfs/manuals/SR865Am.pdf)。

## 1. 接线与接管条件

- XY 是 SR865A，A/B 仍接样品电压测量端；本诊断不把电压自动换算成光电流。
- **XX SR830 的 SINE OUT 必须从样品激励线路实际断开。**
  SR830 最低输出是 4 mV，软件不能通过设置 0 V 关闭该输出。
  程序仍连接 XX，确认并保护它处于不高于 4 mV、DC=0 的设置。
- XY SINE OUT+ 可保留连接到 XX REF IN，XY 输出幅度和 DC 设置只验证并保持，
  不增加样品激励，也不将 XY 输出连接到样品。
- PEM REF OUT 到 XY REF IN 的线可以保留。XY 进入内部参考模式后，不依赖这条线锁定。
- 激光光路、功率计位置和样品允许的光功率须沿用统一站点配置的确认结果。
- SR865A 捕获缓冲不得包含需要保留但尚未下载的数据，也不得正由其他任务占用。
  `capture_buffer_available` 必须根据真实状态确认，不能仅为通过校验填写 `true`。
- 运行时不要同时启动 NKT CONTROL、直接查询仪器的 live monitor 或另一个采集进程。
  本诊断的 `monitor` 只读取保存的文件。

配置声明记录操作者对接线和缓冲可用性的确认；软件不能检测样品激励线是否物理拔出。
在接管前保存身份、参考、谐波、频率、滤波、量程和输出设置；未知状态或通信失败阻止采集。

## 2. 配置与源码目录

光学设备、XX/XY 的地址、功率反馈和保护仍来自统一的站点 TOML。
诊断配置通过 `station_config` 引用它；也可以把诊断表放进同一张站点 TOML，
省略 `station_config` 时使用同一文件。复用现有站点设置避免复制出另一套电流或光功率上限。

从 worktree 根目录复制可跟踪的模板，实际地址及实验记录保存在 ignored local 配置中：

```powershell
Copy-Item config/pem_internal_diagnostic.example.toml config/pem_internal_diagnostic.local.toml
```

按模板填写相对的 `station_config`，并核对接线、缓冲可用性及点表。
相对配置路径以诊断 TOML 所在目录解析；复制到另一个目录后需要重新核对引用。
不要把本机绝对 Windows 地址或未经确认的硬件地址放进可跟踪的示例文件。

`[pem_internal_diagnostic]` 的模板字段如下。数值和跨字段限值由严格 loader 验证，
未知或拼错字段不自动忽略。

| 字段 | 示例值/含义 |
|---|---|
| `schema_version` | `1`，本诊断配置契约版本 |
| `station_config` | `"photonics.local.toml"`，统一站点配置的相对路径 |
| `run_name` / `note` | 运行名称和实验备注 |
| `output_directory` | `"../run_data/pem_internal_diagnostic"`，相对诊断配置的输出根目录 |
| `optical_point_indices` | `[0]`，`nkt_run.points` 的零起点行索引；`[0, 1]` 可选择两项 |
| `internal_frequencies_hz` | `[50000.0]`，明确指定的内部参考频率；受站点参考安全区间限制 |
| `include_pem_frequency` | `true`，增加本次直接观察到的 PEM 频率附近的比较点 |
| `time_constants_s` | `[0.001, 0.0001]`，1 ms 与 100 µs；须属于 SR865A 支持表 |
| `harmonics` | `[1, 2]`，比较 PEM 基频与二次谐波 |
| `capture_duration_s` | `30.0`，每段仪器缓冲采集的目标时长 |
| `minimum_capture_rate_hz` | `1000.0`，要求的最低缓冲采样率；实际选择值可能更高 |
| `capture_timeout_s` | `45.0`，采集有界期限，须容纳目标时长及状态查询耗时 |
| `monitor_interval_s` | `1.0`，缓冲采集期间慢速保护查询的目标间隔，允许 0.1–5 秒 |
| `pem_frequency_observation_s` | `60.0`，准备后的频率观察时长；`0.0` 可关闭额外观察窗口 |
| `xx_excitation_disconnected` | 模板为 `false`；实际移除 XX 样品激励线后才可填写 `true` |
| `capture_buffer_available` | 模板为 `false`；确认可替换缓冲且无 armed/active capture 后才可填写 `true` |

`false` 的接线/缓冲声明允许离线查看计划，禁止真实采集。
运行时保存配置的原始快照和最终解析值，不用后续编辑的 TOML 重新解释旧数据。
SR865A 的 XY 捕获受 4 MiB 仪器缓冲限制；时长、自动提高后的采样率和二通道
float32 大小共同决定能否容纳。确定超过上限的配置离线拒绝，实际硬件采样率的
量化结果仍须在启动缓冲前读回检查。

LK_setup 使用已有 Conda `lyr` 解释器。每个终端先进入当前 worktree 根目录，再确认导入路径：

```powershell
conda activate lyr
$env:PYTHONPATH = (Resolve-Path -LiteralPath ./src).Path
python -s -c "import sys, attodry_control; print(sys.executable); print(attodry_control.__file__)"
```

`attodry_control.__file__` 应位于当前 worktree 的 `src` 内。
共享环境中的另一份 editable 安装可能指向旧 checkout，环境名相同不能证明源码相同。

## 3. 诊断点与执行顺序

初始光学点选择站点点表的第一项：630 nm、10 nm 带宽、目标功率 10 µW。
扩展多点时仍引用站点点表及对应的功率目标，不重新解释目标和初始电流。
每个点复用原来的电流步长、最大电流、最低有效照光、功率上限、目标容差、
反馈稳定窗口和等待预算。不会为了获得信号而自动放宽这些限值。

执行顺序为：

1. 查询所有启用设备的身份与状态，确认 NKT OFF 和 XX 的输出保护，保存 XY baseline。
2. 激光保持 OFF，准备波长、PEM 幅度和功率计；等待 PEM 的稳定读回和启用 ACK。
3. 进行约 60 秒的 PEM 频率观察，记录每次读回的真实时戳、频率和稳定状态。
   平均值用于后续“靠近实际 PEM”的内部频率点。
4. 比较内部参考 50,000 Hz 与本次 PEM 频率均值附近的频率；分别检测 h1、h2，
   比较 1 ms 与 100 µs 时间常数，采用普通 RC、24 dB/oct。
   h1 的检测频率等于内部参考，h2 的检测频率等于内部参考的两倍。
5. 每种设置先在 NKT OFF、PEM 正常调制时采集暗态；再按原反馈达到光功率目标，
   完成稳定和 dwell 后采集照光数据。每段默认约 30 秒。
6. 设置切换前先确认激光 OFF。采集中保持设置和电流，不执行自动调功；
   周期性保存 PEM、NKT、功率计和锁相状态证据。
7. 最终先确认激光 OFF，再验证 XX 保护、恢复 XY baseline，最后结束 PEM 和关闭资源。

频率“均值附近”不是相位锁定。PEM 与内部时钟仍可能漂移；观察读回恒定也不证明真实
周期没有抖动，仪器回报的分辨率和语义限制必须保留。

## 4. 缓冲采样与状态监控

X/Y 使用 SR865A 内部缓冲采集，下载后逐点计算 R。
把 `sample_interval_s` 写成 `0.001` 不能代替仪器缓冲，也不能保证软件查询达到 1 kHz。
程序根据实际差频、所选时间常数和仪器支持的采样率提高捕获采样率，并保存实际读回；
这用于避免明显混叠，不代表全部带外噪声已完全去除。

设置、起停时间、捕获通道、缓冲长度、实际采样率、有效样本数和下载异常均保留。
图中等间隔采样时间由实际仪器采样率推导；软件状态查询的时戳单独保存，不能假定
每个缓冲样本都对应一次新的功率或 PEM 查询。
频率 guard 可能包含启动前和停止后的查询；这些查询的均值与范围是相邻观测的摘要，
不能当作逐个缓冲样本同时测得的 PEM 频率，差频预测也应按近似值解释。

保护查询目标间隔约 1 秒。串行 I/O 会产生实际延迟，保存真实间隔和最后确认状态；
长时间没有新证据不能描述为持续确认正常。功率离开目标、PEM 不稳定、设置改变、
通信/未知状态或采集失败时保留部分缓冲与原始异常，进入有界清理。
暗态不应用照光的最低功率和目标判据；以 NKT OFF 读回验证暗态。

## 5. 命令

先离线检查配置及最终解析的矩阵：

```powershell
python -s -m attodry_control.pem_internal_diagnostic describe --config config/pem_internal_diagnostic.local.toml
```

使用 fake 设备和合成数据验证整个流程：

```powershell
python -s -m attodry_control.pem_internal_diagnostic simulate --config config/pem_internal_diagnostic.local.toml
```

在实际断开 XX 样品激励线、确认缓冲及光路后，真实运行命令为：

```powershell
python -s -m attodry_control.pem_internal_diagnostic run --config config/pem_internal_diagnostic.local.toml --authorize-optical --confirm-optical-route
```

这会切换 XY 内参考、配置滤波/谐波、准备 PEM，并在目标条件合格后照光。
未满足接线声明、配置、身份、接管或授权条件时不得进入写入/发光阶段。

在另一个终端，将下面的路径替换为运行输出显示的实际目录：

```powershell
python -s -m attodry_control.pem_internal_diagnostic monitor --run-directory run_data/pem_internal_diagnostic/RUN_DIRECTORY
```

采集结束后进行离线分析与绘图：

```powershell
python -s -m attodry_control.pem_internal_diagnostic analyze --run-directory run_data/pem_internal_diagnostic/RUN_DIRECTORY
python -s -m attodry_control.pem_internal_diagnostic plot --run-directory run_data/pem_internal_diagnostic/RUN_DIRECTORY
```

`describe`、`monitor`、`analyze` 和 `plot` 不连接硬件。
使用 `--help` 查看当前版本实际参数。运行目录保存配置快照、原始缓冲、状态证据、
进度、失败及清理记录；默认保留 rejected/interrupted 的原始资料。
`result.json` 保存进度和终态，`events.jsonl` 保存逐项状态证据；
每段原始 `capture-*.csv` 包含样本索引、仪器采样时间、X/Y 和逐点 R，
分析与绘图使用新的输出文件/目录，保留输入文件的 SHA256 和图像清单。
分析同时检查整次运行的完成及清理状态；失败或清理未核验的运行保留原始数值，
增加运行层面的排除标记，图和清单明确显示失败/未核验状态。
监控文件状态不等同于确认进程存活；失联后不得另开采集或假定激光已经关闭。

## 6. 怎样分析近似解调信号

内部参考频率为 `f_int`，PEM 实际频率为 `f_PEM`，检测谐波为 `h` 时，
解调后的名义差频是：

```text
f_beat = h × (f_PEM − f_int)
```

例如 PEM 约 50,027 Hz、内部参考 50,000 Hz，h1/h2 对应约 27/54 Hz 的拍频。
应查看 X/Y 随时间、逐点 R、复数 `X + iY` 的频谱，并比较暗态和照光，以及改变
内部参考后拍频峰是否按预期移动。复数 FFT 的正负频率随软件符号约定解释，
其符号不是相对于 PEM 的光电流正负号。

先逐点计算 `R_i = sqrt(X_i² + Y_i²)` 再统计；先平均旋转的 X/Y 可能将响应抵消。
R 有正的噪声偏置，弱信号不能通过简单的照光平均 R 减暗态平均 R 得到无偏响应。
保留暗态分布、X/Y 噪声、频谱与不确定性，不把噪声背景自动解释为 PEM 信号。

普通四阶 RC 的差频幅度传输估算是：

```text
|H(f_beat)| = [1 + (2π × f_beat × tau)²]^−2
```

该值用于说明不同 TC 对拍频的衰减，分析不自动逆滤波、不放大高差频下的噪声。
漂移、非稳态和附加滤波会限制上述定常估算；ADVFILT/SYNC 的实际状态必须随记录保存。

本方法不恢复相对于 PEM 的相位或响应正负号，不代替外参考锁定，也不具备正式
双路 Hall 数据资格。输出应明确标为内部参考诊断；模拟结果标为 simulated，
不能混入正式 accepted-only 的 Vxx/Vxy 分析。

## 7. 清理与恢复边界

正常完成、异常和 Ctrl+C 使用同一清理顺序。保存 primary error 与每个清理/关闭错误，
部分数据不会因最终状态恢复而被改写为有效。

- 激光 OFF 必须由最终读回确认；发出 OFF 命令不是确认关闭。
- XX 保持不高于 4 mV 和 DC=0；物理断开是这次无交流样品激励的依据。
- XY 恢复记录的参考、内部频率、谐波和滤波等已改变设置；其 SINE/DC 输出保持原值。
  恢复外参考后的频率锁定情况与设置恢复分别记录，不能把设置读回相同称作已锁住 PEM。
- PEM 收到 disable ACK 只证明协议应答，不证明光学头物理停振。
- 任一设置恢复、最终读回、资源关闭或审计失败都不得标为 clean；需要人工核验。

保留运行目录和原始证据后再排查。不要为让一次诊断通过而删除原 combination 的
通信、身份、设置、未知状态、输出、光功率或型号/谐波能力保护。
