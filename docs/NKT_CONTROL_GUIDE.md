# NKT 控制脚本：离线使用与后续 commissioning

状态：2026-09-22，离线实现；未进行真实仪器测试。分支 `module/NKT-photonics`。

用户已确认手动换光路，继续使用 `LK_setup`，实验点、功率限制和实际连接关系由用户
后续填写。CONNECT/光纤的可用范围由用户核对。本次脚本不实现自动光路切换。

## 当前能力

| 设备/功能 | 代码状态 |
|---|---|
| EXTREME EXW-12 PP | DLL 设置/读回适配完成 fake-API 验证；发射、当前模式下的百分比设定、PP 分频 |
| VARIA | DLL 适配完成 fake-API 验证；滤波上下边界、ND、运动状态、选配监测百分比 |
| LLTF SWIR HP8 | 完成 1000–2300 nm 模拟调谐和公共 backend 接口；缺少真实厂商 SDK/ABI，硬件模式明确拒绝 |
| 扫描与记录 | 三种模式均可模拟；用户显式列出每个点，保存 JSONL 过程和 JSON 结果 |
| 功率计 | 可注入 `PowerMeter` Python 接口，保存实际 W 和测量位置；没有自动恒功率闭环 |

LLTF 的 <5 nm FWHM 是硬件规格，不能写任意带宽。VARIA 选用 400–840 nm 内的
10–100 nm 带宽；上下边界均必须在范围内，且能用 0.1 nm 表示。读回量化容差仅用于
核对仪器返回值，不能替代波长绝对精度或光谱仪测量。

## SuperK 的读回意味着什么

| 输出字段 | 来源与意义 |
|---|---|
| `current_setpoint_pct` / `power_setpoint_pct` | EXTREME 0x38/0x37 寄存器的设定读回，不是测得的 W |
| `emission_state` / `status_bits` / `interlock` | 光源发射、联锁和错误状态；原始位全部保留 |
| `pulse_picker_ratio` | PP 分频读回，处理历史 1/2 字节两种响应；不猜测实测重复频率 |
| `lower_edge_nm` / `upper_edge_nm` | VARIA LWP/SWP 设置读回；等待运动结束后才接受 |
| `center_setpoint_nm` / `bandwidth_setpoint_nm` | 从上述边界计算的中心和宽度，不是实测谱峰/FWHM |
| `nd_pct` | ND 设定百分比，不等于样品功率百分比 |
| `monitor_pct` | VARIA 选配监测器的读数；仅 `monitor_present=true` 时读取 0x13，无监测器时为 null |
| `measured_power_w` | 仅来自用户注入的功率计，没有功率计则为 null；不把 source/monitor 百分比换算为 mW |

两台设备的寄存器依据为官方 SDK Instruction Manual 第 51–52、64 页；底层 C ABI
依据本次下载 SDK 2.1.16.3027 中的 `NKTPDLL.h`。Python 用标准库 `ctypes` 直接绑定
五个函数（open/close/read/writeU8/writeU16），不需要额外安装 Python wrapper。
SDK 版本是开发依据，旧固件兼容性仍需后续 commissioning 验证。

## 立即运行离线示例

在仓库根目录、现有 Python >=3.11 环境中：

```powershell
$env:PYTHONPATH = 'src'
python -m attodry_control.nkt_test describe
python -m attodry_control.nkt_test validate-config --config config/nkt_simulation.toml
python -m attodry_control.nkt_test simulate --config config/nkt_simulation.toml
```

最后一条应输出 `completed: ...json`。示例数据写到 ignored 的
`run_data/nkt_simulation/`；`simulated=true` 明确标记模拟数据。`simulate` 命令始终
强制使用模拟后端，完全不加载 SDK。示例中的 10% 上限仅供模拟，不是批准的实机限制。

修改 `config/nkt_simulation.toml` 可以试验三种模式。以下为每种模式一个点的字段示意，
用于替换已有 `[[nkt_run.points]]`，不是需要添加到同一次运行的三个不同模式：

```toml
# mode = "broadband"
[[nkt_run.points]]
source_level_pct = 5.0

# mode = "varia_bandpass"
[[nkt_run.points]]
source_level_pct = 5.0
wavelength_nm = 650.0
bandwidth_nm = 20.0
nd_pct = 10.0

# mode = "lltf_swir"
[[nkt_run.points]]
source_level_pct = 5.0
wavelength_nm = 1550.0
```

一个运行只使用一种手动选定的光路；重复 point 表即可做波长、带宽、ND 或光源强度扫描。
不提供切换整套光路的命令。全谱模式不读写无关滤波器，也不把 VARIA 最大带宽当成全谱。

## 本机配置与目标机现状

日常配置仍只有 `config/hardware.local.toml`，对应注释模板位于
[`config/hardware.example.toml`](../config/hardware.example.toml) 的四个 NKT 表。
模拟示例是独立教学数据，不是第二份站点配置。硬件模板的地址、DLL 路径、百分比上限和
实验点故意留空/占位；运行前必须由操作者填写。未使用的其他仪器无需配置完整。

`[nkt_run]` 保存模式、手动路线描述、是否发射、逐点驻留时间、运动超时、轮询间隔、
记录目录、名称、备注和点列表。相对记录目录相对于 TOML 文件位置解析。
`[nkt_source]` 中 `control=current|power` 必须与设备已处于的恒流/恒功率模式一致；
初版不隐式修改 Setup 模式。`allowed_pp_ratios=[]` 意味着不允许修改 PP；如需修改，
填写实机确认的分频集合，并在 point 中加入 `pulse_picker_ratio`。

2026-09-22 经用户授权通过 SSH 在 `LK_setup` 做了只读软件清点：

- 查询 Windows 32/64 位及当前用户卸载列表。
- 检查 Program Files、Program Files (x86)、ProgramData/用户开始菜单，以及
  Downloads、Desktop、Documents 中深度最多四层的相关名称。
- 未找到 NKT CONTROL、PHySpec、LLTF、NKTP DLL/SDK 等匹配项；用户/系统
  `NKTP_SDK_PATH` 均未设置。这个结果不是全盘不存在软件的证明。
- 已确认 `lyr` Python 路径存在。没有安装程序、运行 GUI、加载 DLL、扫描总线或连接仪器。

以后在目标机使用 `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe`，并设置当前 checkout
的 `src` 为 `PYTHONPATH`，避免旧 editable install。当前代码没有同步到目标机，也没有
在目标机执行测试。本次只在开发机运行离线回归。

## Python 接口与功率计扩展

```python
from attodry_control.nkt_config import load_nkt_config
from attodry_control.nkt_control import NktController, SimulatedNkt

config = load_nkt_config("config/nkt_simulation.toml")
backend = SimulatedNkt(config)
result = NktController(config, backend).run()
assert result["completed"]
```

功率计实现 `read_power_w(wavelength_nm: float | None) -> float`，通过
`NktController(..., power_meter=meter, measurement_plane="sample")` 注入。
宽带模式传入 None，适配器应使用经过确认的宽带校准方式，不自行猜一个中心波长。
该接口只记录测量，不调节功率计设置、实现目标 mW、或声称光功率实时保护。
功率计适配器必须自己提供通信超时；后续闭环另行实现。

`NktBackend` Protocol 是公共测试/扩展边界；未来 LLTF 厂商适配器必须提供真实波长
读回、busy/error 和资源关闭语义，再组合 EXTREME。当前 CLI 在缺失这个适配器时于
任何 DLL 加载之前拒绝 LLTF 硬件模式，不返回模拟结果冒充真实读回。

## 后续硬件入口（本次未执行）

仅在用户授权对应阶段、补齐本机配置和核实光路后使用：

```powershell
# 只读诊断；不自动开光、不重置联锁、不写参数。
python -m attodry_control.nkt_test diagnose --authorize-read

# EXTREME/VARIA 写入 commissioning：参数和是否发射由 local TOML 指定。
python -m attodry_control.nkt_test run --authorize-writes --confirm-manual-route
```

真实适配器只打开一个明确端口，关闭 SDK 自动扫描和后台 live mode。EXTREME 与 VARIA
共享连接，并在同一进程中禁止重复占用。其他程序不能同时控制同一端口。
打开端口前验证配置/授权；写路径还要求精确序列号。只读诊断可以暂不填写 expected_serial，
但仍校验模块类型。身份、固件原始字节、寄存器原始回复、DLL hash 和 Git commit 随审计保存。
SDK 的每次 telegram 使用厂商内部超时；配置的 `timeout_s` 是两次返回之间的总状态等待，
不能抢占挂死在本地 DLL 内部的调用。

## 完成、异常和数据解释

接管前必须读到发射关闭；发现已在发射时拒绝接管，不擅自改变原先运行。成功接管后，
每次调滤波前关闭并验证光源，再设置、等待运动结束和读回一致，最后按 `emit` 发射。
源输出超过配置上限、联锁错误、通信失败、超时、Ctrl+C 均终止本次运行。
发射寄存器允许短暂中间态，不能把请求成功当成已完成。

正常和异常收尾都尝试关闭并核验光源；滤波器故障不阻止单独核验光源。
通信失败保存 `last_confirmed_state` 与时间戳，`cleanup.off_confirmed=false` 时需要现场核验。
`source_state_current` 表示最近一次光源读取成功；`state_current` 表示整个所用后端读取成功，
最后仅核验光源的收尾不刷新滤波器状态。它们都只是记录时刻的证据，不能证明断开后的状态。
没有联锁复位、自动重试开光、自动恢复扫描、watchdog 修改、或退出时保留发射功能。

JSONL 每个事件立即 flush；JSON 保存完整结果。通信/设置错误保留 primary error、
cleanup errors、partial register replies、已完成点和未完成点。只有整次完成并关闭核验通过的
运行才把点标为 accepted；分析应同时检查 `completed` 和 `accepted`。模拟结果必须单独筛选。
进程硬崩溃只能留下部分 JSONL，不能保证关光；此版没有配置硬件 watchdog。

## 厂家资料

- [软件和 SDK 下载中心](https://lf.hamamatsu.com/download-center/)
- [官方 SDK 包](https://contentnktphotonics.s3-eu-central-1.amazonaws.com/Software/SDK/SDK%20Current.zip)
- [LLTF 参数表](https://www.nktphotonics.com/wp-json/nktphotonics/v1/download/LLTF/SuperK%20LLTF%20Datasheet.pdf)
- [开发计划与阶段](modules/NKT_PHOTONICS.md)
