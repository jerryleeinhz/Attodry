# V/I 双测与低温启动验收 — 2026-09-22

本次是无样品的分阶段功能检查，不是四模块联合实验或计量精度验收。
真实地址、配置、原始数值、驱动日志仅保存在 ignored commissioning/data 目录。
没有提交、推送、更新 main 或改动既有目标电脑工作区。

## 1. Keithley 2400 电压源模式下的实际 V/I 双测

此前 QCoDeS 的电压源模式只启用 CURR，不能把旧 voltage_v 字段认证为独立
端电压测量。本次显式启用 concurrent VOLT/CURR、双测量自动量程及 VOLT,CURR
返回顺序；配置后和每次正式采样前验证测量功能。输出 OFF 不发送 READ?，
字段数量/顺序不符即拒收。原有旧数据不修改、不重新标记为双测。

目标电脑 lyr Python 3.12.13 / QCoDeS 0.59.0。V/I 验收源码包 SHA-256：
971c306cbd46b0ee3ddb3b462111210e55bfaed6c4bf7f50f01229bbfc04c19d。
完整离线回归 585 项通过，测试期间阻止真实 VISA/QCoDeS/serial/DLL 访问。
最初的打包缺少配置夹具、以及测试路径设置失败均保留日志，未计为通过。

实机 run ID：20260922_122618_5cca0a44。
仅 bias；电压源、两线、NPLC 1、每点等待 3 s、每点 1 个读数；
目标顺序 0,+1,0,-1,0 mV，限值保持 10 mV / 1 microampere。
5/5 样本 clean/accepted，独立分析、Notebook 数据层和组合 legacy adapter
都返回 5 行，原始点序和重复零点完整保留。

测得最大绝对端电压 1.061803 mV，最大绝对电流 18.05176 pA。
这些是空载观测，不用于校准精度、推断样品电阻或认证带负载 compliance trip。
09:26:38 UTC 独立检查确认零设定、output OFF、1 microampere 保护、错误 0，
并确认 VOLT:DC/CURR:DC、concurrent=1、VOLT,CURR 返回格式。

## 2. 升温前的设备状态

09:28 UTC 三次 cryostat 读数：样品 1.6796 K、存储目标 300 K、温控 OFF；
X/Z 读回和设定均为零、场控 OFF、错误 0。没有温度或磁场写入。

两个 SR830 都已是 4 mV，故未发送任何激励设置命令。XX 内参考，XY 外部
上升沿参考。初次状态中的 XX 频段变化和 XY 曾失锁/输出过载锁存均保留；
后续两次成对状态为零、参考已锁定。这是短时状态检查，不是带样品采集。
不能仅凭软件读数证明 XY 的 SINE OUT 物理断开。

## 3. 温控启动修复与本次授权范围

用户允许测试升到 2–3 K。测试采用单独的 2.0,2.2,2.4 K 目标，不读取执行
既有配置中约 32–287 K 的扫描网格。软件设定上限 3 K；每次相对实时样品
温度最大移动 0.5 K；过冲到 target+0.2 K 即拒绝并执行关闭温控。
没有 PID、加热器配置、磁场设置或任何电学输出写入。

四个温度入口统一使用 set_temperature_and_enable：
先预写/确认目标，再开启温控，OFF→ON 后保留已验证需要的强制复写。
未确认的预写不得启用旧目标。相关 fake-DLL/VISA 回归 96 项通过；
完整 LK_setup 离线回归 588 项通过，75.672 s，无失败/跳过。

温度验收源码包 SHA-256：
fad188071b45c3764be9e4df78b746f443e442435f963e7a9e0ab049a0d9d6cd。
文件清单逐项 SHA-256 在连接前复核，DLL 使用既有温度工作区版本。
运行时命令日志进一步禁止磁场写入，并只允许三个位于 2–3 K 内的温度目标。
09:42 UTC 实机日志已确认 2.0 K 预写→开启→2.0 K 复写的顺序。

本次是短时粗容差 commissioning：target 模式 ±0.2 K、10 s 连续窗口、
窗口峰峰值 <=0.02 K、轮询 1.5 s、每点最长 600 s。不是“三秒即稳定”；
实际温度与设定温度分开记录，不把这种测试容差默认为正式实验标准。
正常结束保持最后目标/温控 ON，异常或可捕获中断关闭温控，不声称已到 base。

实机 run ID：20260922T094241Z_lowT_2p0_2p4，09:42–09:58 UTC。
三点全部完成，603 次温度轮询，未中断、未恢复重试，cryostat 错误均为零。
稳定检查期间的实际读数范围为 1.679200–2.210100 K，未触及 3 K。

| 请求目标 / K | 接受窗口实际均温 / K | 窗口峰峰值 / mK | 达到本次验收条件 / s |
| --- | --- | --- | --- |
| 2.0 | 1.802786 | 3.900 | 431.375 |
| 2.2 | 2.003700 | 4.100 | 264.562 |
| 2.4 | 2.205586 | 9.100 | 205.922 |

每个接受窗口含 7 次轮询；不是 7 次独立实验。升温过程中即可满足这个粗容差，
不能声称样品已经在各个名义目标精确热平衡。正式实验需另定容差/驻留标准。
正常结束的实际温度为 2.210100 K，设定保留 2.4 K、温控 ON，程序断开连接。

10:01:39–45 UTC 独立重新连接、禁止写入的三次状态检查，实际温度为
2.330600、2.331700、2.331900 K，设定仍为 2.4 K，温控 ON、错误 0；
X/Z 读回和设定均为零、场控 OFF。检查后断开，没有声称回到 base。
3 K 是本轮软件测试边界，不是断开之后仍在运行的独立硬件保护或连续 watchdog。

随后确认 bias 为 0 V 设定、output OFF、1 microampere 保护、错误 0，
两台 SR830 均为 4 mV。但是 XY 的 LIAS=4 令严格的全干净最终检查失败退出；
final_readonly.jsonl 和对应失败日志完整保留，没有重新标为通过。
10:03:34–40 UTC 又进行了三组成对状态检查，全部 LIAS=0 / ERRS=0、
XX 内参考、XY 外部上升沿参考，未发送任何设置命令。
LIAS?/ERRS? 本身会消费锁存状态；本次查询并非无副作用的被动文件监控。
该过载锁存没有在短时复核中重现，但根因尚未确定，不能认证长期锁定/无过载。

## 4. 未完成范围

- 真实任意循环顺序的四模块单-owner 执行器仍未完成；模拟器通过不是实机通过。
- 本次没有磁场移动、persistent 模式、长期保场或联合故障验收。
- 没有 current-source、four-wire、gate、负载校准或断电/强杀验收。
- 温度测试没有打开 SMU/SR830；电学和 cryostat 的本轮检查是分阶段执行的。

## 5. 原始证据位置

LK_setup / Yuanrong Li 下分别保留两个独立目录：

- Attodry_vi_19001dc5_20260922/source-v2：最终 V/I 源码包、585 项测试日志、
  run_data/vi、receipt 与 status 原始记录。目录名源于第一次打包，实际使用的是
  上述 971c306c… 的 source-v2 包，不能用目录名代替校验。
- Attodry_lowT_fad18807_20260922/source：温度源码、588 项测试日志、独立
  commissioning/lowt.local.toml、温度 JSONL/summary/CSV 与命令 receipt。

本机副本在当前 worktree 的 .test-tmp/vi_20260922/evidence 和
.test-tmp/lowt_20260922/evidence；verification.json 保存只读核验与原始文件 hash。
后者的 postcheck_verification.json 分开记录首次最终检查失败和三次后续通过。

## 6. 诊断曲线和复现

低温 evidence/figures/temperature_trace.png 和同名 PDF 只绘制全部 603 次
temperature_sample 原始读数，保留轮询缺口及条件切换断线，无平滑、拟合或均值化；
橙色虚线是确认后的目标，灰色 3 K 线仅表示操作者上限，不是假读数。
时间轴为相对开始时刻的分钟，样品与设定温度分别编码。扫描后独立复核未拼接
进主曲线，保存在单独原始记录中。原始 JSONL SHA-256：
b9ce872252a691095b59f45edf7858122e17ea5a00ba2e7a2996f0facdaad6f3。

PNG 1800x1000、约 200 dpi，RGBA 但 alpha 全为 255；9x5 inch PDF。
PNG 已目视检查标签、图例和裁切；PDF 元数据独立检查。输出 manifest 保留
源数据/绘图脚本 hash、Matplotlib 3.10.9、Pillow 11.3.0 与变换说明。
蓝色点/实线与橙色虚线提供颜色之外的区分；不据调色板、DPI 或自动检查声称
无障碍认证、论文投稿合规或计量认证。本轮使用 karpathy-guidelines 限定修复
范围，并使用 scientific-visualization 的原始数据、导出和溯源检查流程。

软件方法参考（2026-09-22 已核对当前 arXiv 元数据，无期刊替代条目）：
Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026).
*Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*.
[arXiv:2609.00065](https://doi.org/10.48550/arXiv.2609.00065).
