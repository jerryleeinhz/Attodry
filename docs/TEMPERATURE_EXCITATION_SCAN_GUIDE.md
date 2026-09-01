# 温度—激励扫描指南

## 当前边界

本模块把已存在的逐点温度稳定流程和双 SR830 幅值扫描组合成一个**外温度、内激励**
的编排路径：每个升序温度点先完成温度稳定判据，再在该条件下完成整条 SR830
激励幅值扫描，随后才前往下一个温度点。

代码已完成该组合的编排、记录和恢复合同，并通过 fake-instrument 离线验证；精确测试
记录见开发阶段文档。它不表示真实温度—激励联合实验已经做过，也不把 Temperature 或
Lock-in 的单模块验收自动扩展为联合验收。真实 DLL/VISA 连接、清除锁存位、温度写入和
SR830 写入必须在未来获得一次范围明确的真实硬件授权后才可执行。

## 运行前提和命令

运行配置仍以 ignored 的 `config/hardware.local.toml` 为唯一日常来源：温度网格（包括
可分段的 `temperature_ranges`）与稳定判据来自已有的温度表，SR830 的角色、幅值路径、正式谐波、时序和电学边界来自已有的
Lock-in 表；`[temperature_excitation_scan]` 只保存本组合扫描的运行标签、备注和输出目录。
不要把地址、DLL 路径或原始数据提交到 Git。

未来在已完成独立硬件授权的目标电脑上，预期命令为：

```powershell
C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe -m attodry_control.temperature_excitation_scan `
  --config config/hardware.local.toml `
  --authorize-temperature-excitation-scan
```

或在已激活的 `lyr` 环境中使用：

```powershell
python -m attodry_control.temperature_excitation_scan `
  --config config/hardware.local.toml `
  --authorize-temperature-excitation-scan
```

`--authorize-temperature-excitation-scan` 是组合流程的显式闸门，不是对未来不同接线、
不同温度范围或不同 SR830 写入范围的长期授权。尚未获得该次授权时，只能运行离线配置、
测试和 `--help` 检查；不要把上面的命令当作当前可执行的实验步骤。

## 执行顺序

每个 temperature condition 的顺序固定如下：

```text
升序目标温度
  -> 读回/确认温控与 setpoint
  -> 等待稳定窗口
  -> 记录稳定窗口温度摘要
  -> 完整 SR830 excitation sweep（所有配置幅值与正式谐波）
  -> 每个正式 lock-in 成对采样同步读温度
  -> 验证 lock-in cleanup 与温度后检查
  -> 标记该 temperature condition completed
  -> 下一个温度点
```

两台 SR830 仍按语义角色使用：`lockin_xx` 是内部参考、提供 SINE OUT 并测量 Vxx；
`lockin_xy` 使用 TTL 外参考、测量 Vxy，且其 SINE OUT 必须保持物理断开。成对读数在
时间上仍是顺序读数，记录中不能把它标记成真正同步的两台仪器读数。

## 两个温度值，两个用途

不要用请求 setpoint 代替任一实际温度读回。本模块保存两个不同的温度量：

1. **稳定窗口均值**：温度点在开始 excitation 之前，按
   `temperature_stability` 的控制、错误、容差、dwell 和范围判据取得的稳定窗口样品温度
   均值。它说明“何时允许开始测量”，并保留稳定性标准差、范围和样本数。
2. **正式测量窗口温度**：每个 SR830 正式成对样本前后同步读取 attoDRY 状态，以采样
   时间戳做梯形时间加权平均。此实际样品温度是该 formal sample 的温度坐标；条件级摘要
   还只对全部 formal sample 的实际测量区间做时间权重合并；幅值/谐波转换与两次正式
   样本之间的等待不被误算为测量时间。

稳定窗口均值与正式测量窗口平均值故意分开保存。扫描期间的缓慢漂移会反映在后者中，
而不会重写已经通过的稳定判据。温度读取由正式采样回调顺序执行，不使用会隐藏时序或
错误的后台 DLL 线程。

## 数据、失败和恢复

每次尝试保留三个层次的证据：

- 增量 JSONL 事件流：温度状态、稳定等待、锁相转换/正式样本、每个同步温度读回、
  cleanup 和异常都会立即留档；已捕获的部分样本不会因后续失败而删除。
- 总结 JSON：保存解析后的配置快照、preflight、每个完整 temperature condition、
  final/last-confirmed state、cleanup 结果和最终 `completed`、`rejected` 或
  `interrupted` outcome。
- 正式样本 CSV：用于默认分析，只展开已完整完成 temperature condition 的正式
  lock-in 样本及其实际测量窗口温度。rejected/interrupted 的原始记录仍在 JSONL/summary
  中，只能通过显式审计选择使用，不能混入默认曲线。

恢复使用同一次运行的 progress JSONL：

```powershell
python -m attodry_control.temperature_excitation_scan `
  --config config/hardware.local.toml `
  --authorize-temperature-excitation-scan `
  --resume-progress PATH_TO_PROGRESS_JSONL
```

恢复会核对已归档的配置合同，并且**只**跳过连续的、已经完整结束的 temperature
condition。它绝不从某个幅值或谐波中间继续；只要该温度点的 excitation 或其 cleanup/
温度后检查不完整，下次会从该温度点的稳定阶段重新开始。这样部分原始数据可审计，
但不会伪装成完整条件。

## 异常与人工核验

任何 preflight、锁定/过载/错误状态、读回或通信问题都会停止当前 condition。已经启动
的 Lock-in 路径先执行其既有最小激励/基线恢复，然后温度路径按既有失败策略处理温控；
通信失败不能被写成“已确认安全”。检查 JSONL 中的 primary error、每个 cleanup error 和
last-confirmed state，再决定是否需要前面板或接线检查。正常完成沿用温度扫描的最终目标
保持行为；它不代表磁场或 SMU 已被接管。

## 分析约定

默认图和表只使用 `completed` condition 内、状态干净的 formal samples。横轴的温度应选用
正式测量窗口的实际带时间权重平均值；稳定窗口均值是质量/准备度元数据，适合与该横轴
一起显示或做漂移审计，但不能被悄悄替换。相位、X、Y、R、谐波、SR830 source/readback
和状态元数据继续原样保留。

读取和叠图使用 `notebooks/sr830_commissioning_sweeps.ipynb` 顶部的
`TEMPERATURE_DATA_DIRECTORY`，默认指向
`run_data/temperature_excitation_commissioning`。刷新后可同时选择多个 summary JSON 或
formal CSV，再按状态和具体 temperature condition 筛选。若同一次扫描的 summary 与 formal
CSV 同时存在，目录发现优先使用 summary，避免同一数据重复计数；选择多个不同文件时则
保留文件和 temperature index 身份，不会把相同温度的不同运行静默合并。

每个可用的 `Vxx/Vxy × h1/h2/h3` 通道生成两张独立图：一张 `R` 幅值图和一张相位图。
横轴是文件中归档的、由实际 SINE OUT readback 得到的 `nominal_current_a_rms`，不会用当前
TOML 重新计算。每个实际 formal-window 条件平均温度是一条曲线，图例同时显示实测温度和
请求 setpoint。幅值重复样本使用普通均值/样本标准差；相位使用圆周均值/圆周标准差，并
只在递增电流方向做显示展开。低幅值处即使锁定正常，相位也可能没有物理意义，因此应结合
相位误差条、幅值和状态筛选判断，而不能只看展开后的线条。可选导出会保存选中样本、每张
PNG/PDF 和包含输入文件、状态、temperature condition 与相位处理方式的 manifest。

此文件只说明组合扫描的合同和将来操作路径；它不构成真实仪器授权，也不报告任何实际
温度—激励硬件运行已经发生。
