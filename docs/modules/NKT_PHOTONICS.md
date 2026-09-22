# NKT Photonics 光源与滤波模块开发计划

日期：2026-09-22。状态：`planned`，计划和依赖调查已完成，N0 硬件契约尚未冻结。
分支：`module/NKT-photonics`。用户原名 `module/NKT photonics` 包含 Git 不允许的空格，改为连字符。
基线：本地 `main` 的 `b9e50f7`。本次只提交规划文档，不实现驱动、不安装 SDK、不连接或写入仪器。

## 1. 目标、模式及硬件能力边界

建立独立 NKT 模块，用同一 Python API/CLI 控制以下语义角色：

| 角色 | 照片对应设备 | 计划控制内容 |
|---|---|---|
| `nkt_source` | SuperK EXTREME EXW-12 PP | 发射开关、受限输出设定、已确认模式下的电流/功率百分比、PP 重复频率，以及状态读回 |
| `nkt_varia` | SuperK VARIA | 可见光波段上下边界、中心波长和带宽换算、内部 ND 衰减、运动/联锁状态 |
| `nkt_lltf` | LLTF CONTRAST SR-SWIR-HP8 | 1000-2300 nm 波长选择、调谐完成确认、状态和能力读回 |

用户最终日常用法是：在一个 `config/hardware.local.toml` 中选择模式、扫描参数、
输出限制和记录名称，通过简短 CLI 执行、停止及查看记录；Python API 调用同一控制器。

拟支持三种显式模式：

1. `broadband`：EXTREME 宽带脉冲输出。不能用 VARIA 的 100 nm 最大常规带宽
   冒充完整超连续谱。是否有直接输出、IR 直通、手动换路或可控切换器，必须先确认。
2. `varia_bandpass`：400-840 nm 内选择中心波长及常规 10-100 nm 带宽。
   校验上下边界同时落在范围内，例如 650 nm / 20 nm 对应 640-660 nm。
   API 分辨率不等于绝对光学准确度；读回滤波边界也不等于光谱仪实测 FWHM。
3. `lltf_swir`：1000-2300 nm 内选择窄带输出。HP8 的 FWHM <5 nm 是硬件规格，
   不是可以随意设置的带宽。初版拒绝该模式的可写 `bandwidth_nm` 请求；不模拟这个能力。

这两台滤波器的主调谐范围之间存在 840-1000 nm 空档。此硬件组合不能承诺整个
400-2300 nm 范围内连续可调的任意带宽输出。未来若需要红外任意带宽或填补空档，
需要另选硬件，不能靠软件宣称实现。

两台滤波器不能默认串联使用其主带通输出：VARIA 可见带通输出与 SWIR 波段不重叠。
若现场使用 VARIA IR 直通馈入 LLTF，需确认真实端口、传输范围和全部残余光出口。
没有已确认的电动切换器时，软件只记录并验证操作者选择的固定路线，不声称能自动换光路。

## 2. 依赖选型及现有项目复用

仓库实际声明见 `pyproject.toml`：Python >=3.11，基础 `dependencies = []`；
`hardware = [pyvisa==1.16.2, qcodes>=0.47,<1]`，`analysis = [matplotlib==3.10.9]`。

| 依赖/资产 | 决策 | 用途与限制 |
|---|---|---|
| 64 位 Python / 现有包 | 复用 | 无需另起应用；项目支持 >=3.11，目标机按现有约定使用 `lyr`，现有记录为 Python 3.12.13 |
| `ctypes` | 复用标准库 | 已在 attoDRY adapter 使用；新模块采用独立 NKT ABI 和返回码，不复用 attoDRY 命令 |
| `tomllib`, `dataclasses`, `typing`, `time`, `json`, `csv`, `sqlite3`, `unittest` | 复用标准库 | 严格配置、单位模型、可注入 clock、审计、离线测试；基础模块不新增 pip 必装包 |
| NKT 官方 SDK + `NKTP_DLL.py` + `NKTPDLL.dll` | 新增厂商运行时 | EXTREME/VARIA 主选方案；官方 wrapper 使用 `ctypes`，DLL 实现 Interbus 协议；需要匹配 64 位架构和 USB 驱动 |
| LLTF 官方 SDK / PHySpec 对应驱动 | 新增，准确包待确认 | 厂家已确认存在 SDK；通用 NKTP SDK 手册未找到 LLTF 专属章节。不可直接假设 EXTREME 的寄存器接口适用于 LLTF；优先独立 C-ABI/ctypes adapter，最终依赖由实物 SDK 决定 |
| `pyvisa==1.16.2` | 可选复用 | 未来接入支持 VISA 的功率计或既有仪器时使用；不能替代 NKT/LLTF 的官方 SDK |
| `qcodes>=0.47,<1` | 保留为可选集成 | 目前用于 Keithley adapter；本次未在仓库发现可直接复用的这三款 NKT 驱动，不把 QCoDeS 变成 NKT 核心硬依赖 |
| `matplotlib==3.10.9` | 复用现有 analysis extra | 后期只读扫描记录、校准与状态可视化，不参与驱动 |
| `pyserial`, `pythonnet`, `nkt-tools`, `pylablib` | 初版不引入 | 官方 DLL 路线暂不需要；只有实际厂商 API 证明需要时才增添依赖，不同时维护两个底层传输实现 |

本机当前 shell 的 `python` 是 Python 3.11.15/64 位，未安装上述 pyvisa、qcodes、
matplotlib、pyserial；这不妨碍标准库/fake-backend 阶段。没有检查或修改目标机环境，
不能把仓库声明、当前 shell 安装状态和 `LK_setup` 安装状态混为一谈。

2026-09-22 已从厂家下载中心获取 SDK 压缩包并只读检查：包含
`SDK_Installer_2.1.16.3027.exe`、安装手册、121 页 API 手册和 ReleaseNotes。
API 手册第 21、25 页确认 NKTPDLL 与官方 Python wrapper/ctypes 路线；
第 51-52 页覆盖 EXTREME，第 64 页覆盖 VARIA。没有运行安装器、加载 DLL、
执行示例或进行设备发现。候选 SDK 版本不能代替旧仪器固件兼容性验证。

现有代码可复用的主要是严格配置模式、Protocol/依赖注入、fake instrument 测试、
read-before-write/readback、状态保留、JSON/SQLite accepted/rejected 合约及只读分析。
激光联锁/发射策略需要独立实现，不能把 SMU 的电压 ramp 或磁体 cleanup 原样套用。

## 3. 最小实现边界与预计文件

下面是后续实现的预计文件，按阶段确有需要再创建，不在计划阶段建立空壳：

| 文件 | 职责 |
|---|---|
| `src/attodry_control/nkt_config.py` | 模块配置、不可变请求/状态/能力类型，纯静态验证 |
| `src/attodry_control/nkt_sdk.py` | 官方 NKTP DLL/wrapper 的薄适配；EXTREME/VARIA 的准确寄存器、返回码与状态解析 |
| `src/attodry_control/lltf.py` | 与上述 adapter 分离的 LLTF 厂商 SDK 适配，精确 ABI 待 N0 核验 |
| `src/attodry_control/nkt_control.py` | 三种模式、波段转换、等待/超时、功率边界、状态机、cleanup，接收 fake backend/clock |
| `src/attodry_control/nkt_test.py` | 分阶段 commissioning CLI；仅在日常用法稳定后决定是否另建 `nkt_run.py` |
| `tests/test_nkt*.py`, `tests/test_lltf.py` | 配置、边界、fake transcript、故障、CLI、记录验证 |
| `config/hardware.example.toml` | NKT 相关配置注释和占位符，与专用 loader 同步实现 |
| `docs/NKT_DAILY_OPERATION.md` | 有可运行命令和验收依据后再创建 |

仅在集成确有需要时修改 `interfaces.py`、`records.py`、`storage.py`、
`acquisition.py` 和 `cleanup.py`；先独立验收，不把初版强接入温控/磁场/SMU 流程。
后期若需要 Notebook，它只读取文件，不加载 SDK 或驱动。

## 4. 配置草案与数据记录

日常入口保持 `config/hardware.local.toml`；下列是字段职责草案，不是当前已支持的配置：

- `[nkt_source]`：显式 backend、SDK 路径、连接标识、预期设备身份、允许操作模式、
  `max_current_pct`/`max_power_pct`、已确认 PP 分频集合、通信超时。
  各限制必须由现场确认，不能把 100% 当作已批准样品上限。
- `[nkt_varia]`：模块身份/地址、滤波范围、ND 设置范围、读回/运动超时。
  通讯层复用同一 EXTREME 总线连接，不按每个模块重新打开同一端口。
- `[nkt_lltf]`：SDK/设备标识、厂商校准或配置文件路径（若 SDK 要求）、确认的调谐范围、
  调谐完成条件。未使用 LLTF 的离线/可见光模式不因 LLTF SDK 缺失而失败。
- `[nkt_run]`：`mode`、已确认 `optical_route`、中心波长和带宽（仅 VARIA）、
  LLTF 波长、输出设定类型与值、固定值或有界扫描点、`run_name`、`note`、
  `output_directory`、预期最终发射状态。标准完成策略先定为关闭发射并核验。

更新共享 top-level table 识别时，必须回归已有温控、Lock-in、SMU loader：
它们允许忽略 NKT 的无关字段，但仍拒绝自己字段的拼写错误；NKT 同理不要求其他设备配置完整。
日常目标、接线、SDK 路径与地址只维护一份，不另外复制到多个运行 TOML。

独立运行先采用现有 JSON/JSONL 记录习惯，保留 `schema_version`、Git/SDK 版本、
配置快照、identity/firmware、`condition_id`、`attempt_index`、requested/readback、
原始状态位、transition/settling/formal、时间戳、accepted/outcome、primary error、
cleanup errors、last confirmed state。断点恢复重做未完成点，并重新预检，不能自动恢复发射。
实际光学测量与设备设置读回分栏：没有功率计/光谱仪时，不生成“实测 mW/中心波长/FWHM”。
后续集成才接 SQLite/WAL，继续保证每个 condition 最多一个 accepted attempt。

## 5. 功率控制、联锁和异常语义

1. 输出设定显式区分 `source_current_pct`、受支持模式的 `source_power_pct`、
   `varia_nd_pct`、实测 `measured_power_w`。百分比及监测百分比不是样品处 mW。
   只有确认固件支持的模式才允许调用；不假设所有 EXTREME 都支持恒功率模式。
2. VARIA 下优先以稳定光源设置配合 ND 改变输出强度；改变源电流可能改变光谱，
   PP 改频会改变平均功率与时域条件，二者均需单独记录。
   LLTF 初版不提供未经厂家证实的独立强度设置。
3. 改波段/路线前按获准流程关闭发射并验证，再改滤波、等待 motion/busy 清除、
   读回边界和状态；重新发射必须仍处于获准运行范围。未确认的机械快门不假设可编程。
4. 每次写前检查身份、钥匙/联锁、错误和当前状态。厂家手册中的 interlock reset 是写操作，
   不在 discover/diagnose 或失败重试里自动执行，不绕过物理安全链。
5. 发射转换允许厂家定义的中间态和有界等待，不将写入成功立即等同于发射状态已确认。
   VARIA 按完整运动状态等待；LLTF 完成条件由其真实 SDK 确认，不能只固定 sleep 55 ms。
6. 超时、错误、Ctrl+C：保留部分数据，已获准写路径尝试关闭发射、核验并记录；
   只读路径不得借 cleanup 写设备。通信失败不报告“已关闭/零功率”，保留最后确认状态，
   标为 unknown 并要求人工确认。断线恢复或进程重启不自动重新开光。
7. SDK 已记载 EXTREME 通信 watchdog。N0 需核实实机支持、心跳来源及并发客户端影响；
   只能在获准写阶段启用并验收，不能承诺软件进程崩溃必然关光。厂家 GUI 与本程序不并发占用。
8. 本模块只控制 NKT 设备，不连接或改写温控、磁场、SR830、SMU；未来 Integration 保留
   `sqrt(Bx^2 + Bz^2) <= 3 T`、语义 Lock-in 角色及原始失败数据规则。

绝对功率闭环作为后续独立增量：确认功率计型号、探测器量程/波段、取样点与路径损耗，
建立校准后，才增加目标 W 值和波长扫描恒功率功能。本次计划不把新增功率计硬件假装已存在。

## 6. 分阶段交付与验收

| 阶段 | 工作 | 完成证据/门槛 |
|---|---|---|
| N0 contract | 核对 EXW-12 PP/VARIA/HP8 手册、SDK、64 位 ABI、状态位、路由及现场边界 | 固化能力矩阵/接口/单位/错误表；LLTF SDK 未核验前不写猜测 adapter；目前未完成 |
| N1 offline policy | 严格配置、三种模式、波段/扫描生成、状态机和模拟设备 | NaN/Inf、越界、中心带宽交叉边界、空档/不支持能力、超时/Ctrl+C、恢复测试；零真实 I/O |
| N2 fake adapters | EXTREME/VARIA DLL 薄封装及已核实的 LLTF adapter | fake ABI/transcript、返回码、端口单例所有权、过期/部分读回、缺 DLL、异步 emission、motion/联锁错误、cleanup 失败测试 |
| N3 target offline | 在实际控制电脑安装匹配运行时，验证包及官方 wrapper/架构 | 使用 `lyr`（若为 LK_setup），检查实际 import 路径、版本/hash、离线测试；不打开端口或运行厂家开光示例 |
| N4 real read-only | 单独授权后读取 identity、固件、输出/联锁/滤波/PP 状态 | 原始记录和命令审计；不设置参数、不清错/复位联锁，必要的有副作用查询另列 |
| N5 smallest write | 先在确认 emission-off 下验证最小滤波步进，再另行授权最小光功率发射及关闭 | 全部前后读回、光路与独立光学测量；各设备分别验收，不把设置读回当作光学标定 |
| N6 standalone daily | 三种实际可用路线、波长扫描、VARIA 带宽与强度扫描、正常/中断收尾 | 有界场景验收及操作文档；没有可控光路切换器时保持人工固定路线 |
| N7 integration | 将验收后的窄 API 接入其他测量模块 | accepted-only 数据、跨模块清理/错误传播、完整离线和获准实机端到端验证 |

N1-N2 不等待硬件到场；已经确认的 EXTREME/VARIA 部分可以先开发，LLTF 真实 ABI
确认前只做语义模拟，不宣称生产 adapter 完成。每阶段都更新本文件、
`DEVELOPMENT_STAGES.md`、`PROJECT_HANDOFF.md`，并记录 exact commit、测试及硬件动作。

拟定 commissioning CLI：`describe`（纯离线）、`simulate`、`diagnose`（获准只读）、
最小设置/发射验收命令。后续 `run` 调用同一 controller。不要为了形式完整添加无明确
语义的 discover/recover 命令；SDK 的 discovery 如果会打开端口，同样属于真实连接阶段。

## 7. 待确认事实及下一步

以下问题不阻止本次计划提交或纯模拟开发，但阻止相应真实控制验收：

- 实际控制电脑是否为现有 `LK_setup`，以及 SDK/USB 驱动版本、设备固件和位数。
- 实际光纤/自由空间拓扑：EXTREME 输出如何接到 VARIA/LLTF，宽带端口和余光端口在哪里，
  哪些路线手动切换，是否存在额外可控快门/切换器。
- 与 SWIR HP8 对应的厂商 SDK/API 手册、运行时和配置/校准文件（若要求）。
- 经现场确认的允许波长、源设定/ND 上限、样品功率上限、联锁和有效输出封闭方式。
- 是否需要实际 mW 闭环；如需要，确认功率计和测量位置。没有这项硬件时交付百分比设定及读回。

本次用户明确要求先列计划并保存分支。因此接下来由用户审阅后推进获准的离线阶段；
真实连接、读状态或写入仍遵守仓库分阶段授权要求。计划本身不是操作激光器的许可。

## 8. 本次验证及 Git 交付

- 已阅读项目规定的五份入口文档及模块约定，核对 `pyproject.toml` 与现有 adapter 分层。
- `PYTHONPATH=src; python -m unittest discover -s tests`：385 tests，OK，5 skipped
  （可选 matplotlib 测试；本次 shell 未安装）。初次沙箱运行受 Windows 临时目录权限
  阻断；相同离线测试在获准环境重跑通过。没有真实设备 I/O。
- 检查文档链接与 diff，只暂存本次新文档和新增段落；已有 SR830 图示与相关未提交段落保留。
- 计划提交到 `module/NKT-photonics` 并推送同名 origin 分支；不合并 main，不同步目标电脑。
- 厂家压缩包/安装器、个人路径、设备地址、校准文件和原始数据不随本次文档提交。

## 9. 厂家依据

- [SDK 和 USB 驱动下载中心](https://lf.hamamatsu.com/download-center/)
- [官方 SDK 包](https://contentnktphotonics.s3-eu-central-1.amazonaws.com/Software/SDK/SDK%20Current.zip)：
  本次包内安装器版本 2.1.16.3027；手册第 21、25、51-52、64 页。
- [VARIA Product Guide](https://www.nktphotonics.com/wp-json/nktphotonics/v1/download/SuperK-VARIA/SuperK%20VARIA%20Product%20Guide.pdf)
- [VARIA Datasheet](https://www.nktphotonics.com/wp-json/nktphotonics/v1/download/SuperK-VARIA/SuperK%20VARIA%20Datasheet.pdf)
- [LLTF CONTRAST Datasheet](https://www.nktphotonics.com/wp-json/nktphotonics/v1/download/LLTF/SuperK%20LLTF%20Datasheet.pdf)：
  PHySpec/SDK 支持及 SWIR HP8 能力，不提供本次所需的完整 ABI。
- [EXTREME/FIANIUM 2019 厂家参数表，经销商副本](https://www.optoprim.it/wp-content/uploads/2020/03/nkt_supercontinuum_extreme_fianium_opt.pdf)
- [EXTREME 2021 厂家手册，研究组保存副本](https://www.alinakarabchevsky.com/_files/ugd/12ada6_bf63d05653ac4448a1966a87c32316b6.pdf)
