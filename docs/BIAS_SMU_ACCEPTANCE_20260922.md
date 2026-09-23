# Bias SMU 空载验收记录 — 2026-09-22

## 结论与范围

本次限定范围的控制、采集、实时流、归档、默认分析筛选及正常/软中断清理通过。
不是带样品实验，也不是完整电气计量或真实四模块联合验收。

用户最后确认未连接样品，并授权自行设置验收 goal、执行扫描。
仅启用 smu_bias；所有真实地址保存在未提交的本机配置/原始记录中。
安全上限不变：绝对电压 0.01 V，绝对电流 1 microampere。
源目标选在 +/-9 mV 内，不刻意贴着硬边界运行。
其他 SMU、两个 Lock-in、温度与磁场未连接或操作，物理状态不能推断。

## 运行环境与版本

- 分支：codex/integration-four-module-scan；本机/目标运行源码基线 1a87f03。
- LK_setup：Yuanrong Li/Attodry_combination_offline_1a87f03_20260922/source。
- 该目录为源码归档，不是 Git checkout；执行前逐个核对 src Python 文件与
  source.zip 字节一致。归档 SHA-256 见 PROJECT_HANDOFF。
- 精确解释器：C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe，Python 3.12.13。
- 原 lyr 无 QCoDeS；先 dry-run，再安装 QCoDeS 0.59.0 和缺失依赖。
  未替换已有 NumPy 2.5.2、PyVISA 1.16.2、Matplotlib 3.10.9。
  安装前 freeze 和完整 pip 安装报告保存在 bias_preflight。
- 安装后完整离线回归 580 项全部通过，无 skip，耗时 68.048 s；
  测试期间阻止真实 pyvisa/qcodes/serial 导入和 DLL 加载。
- 没有更改 runtime src、schema 或安全策略，也没有 commit/push/合并 main。

## 实际测试

每个目标 delay=0.5 s，NPLC=1，电压源、两线、源/测量 autorange。
正常测试每点三个连续读数，不视为三个独立样品。

| 项目 | 点序列/行为 | 正式样本 | 结果 |
| --- | --- | ---: | --- |
| 零点保持 | 三个 0 V 时间点 | 9 | completed/accepted |
| 最小步进 | 0, +1, 0 mV | 9 | completed/accepted |
| 双向扫描 | -9,-6,-3,0,+3,+6,+9，再反向至 -9 mV | 39 | completed/accepted |
| 有序重复点 | 0,+3,+3,-3,0 mV；重复点不去重 | 15 | completed/accepted |
| 受控软中断 | 0,+1,+2 mV 后在 callback 触发 KeyboardInterrupt | 3 | interrupted，原始数据保留 |

72 个正常正式样本全部 clean，无 trip 或错误。五次结束均由执行器确认
归零读回、output OFF、cleanup_errors=[]，无需未知状态人工核验。
运行后另一个短会话独立查询验证，不与采集并发。
最终三次检查时间为 08:11:39--08:11:41 UTC：0 V setpoint、output OFF、
current compliance 1 microampere、两线、错误 0。OFF 状态未发送 READ?。

首个预检在任何设置写入之前返回 601 / Reading buffer data lost，
因此停止并保留失败 receipt。随后错误队列返回 0，三次 OFF/zero/error-free
只读复核通过后仅重试一次零点测试。不把首个失败记录改成成功。
没有发送 *RST、*CLS 或 deliberate reading-buffer-clear。
QCoDeS 构造阶段包含默认 VISA interface clear 和 trigger/format 设置，
它们也在 driver.log 中；不能将 QCoDeS 构造称为纯只读。

实际设置命令由原 QCoDeS adapter / ThreeSmuSession 发出，包括 trigger count /
VOLT,CURR 返回格式、source zero/targets、source mode、sense CURR、NPLC、
autorange、remote sense OFF、current compliance 1 microampere 和 output ON/OFF。
验收 helper 只做身份/OFF/零点校验、源码 hash 校验和日志，不替代安全执行器。

## 监控与分析

- CLI 是唯一硬件 owner；实时监控只订阅本机 HTTP 内存样本流。
- 双向扫描接收 41 个事件：start、39 samples、正常 terminal。
  39 个 sample payload 与 raw.jsonl 逐条相同，无额外 VISA 查询。
- 原始事件终止状态、五次 metadata、CSV 样本数、cleanup、重复点及
  forward/reverse 分段均经过只读程序交叉核对。
- standalone loader、Notebook 数据层和组合分析 legacy adapter 均返回
  72 个 accepted rows；中断的三行默认返回零行，audit opt-in 可完整载入。
- 图使用 source setpoint readback 作横轴、实测电流作纵轴，显示 39 个原始点；
  不平均、不平滑、不拟合、不剔除点，正反向用颜色和点型共同区分。
  PNG/PDF、原始数据 hash 和选择/单位换算 manifest 一同保留。
- 这是数据层及静态绘图验证，不声称实际 VS Code/Jupyter UI 已人工交互验收。

## 必须保留的缺口

1. 最终 :SENS:FUNC? 为 CURR:DC，与 QCoDeS 电压源模式只启用电流测量一致。
   当前 voltage_v 字段不能当作已验证的独立端电压测量。本次只证明源设定、
   电流读回和软件流程；接样品前应明确并测试实际 V/I 双测语义。
2. 空载无法证明带负载的 compliance trip 响应，也无法校准电流/电压精度；
   本次没有为了制造 trip 而接负载或扩大安全限值。
3. 软 KeyboardInterrupt 只验证可捕获中断，不代表进程强杀、电脑掉电、
   SSH 断开或 VISA 断线之后仍能自动关闭输出。
4. 没有验收 current-source、four-wire 或另外两台 gate。
   新样品的接线、ground/guard/common 和负载适用性仍需要确认。
5. 任意 temperature × SMU × magnetic × lockin 的真实单-owner 执行器仍待实现；
   这次不将 combination simulator 的离线通过等同于真实联合通过。

## 证据位置

以下均在目标 source/run_data 下，原始配置、地址与数据不提交 Git：

- bias_acceptance：五次正式记录，各含 metadata.json、raw.jsonl、data.csv。
- bias_acceptance_receipts：包括首个拒绝及五次测试的状态查询、源码/配置 hash、
  QCoDeS/PyVISA driver.log。
- bias_preflight：初始只读记录、601 复核、最终三次确认、依赖安装/测试日志、
  五次执行 stdout/stderr 和双向实时流。

对应 run IDs：

- zero：20260922_110649_6646afe1
- small：20260922_110743_8d83185a
- bipolar：20260922_110831_9e1f5c0d
- ordered：20260922_110925_b4e913e2
- interrupt：20260922_110934_8fb00957

本机复制及文件校验产物保存在此 worktree 的
.test-tmp/lk_setup_20260922/evidence；verification/verification.json 记录每个
原始文件 hash、样本数、筛选结果、设置命令摘要及验收图的变换参数。
配置文件已单独命名，不覆盖任何既有 hardware.local.toml。
