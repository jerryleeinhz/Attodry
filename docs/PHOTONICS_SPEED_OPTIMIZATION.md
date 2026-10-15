# 光电组合扫描耗时优化（2026-10-15）

交付分支：`integration-photonics-nonlinear-hall`。开发阶段仅本地实现和离线验证。
操作者随后于 2026-10-15 授权提交、推送 origin 和部署 LK_setup；部署状态与目标机
离线验收见 `PROJECT_HANDOFF.md` 的交付条目。保留本机 TOML、数据、Notebook 修改
和当前采集所有权；实机对照待操作者另行确认。

## 依据和真实耗时

审阅已发布的 `origin/codex/integration-four-module-scan`，功能提交
`accacb089edc5690f71d010c46f31faa053c6867`，交付记录 `e2d66ef`。
公共 `lockin_settling.py` 从该功能提交复用，保留其接口；本分支用显式变化通知
连接混合型号适配器、PEM 恢复与光学保护，不包装或更换底层仪器驱动。

对本分支两份历史数据库进行只读事务分析：

| Run ID | 完整条件及样本 | 事件时间戳耗时 |
|---|---|---|
| `20261007T152747501378Z_630-real_0.004V_excitation-LP-at-45-axis` | 6 条件 / 18 样本，完成 | 条件循环 769.79 s；准备 638.48 s（83%）；正式采样段 131.30 s（17%） |
| `20261007T055106814179Z_532-796nm-gate-no3mW` | 中断前 65 个完整条件；另有部分第 66 条件 | 完整条件 11849.61 s；正式采样段 8568.77 s（72%）；普通条件中位数约 163 s |

第一份用 PEM 2f 外参考，XX SR830 / XY SR865A 均为 h1、TC 1 s、15 tau。
第二份两路均依次读 h1/h2、TC 1 s、15 tau。磁场、温度轴均未启用。
启动和全局清理不包含在上述条件循环内。正式采样段包含谐波切换、稳定等待、
光学保护和通信，不能视为纯数据读取时间。第二份的中断和过载无效数据保留。

锁相审计分别记录 11,076 和 190,757 个底层 I/O 项（第二份包含中断条件）。
此计数不是独立正式样本数，也不是通信耗时。现阶段不凭计数删除身份/状态查询，
更不并行访问共享接口。实机通信延迟和真实暂态响应仍需对照测量。

## 已实现的等待规则

每一路的期限为：

```text
deadline[role] = last_relevant_change[role]
                 + time_constant_s[role] * settle_time_constants
wait = max(0, max(deadline[XX], deadline[XY]) - monotonic_now)
```

XX/XY 共用等待阶段，分别满足各自期限后依次读取。时间常数和配置倍数不变。
重复资格检查继续读取设置、源状态、参考频率和状态，只补剩余等待。
正式采样入口也检查期限；不能跳过 `qualify()` 而提前取得合格数据。

| 变化 | 本分支记录的计时起点 |
|---|---|
| 首次测量配置 | 对应角色配置成功返回；初次外参考确认还会保守重置两路 |
| XX 激励幅值 | 底层实际发生写入且读回确认后，重置两路；无写入不重复重置 |
| 谐波 | 仅更改实际需要切换的角色，确认成功后重置该角色 |
| PEM 参考切换 | 外参考等待完成后重置两路，保留保护激励下的稳定检查 |
| 参考故障恢复 | 连续合格要求满足后开始原有完整滤波稳定段；保留共享恢复期限 |
| 光学开光/调功 | 本次调功成功返回、进入 guarded dwell 前重置两路 |
| SMU/gate | 所有本点设置成功返回、进入原有 delay 前重置两路 |
| 磁场 | 执行器完成实际到场和稳定窗口后重置两路 |
| 温度 | 本次轴入口完整判稳完成后重置两路 |

光学起点采取保守值：不把调功期间或反馈稳定窗口的早期时间计入锁相稳定，
只抵扣最终调功返回后的光学 dwell、读回和检查时间。gate 起点同样位于全部
设置完成后。前一次光照/gate/环境条件的时间不能抵扣新变化后的等待。
`sample_interval_s`、`dwell_s` 和功率稳定窗口仍有各自含义。

恢复中所有补等仍经过原 `_wait()` 的保护守卫和共享 deadline；不会改用裸 sleep
绕过光学保护。恢复成功但没有再次写激励时，不再叠加一段完整激励等待。
`record_continue` 的过载/失锁质量标记和默认分析排除保持原契约。

过渡 raw pair 与正式 pair 继续分别记录。新增事件 `lockin_settling_changed`、
`lockin_settling_wait` 保存变化原因、角色、单调时间、要求时长、已耗时间、
期限、补等时长和完成状态。源变化同时保存在下一次等待的 changes 内；保护操作
不会因新增日志失败而跳过保护结果的核验。失败/中断的等待记录 `completed=false`。

## 温度和磁场资格生命周期

Combination 按轴索引前缀调用 `qualify_axis`，scope 包含 repeat、外层/本层索引和
condition ID。每次温度轴 apply 都清除旧资格，即使请求温度数值相同。
本次进入完成一次稳定窗口后，内层条件继续采集新鲜实际温度和严格保护检查。
回访、重复、内层重置重新判稳；硬件断点恢复仍不受支持，没有新增恢复能力。

磁场 `execute_field_target()` 本身已完成设置确认、实际到场及稳定窗口。
取消每个叶子条件重新开启的 `wait_for_field()`，保留执行器内部的安全中间点
等待、重复磁场点的完整执行、正式窗口读回以及实际回零判稳。
field qualification 明确来自本次执行，不能按场值相同跳过执行器。

本分支保持严格温度异常中止策略，不移植参考分支的温漂仅记录策略。
温度 assessment 保存 baseline/target error 和 quality flags；异常原始读回先保存
再拒绝。磁场控制、设定值、实际偏差、窗口波动、通信和未知状态仍为硬故障。
Photonic profile 的严格 resultant <=3 T 和清理/人工核验规则均保留。

## 适用范围与收益边界

单调等待优化适用于 `PhotonicsLockinPointSession` 的两种 PEM 外参考拓扑。
普通 internal-reference 双 SR830 acquisition 和独立诊断入口保留其原有实现；
本次不移植参考分支的全部 daily sweep/autorange 代码。
温度/磁场轴入口接口由 Combination 共用，所有受影响组合顺序都参与离线回归。

历史六点 2f 记录仅重复资格等待有约 90 s 优化空间；若激励恢复无实际写入，
另有约 90 s，即条件循环约 12–23% 的结构估算。h1/h2 实际切换仍要求新的稳定段，
不能据此推断栅压扫描也有相同收益。每场点 N 个内层条件、重复磁场窗口 D 秒时，
重复等待的结构开销约 N*D；这里没有含磁场/温度轴的实测速度结论。

## 验证与待实机对照

修改前 `016d741` 与本次候选使用同一套 fake SR830/SR865A、NKT、PEM、PM100D
和 SMU，在独立进程执行相同两光学组 × 三 gate × 三正式样本。TC 均为 1 s，
settle 为 15 tau，目标激励 6 mV（组间先保护至 4 mV）。结果如下：

| 模拟计划 | 修改前时钟预算 | 修改后时钟预算 | 减少 | 样本/清理 |
|---|---:|---:|---:|---|
| 两路仅 h1 | 227.75 s | 197.49 s | 30.26 s，13.3% | 两版均 18/18 clean，cleanup clean |
| 两路 h1/h2 | 752.75 s | 722.49 s | 30.26 s，4.0% | 两版均 18/18 clean，cleanup clean |

模拟不注入通信延迟；表中是同配置等待预算，不是真实墙钟提速。两版 VISA
调用数分别相同（h1 9,605；h1/h2 18,637），说明本阶段收益来自等待合并，
没有通过删除正式检查减少读取。两版均完成 6 条件，实际谐波切换仍占主要等待。
原始比较 JSON、配置生成脚本和基准源码快照保存在 ignored timing-audit 目录。

离线 runner 阻断 pyvisa、serial、qcodes 与真实 DLL 加载，使用 fake transports、
fake DLL、可控单调时钟及临时 SQLite。新增回归覆盖重复资格、实际变化重新计时、
两路不同时间常数、每角色谐波变化、正式入口补等、等待中断保护、光学 dwell 和 gate
delay 抵扣、温度回访/repeat、磁场重复点及测量中漂移故障。原有参考恢复、PEM 1f/2f、
过载/失锁继续记录、整组重试和失败清理回归继续运行。

验证记录（数量有交集，不相加）：

- 最终光电相关回归：294 项通过，250.788 s。
- 最终等待/点引擎/参考恢复：64 项通过；同一组 Python 3.12 复验也全部通过。
- Combination/光学点/等待补充回归：67 项成功运行，2 项因原解释器缺分析库跳过；
  后续分析环境复验包含这两项。
- 环境、连续照光及恢复首轮相关回归：92 项通过；新增 12 项功能回归通过。
- 完整首轮：1560 项，650.207 s；58 个错误来自缺少 NumPy/Matplotlib 和沙箱
  禁止回环套接字，22 项因缺分析依赖跳过；另有一处旧测试精确比较 `0.1` 与
  `0.10000000000000009` 失败。改为近似比较后该模块 20 项通过，最终 294 项也通过。
  原始失败日志保留，没有把这次完整首轮描述为全部通过。
- 分析环境及首轮全部错误/跳过涉及模块复验：209 项通过，163.321 s，无跳过。
  此复验包含绘图、Notebook、PEM 缓冲诊断分析、回环流以及 Combination；
  首轮问题均已修正或在补齐环境后复验通过，未通过修改采集逻辑绕过测试。

分析依赖在独立 ignored 目录安装，不修改已有解释器环境。复验用 Python 3.12、
NumPy 2.5.4、Matplotlib 3.10.9、ipywidgets 8.1.9，并以 Agg 绘图。
本机回环测试在沙箱外运行，仍使用同一个阻断硬件导入和 DLL 的 runner；
不连接仪器或 LK_setup。复验清单覆盖全部首轮错误及跳过涉及的模块。

常用离线命令（在本分支根目录、已安装 analysis extras 的环境执行）：

```powershell
python -B tools/run_guarded_tests.py tests.test_photonics_settling tests.test_photonics_lockin_points tests.test_photonics_reference_recovery
python -B tools/run_guarded_tests.py tests.test_combination_cryostat tests.test_combination_continuous_gate tests.test_continuous_gate_direct tests.test_optical_points
python -B tools/run_guarded_tests.py
```

每次运行的 stdout/stderr、原始失败、复验结果、基准/候选模拟 TOML 和匹配的配置
哈希、最终源码 SHA256 均在 `.test-tmp/timing-audit-20261015`。比较配置逐字节一致。

实机对照在操作者确认仪器占用、光路和授权后进行，使用独立配置/唯一 run ID，
保存修改前后源码和配置哈希。先固定波长少量功率点，再少量 gate，比较耗时、
X/Y/R/相位、离散程度、无效比例及清理。磁场/温度的小规模对照另选已批准范围。
离线时钟收益不代表真实仪器提速，也不能证明样品达到稳态。
