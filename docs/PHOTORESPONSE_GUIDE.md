# 光电压 / 光电流运行指南

本分支已完成可离线验证的软件实现：NKT/CLD 光源、6221 DC、2182A 新鲜读数、
2400 电压/电流测量、可选温磁场扫描、SQLite 审计与新尝试恢复。**硬件联合验收尚未完成。**
程序不会扫描总线、复位仪器或自动接管已开启的输出。工作边界见 [工作包](modules/PHOTORESPONSE.md)。

## 已知实验状态（2026-10-13）

- 2182A 输入由用户确认悬空；保留通信/配置通过及过载拒绝结果，暂不重复有效电压验收。
- 2400 实际连接底栅极（SiO2/Si）与 source，属于 `gate_bottom` 偏置/漏电角色。
  先前 0 V / 10 nA 尝试仍被拒绝，已确认回零、关闭、HIMP；该路径不能作为沟道光电流探测器。
- 6221 已批准的空载五点测试在 0 A 开启输出时达到 0.1 V compliance，尚未施加第一个
  4 pA 点。五点扫描未完成；已确认 0 A、输出关闭。转发 2182A 的 EOI 通信修复已验收。
- CLD 默认可用 `backend="visa"`，无需 TL4000 DLL；真实 USB 查询已验收。
  LD 上限已降至 30 mA。后续手动操作后的设定为 30 mA、LD 关闭；用户明确允许
  型号暂不确定，元数据须如实记录。
- 用户已确认光束收集装置。TEC-only 测试采用缓存目标/限流，配置 PROT 保护并短暂开启 TEC，
  首次状态检查发现连接保护而停止；已确认 LD 零/关闭及 TEC 关闭，未开启 LD。
  这是保留的历史尝试；其“已确认物理连接故障”结论由后续证据撤回。
- 用户随后报告手动出光正常，将目标改为 26 °C。只读生产驱动验证了 5 秒稳定温控，
  TEC 保持开启，实际温度约 26.000 °C、TEC 电流约 0.158 A、温度保护为 0，LD 关闭。
  未改动当前设置或输出。CONN=1 与 MEAS bit13 仍存在且无 SCPI 错误，诊断含义待核实；
  不反转为连接正常，不再冠以不适用 CLD 的通用 cable 故障名称，仍阻止自动开光。
- 温度保护窗口是全宽，实际允许区间为目标 ± 窗口/2；软件稳定容差不能超过窗口/2。
  温度保护 trip 允许 LD 关闭的 TEC 预热，但准备/开启 LD 或正式发光验证必须清除。
- CLD 的电压 MAX 查询是测量范围，不能作为缓存的二极管允许电压。真实 LD 软件电压
  边界仍需明确；不把未知二极管型号或设备测量能力伪装成器件规格。
- 当前完整离线回归 511/511、光响应 96/96 通过；目标机 `lyr` 中 96/96 通过。
  目标机内存故障经用户授权停止一个遗留只读 PowerShell 后恢复；未停止其他实验进程。
  地址、完整身份、缓存、原始通信和内存诊断均保存在被 Git 忽略的本地审计中。

## 离线运行

```powershell
$env:PYTHONPATH = 'src'
python -m attodry_control.photoresponse_cli describe
python -m attodry_control.photoresponse_cli validate-config --config config/photoresponse_simulation.toml
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_simulation.toml
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_cld_simulation.toml
```

成功输出 `SIMULATION completed: <JSON路径>`，退出码 0；采集失败为 1，配置/文件错误为 2。
没有后台进程。`simulate` 拒绝真实 backend，不会偷偷替换硬件配置。
示例限值只是合成数据参数，不能作为样品或激光二极管的安全参数。
控制电脑须使用 `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe`，不能用裸系统 Python。
本地 Anaconda 若存在用户目录 NumPy/Matplotlib 冲突，可设置 `PYTHONNOUSERSITE=1` 并用 `python -s`。

## 三种测量模式

| mode | detector | 偏置字段 | 结果与电路含义 |
|---|---|---|---|
| `open_circuit_voltage` | `keithley2182a` / `keithley2400` | 两个偏置数组都必须省略 | 光电压；外部激励路径须物理隔离 |
| `dc_current_voltage` | `keithley2182a` / `keithley2400` | `bias_current_points_a` | 6221 恒流下的光致电压变化 |
| `voltage_bias_current` | `keithley2400` | `bias_voltage_points_v` | source–drain 沟道电压偏置下的光电流；0 V 点用于短路光电流 |

2400 的电流探测接线须为沟道 source–drain 路径；底栅–source 电流是栅漏电流。
当前站点 2400 属于底栅角色，不能勾选该光电流探测配置的接线确认。

`none` 不创建 6221 电流源控制器、不发送其输出设置。若 2182A 经 6221 转发，
仍需连接并查询 6221 的身份、关闭状态和 Delta/Pulse Delta/DCON 是否处于 armed 状态。
`none` 不等于 DC 0 A。6221 的电流设置不是实际样品电流，2182A 电压也不能当作电流。

2400 作电压表时采用厂商所述的 0 A 电流源模式、最低源电流量程、固定电压测量量程，
且测量期间必须开启其 OUTPUT。这与 2182A 的被动电压输入不同，需单独确认负载影响。
2400 电压 compliance 须高于预期电压；电流模式则配置电流 compliance。两种模式均记录
V/I 与读数状态、禁用电阻测量/处理功能，结束后回零、关闭并确认 HIMP 关闭模式。

扫描顺序：环境点（可选）→ 光学点 → 偏置点 → repeat → dark_before/light/dark_after。
每个窗口采集 `samples_per_window` 个新读数。`settle_s` 是每次切光后的等待；
`sample_interval_s` 是读数前的等待，不代表保证的采样周期。串口转发与状态检查有额外开销。
硬件样本时间取主机发送 READ? 到得到响应的区间，不包含周围的配置核对时间；不是仪器内部时间戳。

## 配置模板

默认本地文件是被忽略的 `config/hardware.local.toml`。合并需要的表，保留已有站点配置。
也可把临时配置放在被忽略的 `run_data/` 下。所有相对输出/数据库路径相对该 TOML 文件解析。

| 模板 | 用途 |
|---|---|
| `config/photoresponse_hardware.example.toml` | NKT + 6221 + 串口转发 2182A |
| `config/photoresponse_cld_hardware.example.toml` | CLD1010LP + 2400 光电流 |
| `config/photoresponse_6221_test.example.toml` | 用户授权的独立空载 6221 五点测试；不连接探测器/光源 |
| `config/photoresponse_environment_simulation.toml` | 可附加到仿真配置的温磁场扫描表 |

真实模板故意含 `CHANGE_ME`，不能直接运行。未知身份、限值、接线、DLL、步长或不匹配的模式
在连接前拒绝。切换电压表/电流表时必须重新检查单位、量程和 compliance。
`photoresponse.light_source` 选择 `nkt` 或 `cld1010lp`，不同时连接两个光源。

2182A 直接连接时用 `detector_transport="visa"` 并填写 `[photoresponse_2182].address`；
经 6221 时用 `via_6221`，省略该 address，只填 `[photoresponse_6221].address`。
无激励而使用转发时，6221 表只保留 `address` 与 `expected_idn`，不应含输出参数。
2400 只支持直接 VISA，使用 `[photoresponse_2400]`。未选中的仪器不会被打开。

NKT 复用现有 EXTREME/VARIA SDK 模块；LLTF 的真实 SDK 仍不可用。`nkt_run.emit=true`，
`dwell_s=0`，由光响应窗口控制明暗和等待。NKT 百分比不代表样品瓦特。
CLD 默认使用 `backend="visa"` 和已有 NI-VISA；也可选 `tl4000` 并提供 x64 DLL。
VISA 单次通信最多等待 10 秒；两条路径均不 reset。先检查互锁、LD auto-on、TEC 故障、模式与保护，
关闭激光配置电流，TEC 稳定后才允许发光。CLD 的 760 nm 是二极管标称值，不能扫描波长。
USB 固件查询已经实测，出光/温控仍待验收；型号可按用户明确指示记录为未确定，
不能虚构规格。电流/电压边界、TEC 电流/温度、保护窗口、接线和光路须满足当前阶段要求。
ENAB 是输出允许模式，PROT 是越界关断模式；真实控制要求 PROT。故障阻止输出。
结束独立尝试关闭 LD、回零并恢复 TEC 原状态；任何失败都会保留未知状态和清理错误。

## 实机命令（只有对应阶段已获授权且总线空闲时运行）

```powershell
# 只查电学身份与现有设置；不 READ、不 reset、不消费错误队列，不打开光源。
python -m attodry_control.photoresponse_cli diagnose --config config/hardware.local.toml --authorize-connect

# 仅 6221 空载测试。使用独立五点测试表；不会打开 2182A、2400 或光源。
python -m attodry_control.photoresponse_cli test-6221 --config config/hardware.local.toml --authorize-connect --authorize-writes

# 完整光响应；真实量测、源/激光设置及错误队列读取均属写授权范围。
python -m attodry_control.photoresponse_cli run --config config/hardware.local.toml --authorize-connect --authorize-writes --confirm-route
```

VISA 使用显式地址和资源独占锁，但这不能防止其他程序使用同一控制器的其他地址。
实际测试前仍须协调共享 GPIB 总线。通信超时后不盲目重试可能滞留的响应；本会话只尝试安全关闭，
不把关闭命令当作关闭确认。6221 空载进入 compliance 会停止，不将限幅点算作正常测量。

## 可选温度/磁场扫描

使用现有 `[cryostat]`、`[magnet]`、`[temperature_stability]`，另加
`[photoresponse_environment]` 的明确温度/Bx/Bz 点、最大温度步长、最大超调及磁场步长。
真实 run 还必须加 `--authorize-environment`。不配置该表就不创建 attoDRY 驱动。

变温/变场前关闭光与电学源；温控沿用先启用反馈、后写温度目标的验收顺序。
等温磁场稳定后再准备电学采集。保留 `sqrt(Bx²+Bz²) <= 3 T` 的路径与读回检查。
样本保存实际 `measurement_temperature_k` 和实际 Bx/Bz，不能用目标温度替代实测温度。
正常结束按 hold/zero 配置，失败先尝试关光及电学清理，再请求磁场归零。通信失败时保留
最后确认的非零磁场，`state_current=false`，要求人工核实；不能推断零场。

## 数据、恢复和分析

JSONL 逐事件 flush；配置 `[photoresponse_storage].database_path` 后另存 SQLite WAL/FULL 审计。
采用独立 `photo_runs/photo_events/photo_conditions` 表，不改历史 SR830 数据表。
最终 JSON 保存原始窗口、命令/响应日志位置、配置、环境、错误、清理及软件版本。
每次 run 全部条件与清理通过后才接受；失败/中断的原始尝试永久保留，默认分析排除。
源码不同或计划不同的重试应作为新系列，不能强行与旧计划拼接。

失败后显式指定 `--resume-run <原run_id>` 创建同一系列的新 attempt，重新 preflight 并重测
整次计划；不拼接不完整的暗亮窗口，不自动重新开启输出。计划 hash 必须一致，成功系列禁止重复接受。
若上次进程崩溃，查询数据库中的 `photo_runs` 获得 run_id；旧 running 尝试会标记 interrupted。
输出目录/数据库进程锁阻止重复任务同时运行；不同输出目录不能代替实验台资源协调。

暗背景按三个窗口实际采样中点进行线性插值：

```text
w = (t_light - t_dark_before) / (t_dark_after - t_dark_before)
dark_at_light = dark_before + (dark_after - dark_before) * w
delta = light - dark_at_light
```

保留暗漂移、权重和公式版本；不声称消除非线性漂移。缺少功率计时没有 A/W、V/W，
光源百分比/LD 电流不是测得的样品光功率。仿真标记明确，未配置环境时环境为 null。

```powershell
python -m attodry_control.photoresponse_cli analyze --record 'run_data/实际文件_completed.json'
python -m attodry_control.photoresponse_cli export --record 'run_data/实际文件_completed.json' --output 'analysis_output/新结果.csv'
```

[分析 Notebook](../notebooks/Photoresponse_Analysis.ipynb) 只读原始 JSON，不导入仪器驱动。
导出 CSV 与选择 manifest，保存输入 SHA-256，不覆盖既有文件。需要诊断失败尝试时可显式调用
`load_results(path, include_rejected=True)`，这些结果仍标记 accepted=false。

## 离线验证

```powershell
python -s -m unittest discover -s tests -p 'test_photoresponse*.py' -q
python -s -m unittest discover -s tests -q
```

测试只使用 fake/simulated resources；通过离线测试不表示实机接线、固件、测量噪声、
光功率或样品允许值已经验收。当前验证总数和目标机验证见 [开发阶段](DEVELOPMENT_STAGES.md)。
