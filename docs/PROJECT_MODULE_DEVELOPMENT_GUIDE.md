# 项目模块开发与操作者协作规范

最后更新：2026-08-25

本文把长期 Lock-in 开发中形成的项目结构、操作者习惯、安全边界、数据审计、
Notebook、Git/worktree 和 `LK_setup` 工作流整理为项目级约定。它适用于 Lock-in、
Temperature、Magnetic field、SMU 和 Integration 等任意模块。

本文描述的是“以后怎样开发和交付”。各仪器的物理接线、型号、命令和安全上限仍以
[`HARDWARE_AND_SAFETY.md`](HARDWARE_AND_SAFETY.md)、对应
[`modules/`](modules/README.md) 文件、厂商手册和用户当次明确确认的信息为准。

## 1. 规则优先级和使用方法

发生冲突时，按下面顺序处理：

1. system/developer 指令、`AGENTS.md` 和不可覆盖的 fail-closed 规则；
2. `HARDWARE_AND_SAFETY.md` 与已确认的硬件/器件安全上限；
3. 用户当前明确提出的目标、物理事实和授权范围，但只能在前两项范围内细化，
   不能覆盖安全不变量；
4. 对应模块的 `docs/modules/<MODULE>.md`；
5. 本文的通用习惯、既有实现细节和历史示例。

不能把一次实验的参数误当成项目通用规则。例如 SR830 的 GPIB 地址、Vxx/Vxy 量程、
谐波选择、电阻和相位阈值都不能成为 SMU 的默认值。若用户的新要求改变了长期工作
习惯，应同步更新本文，而不是只留在聊天记录中。

若用户希望改变既有硬安全边界，应把它作为独立的安全协议变更：先计划，更新配置、
文档和边界测试，并重新 commissioning。不能在提出新边界的同一次真实运行中直接按
新边界操作。

## 2. 协作方式：先计划，批准后持续完成

### 2.1 哪些修改应先给计划

下列工作在动手前先给出可审阅计划：

- 新硬件模块、真实硬件写路径或安全判据；
- 配置结构、记录格式、CLI 日常运行方式或 cleanup 的改变；
- 会影响历史数据解释的分析或拟合规则；
- 跨模块共享文件和 Integration 合并；
- 需要在 `LK_setup` 操作、提交、push 或同步的成组修改。

计划至少写清：目标日常用法、修改文件、配置字段、记录变化、安全/异常/cleanup、
分析界面、测试、分支和目标电脑同步方式。用户回复“同意”“允许”“继续”后，按批准
范围持续完成实现、测试和文档；不要为普通、可逆、范围内的步骤反复询问。

只有遇到以下实质阻塞才停下来：

- 缺少确切仪器型号、手册、接线或硬件允许值；
- 缺少器件安全上限或两个选择会产生不同物理结果；
- 即将进入尚未授权的真实读取、消费状态锁存或写入阶段；
- 需要用户在前面板、接线或样品上做物理确认；
- 目标电脑有无法安全保留或合并的未知本地改动。

### 2.2 授权是分阶段、分作用的

下面的权限不能互相替代：

```text
offline
  -> target offline
  -> real discover/read-only
  -> consume status latches（若查询有副作用）
  -> smallest controlled write
  -> standalone commissioning
  -> daily commissioned run
  -> integration
```

一次只读授权不包含清除状态锁存；一次最小写入不包含完整扫描；一个模块的写入授权
不包含另一个模块。日常命令可以在 commissioning 完成后移除重复的
`--authorize-writes` 型 CLI 参数，但这不取消项目对真实硬件阶段明确授权的要求。

### 2.3 计划和状态必须长期可见

- `docs/DEVELOPMENT_STAGES.md`：完成阶段、日期和验证证据；
- `docs/PROJECT_HANDOFF.md`：当前真实状态、遗留风险和下一步；
- `docs/modules/<MODULE>.md`：模块目标、非目标、接口、阶段和待确认项；
- `<MODULE>_DAILY_OPERATION.md`：commissioned 后的日常操作；
- 当前聊天：本次计划和即时进度，不作为唯一长期记录。

## 3. 当前代码结构和依赖方向

### 3.1 现有层次

| 层次 | 当前主要文件 | 职责 |
|---|---|---|
| 配置契约 | `config.py`, `config/*.toml` | 严格解析、单位、枚举、边界和跨字段校验；不打开硬件 |
| 领域模型 | `models.py`, `records.py` | 不可变状态、condition/attempt/raw/accepted 合约 |
| 窄接口 | `interfaces.py` | 以物理语义定义 `Protocol`，隔离厂商实现 |
| 纯策略/安全 | `safety.py`, `scans.py`, `stability.py`, `lockin_autorange.py`, `gates.py` | 扫描点、稳定性、量程、ramp、限值和 fail-closed 状态机 |
| 厂商适配 | `attodry.py`, `sr830.py`, `sr830_settings.py` | DLL/VISA/命令映射、查询解析、I/O 错误和实际读回 |
| 独立 CLI | `lockin_test.py`, `attodry_test.py`, `temperature_test.py`, `temperature_run.py` | discover、diagnose、commissioning、日常命令和依赖注入 |
| 编排与清理 | `acquisition.py`, `cleanup.py` | 组合已验收接口、重试/恢复、确定性 cleanup；不复制设备安全逻辑 |
| 存储/监控 | `storage.py`, `monitor.py` | SQLite WAL、事件、checkpoint、只读监控 |
| 只读分析 | `analysis.py`, `commissioning_analysis.py` | accepted-only/显式 rejected audit、聚合、拟合和绘图 |
| 操作界面 | `notebooks/` | 薄的只读筛选、绘图和导出界面 |
| 验证 | `tests/` | strict config、fake instrument、故障注入、cleanup、schema、分析和 Notebook |

推荐依赖方向：

```text
TOML
  -> immutable config/domain models
  -> pure safety policy
  -> narrow Protocol
  -> vendor adapter
  -> standalone orchestration/CLI
  -> raw audit record/storage
  -> read-only analysis/Notebook
```

下层不能反向依赖上层。分析代码不得导入或构造 VISA/DLL/硬件驱动；Integration 只组合
已经验收的窄接口，不能重新实现 SR830、SMU、温控或磁场的安全逻辑。

### 3.2 新模块的最小文件边界

只在实际需要时增加文件，不为未来可能性预先建立通用框架：

```text
models.py / records.py             仅增加真正共享的状态或审计字段
interfaces.py                      厂商无关的最窄 Protocol
<module_policy>.py                 可离线测试的纯规则或安全状态机
<vendor_model>.py                  厂商命令适配器
<module>_test.py                   分阶段 commissioning CLI
<module>_run.py                    仅在日常流程稳定后增加
<module>_analysis.py               仅当现有分析层不能表达数据时增加
notebooks/<module>_*.ipynb         仅当交互筛选/绘图确有价值时增加
tests/test_<module>*.py            与以上边界对应的测试
docs/modules/<MODULE>.md           工作包和真实验收状态
docs/<MODULE>_DAILY_OPERATION.md   有可运行日常命令后再增加
```

公共 `__init__.py` 只导出稳定接口；一次性 commissioning helper 不进入公共 API。

## 4. 模块化设计原则

1. **按物理语义命名。** 使用 `lockin_xx`、`lockin_xy`、`gate_top`、
   `gate_bottom`、`field_x`、`field_z`，不用容易接反的“设备 1/2”。
2. **一个模块只控制自己的硬件。** SMU 模块不顺便操作 Lock-in、磁场或温度。
3. **安全策略与命令翻译分离。** 厂商 adapter 负责命令/I/O；独立控制器负责限值、
   状态转换、readback、trip 和 cleanup。
4. **策略尽量是纯函数或可注入状态机。** clock、sleep、resource factory 和 backend
   可注入，离线测试不得碰真实硬件。
5. **不要复制安全逻辑。** Integration 和 CLI 调用同一安全控制器。
6. **只实现已确认需求。** 未知型号、状态位或硬件范围写成待确认项，不猜测命令。
7. **先定义可验证结果。** 每个功能都要能由具体测试、JSON 证据或人工验收判定完成。

## 5. 配置：一个日常入口，分清四类事实

### 5.1 四类配置和证据

| 类别 | 存放位置 | 示例 | 是否版本控制 |
|---|---|---|---|
| 硬件能力 | 代码映射/厂商手册 | 离散档位、命令代码 | 是 |
| 项目批准边界 | 少改的 `<module>_safety.toml`（仅确有必要时） | 最大允许档、允许 ladder、绝对上限 | 是 |
| 本机与日常参数 | `config/hardware.local.toml` | 地址、目标值、扫描网格、接线、电阻、run metadata | 否 |
| 每次实际证据 | JSON/SQLite 记录 | 请求值、readback、状态、配置快照、policy hash | 原始数据不提交 |

不要把同一事实重复放在两个配置文件。只有确实稳定、需要代码审查的项目白名单才单独
建立 safety TOML；普通目标、扫描点、等待和本机地址继续留在同一个
`hardware.local.toml`。若有序安全 ladder 已经决定全部相邻转换，就不要再增加
`max_steps` 之类的重复旋钮。

### 5.2 日常配置约定

- 日常操作者只需维护 `config/hardware.local.toml`；
- 长命令行参数应收回 TOML，日常命令保持短；
- 字段名带单位，例如 `_v`, `_a`, `_hz`, `_s`, `_ohm`, `_k`；
- 枚举列出全部允许值和模式专属字段；
- 未使用模块的空字段不应阻止独立模块命令运行；使用类似
  `load_temperature_operation_config()` 的模块专用 strict loader，只解析相关表，同时
  拒绝拼错的已用字段；
- 在构造硬件 resource 前完成静态和跨字段校验；
- Windows 路径在 TOML 中优先用 `C:/...` 或相对路径；
- `hardware.local.toml`、本机地址、个人路径和秘密保持 Git ignored。

### 5.3 `hardware.example.toml` 是可复制契约

每次增删或改名字段，必须同步：

1. `config/hardware.example.toml`；
2. strict-config 测试；
3. 对应模块/日常 MD；
4. 数据快照和 loader（若 schema 受影响）。

示例应说明字段含义、单位、允许值、默认值、模式组合、互斥/依赖关系、等待时间推导、
cleanup 后状态和安全影响。站点地址是否能版本化必须明确决定，不能在“占位符”和某台
电脑的 GPIB 地址之间静默切换；未确认的 SMU 地址始终只放 local TOML。

### 5.4 避免重复参数

有物理主参数时从它推导时间或范围，不再保留会冲突的秒数副本。例如 Lock-in 以仪器
time constant 乘 `settle_time_constants` 和 `sample_interval_time_constants` 推导等待。
对 SMU 只有在厂商或样品确实给出独立的 settling 概念时才保留 `settle_s`。

## 6. 硬件命令的统一状态机

所有真实写路径按下面顺序设计，并保存每一步证据：

```text
1. 在 open resource 前解析配置、授权和静态安全边界
2. 打开资源并查询 identity / current state / error or status
3. 判断当前状态能否安全接管；未知状态不假定为 zero/off
4. 计算整条请求路径和最坏情况边界
5. read-before-write，只发送最小且幂等的必要设置
6. 每次写入后读取实际 readback 和状态
7. 把 transition/settling 数据与 formal samples 分开
8. formal window 使用严格安全判据
9. 正常、异常和 Ctrl+C 进入同一 cleanup 状态机
10. 保存 primary error、cleanup errors、partial data 和 last confirmed state
11. 只有最终读回确认后才报告 zero/off/clean
```

可恢复的瞬态只能有明确、有限、留痕的 settled recheck；重复出现或不同安全位出现后
停止。不要因为某个仪器状态位与当前项目不用的输出通道有关就直接删除原始位；可以在
模块策略中不把它作为拒绝判据，但仍保存原始状态和理由。

### 6.1 请求值、读回值和分析值

三者分开记录：

- `requested_*`：软件希望仪器设置的值；
- `readback_*` / `actual_*`：仪器实际返回的量化值；
- analysis coordinate：通常使用实际 readback。

合理的仪器分辨率/量化差异通常只作审计，不应单独丢弃数据。但不能把这一规则推广为
忽略安全读回：SMU 实际 V/I 越界、compliance trip、输出状态未知，以及磁场/温度的
安全边界仍必须 fail closed。

### 6.2 通信失败

通信失败不代表 Lock-in 输出已降到最小、gate 已为 0 V、SMU 已关闭、磁场已归零或
温度已稳定。记录最后确认状态，完成所有仍可尝试的 cleanup，并明确要求人工查看前面板
或接线；不能把“发出了关闭命令”写成“已确认关闭”。

## 7. 数据与审计合约

### 7.1 独立 commissioning JSON

推荐从上到下保持一致结构：

```text
schema_version
command / scan
completed
outcome                 # completed / rejected / interrupted
captured_at_utc
run_metadata            # run_name, note, software/git version
measurement_config      # 本地原始记录中的已解析配置和实际 role/resource
safety_policy           # resolved values + hash（若有独立 policy）
preflight
interface_clear         # 若执行过
points / conditions
  point_index / grid indices
  requested values
  actual readbacks
  transitions
  formal samples
  problems
cleanup
last_confirmed_state
error
```

文件名要能被人识别，优先包含 UTC 时间、经过净化的 `run_name`、扫描类型和 outcome；
哈希只用于避免碰撞，不应是唯一信息。二维扫描保存各轴索引、请求坐标和实际坐标。

本地、不得提交的原始审计记录应保存 semantic role、实际 resource address、IDN/型号/
序列号/固件（仪器能提供时）、配置源和已解析配置。密码、令牌和密钥永不进入记录。
公开或共享导出可以脱敏本机地址和个人路径，但 manifest 必须列出被脱敏字段，并保留
semantic role 与足以区分仪器的 identity/serial。若某个既有模块按明确项目策略使用
address-free 原始快照，也必须记录该省略规则和可唯一辨认仪器的 identity；不能静默
删掉复现所需信息。Git 不提交本机地址与 ignored 原始记录保存实际地址并不冲突。

### 7.2 必须保留的内容

- completed、rejected、interrupted；
- 转换期、settling、formal 和 cleanup 样本；
- 中断前已经完成的点和部分读回；
- primary error 与每个 cleanup error；
- 仪器 identity、实际设置/量程/状态；
- 解析后的运行配置、policy hash 和 Git commit；
- `run_name` 与 `note`。

原始失败记录永不因“不好看”而删除。默认分析只加载 completed/accepted 且 clean 的
formal samples；rejected 只能显式 audit opt-in。

### 7.3 集成 SQLite

继续使用 `condition_id`、`attempt_index`、`accepted`、WAL、checkpoint 和事件审计。
每个 condition 最多一个 accepted attempt；raw 数据先落盘，只有满足安全/完整条件后
才能 promote。恢复运行不得覆盖或伪装先前的 rejected attempt。

### 7.4 `database_path` 与 `output_directory`

- `database_path` 指 Integration/正式 acquisition 使用的单个 SQLite 审计数据库，
  保存 run、condition、attempt、raw sample、event、checkpoint 和 accepted 状态；
- `output_directory` 指独立 commissioning/sweep 每次写 JSON 的目录，也是 Notebook
  catalog 的输入目录，通常相对 `hardware.local.toml` 解析到
  `run_data/<module>_commissioning`；它不是数据库；
- 只写独立 JSON 的模块命令不应因无关 `database_path` 未配置而停止；只有进入
  Integration 后才由 SQLite 路径承担跨模块索引、恢复和审计。

## 8. Notebook 与分析习惯

用户通常通过 VSCode Remote SSH/Jupyter 访问 `LK_setup`，因此远端 kernel 的桌面文件
对话框不是可靠交互。Notebook 应是只读薄 UI：

1. 开头只设置一次 `DATA_DIRECTORY`；
2. `Refresh records` 后用下拉/多选选择实际文件；
3. `Only completed records` 默认开启；
4. `Formal samples` 的每种状态在 cell 中解释；
5. `Load selected records` 立即刷新数据和可排除点列表；
6. 只绘制文件中实际存在的 scan/role/harmonic；
7. 缺少某种数据时跳过并提示，不让整个 Notebook 抛错；
8. 异常点剔除只改变当前 selection，不修改原始 JSON；
9. threshold、拟合规则和模型公式集中在可编辑配置 cell；
10. 导出图同时导出可复现的 `selection_manifest.json`。

manifest 至少记录输入文件、record/formal-sample 过滤、手动排除点、换算参数、相位/信噪
阈值、拟合规则、模型参数/判决、代码版本。图中显示实际使用的数值公式，而不只写模型
名称。分析优先使用历史记录中的实际 readback 和当次配置快照，不能用“今天的 TOML”
重新解释旧数据，也不能推断缺失角色或谐波。

Notebook 修改至少验证：合法 JSON、所有 code cell 可编译、fake widgets 下刷新/选择/
加载能填充点列表、单一扫描类型、缺失通道和任意已支持组合不会报错。Notebook 不导入
硬件控制模块，不自动连接仪器。

## 9. 日常 CLI 和诊断体验

commissioning 模块应逐步提供：

```powershell
python -m attodry_control.<module>_test discover
python -m attodry_control.<module>_test diagnose
python -m attodry_control.<module>_test recover-interface
python -m attodry_control.<module>_test monitor-live
python -m attodry_control.<module>_test <daily-command>
```

是否需要某个命令由硬件能力决定，不为形式完整而实现空壳。例如 `recover-interface`
只有在通信层确实存在可安全清理的 pending response 时才增加。

日常 MD 必须给出可复制 PowerShell 命令、正常输出、错误排查、Ctrl+C 后恢复、进程是否
仍在运行的判断、数据位置、最终输出状态、人工前面板检查条件，以及如何确认导入的是
当前 checkout：

```powershell
python -c "import attodry_control; print(attodry_control.__file__)"
git status --short --branch
git log -1 --oneline
```

`LK_setup` 上始终使用 Conda 环境 `lyr`，或明确调用：

```text
C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe
```

正式 sweep/写入与会消费状态锁存的 live monitor 不得并发占用同一 VISA resource。

## 10. Git、worktree 和目标电脑同步

### 10.1 分支与 worktree

- 不同模块并行开发时，一个模块一个 branch/worktree；Integration 最后合并；
- 同一模块已有专用 worktree 时继续使用，不重复创建；
- 若当前只有一个 checkout，同一时刻只允许一个修改代码的任务；
- `config.py`、`acquisition.py`、公共记录和阶段文档是共享冲突热点，先各自提交，再在
  Integration 分支解决冲突并跑全套测试。

推荐分支名：

```text
module/lockin
module/temperature
module/magnetic-field
module/smu
module/integration
```

### 10.2 完成功能后的链路

```text
实现 -> 窄测试 -> 完整相关测试 -> 更新文档
     -> commit -> push 模块分支
     -> 经授权 merge main -> push main
     -> 本机确认 -> LK_setup pull/确认
```

提交、push、merge 和远端 pull 是外部状态动作，应按用户当次要求执行；不能仅因代码已
完成就擅自声称 GitHub 或 `LK_setup` 已同步。

### 10.3 目标电脑有本地 changes

先运行 `git status --short --branch`，逐类处理：

- 要共享的代码/Notebook：review、stage、commit、push；
- `hardware.local.toml`、原始数据和本机地址：保持 ignored，不 stage；
- 暂时保留的 tracked 修改：先逐文件查看 `git diff`，使用带明确路径的 stash 或复制到
  明确备份，再 `git pull --ff-only`；不要把 `tmp/`、`vendor/`、实验数据或 checkpoint
  无差别塞进 stash；
- 不需要的 tracked 修改：只有用户确认后才 discard；
- `.ipynb_checkpoints/`：ignore，不提交；
- vendor DLL 二进制是否纳入 Git 不作为模块安全或阶段验收限制；本机 DLL 路径仍只保存在 ignored local TOML。

`stage` 只表示准备提交，不会帮助 pull 或产生同步。pull 后根据用户目标决定：若需要
“精确的最新 main Notebook”，保留旧版备份但不恢复该文件；若需要“最新 main + 本地
实验修改”，才有选择地恢复并检查 diff，随后重跑 Notebook 编译/widget 测试。

`git pull` 只证明提交图更新。若随后恢复了本地 notebook 修改，目标文件实际是“最新
main + 本地改动”。交付时分别报告 HEAD、`origin/main`、目标电脑 HEAD、指定文件是否
与远端字节一致，以及仍存在的 `M`/`??`；不能只说“同步了”。

`LK_setup` 原则上是运行/验收电脑，不直接在 `main` 上开发 tracked 代码。有价值的目标
电脑修改先导出 patch/明确备份，再回到对应模块 worktree review、测试和提交。

## 11. 测试矩阵和完成标准

每个模块按风险选择并记录以下验证：

| 风险 | 最少验证 |
|---|---|
| TOML/schema | 完整、缺失、未知、非法枚举、边界、跨字段、无关模块未配置 |
| 纯策略 | 每个边界、相邻步进、稳定/超时、重复 transient、不可恢复状态 |
| adapter | fake transcript、命令顺序、解析、错误队列、partial response、close |
| 写路径 | 零 I/O 未授权、preflight、read-before-write、readback、trip、Ctrl+C、cleanup |
| 记录 | completed/rejected/interrupted、partial、last confirmed、schema/load round-trip |
| 存储 | accepted promotion、one accepted attempt、resume/checkpoint、WAL/read-only |
| 分析 | accepted-only、actual readback、缺失组合、异常点、manifest、拟合 synthetic data |
| Notebook | JSON/cell compile、widgets、单一扫描、缺失通道、headless render（若适用） |
| 目标电脑 | `lyr` 的 exact Python、import path、target-offline，不打开硬件 |

完成前检查：

- diff 只包含该目标需要的内容；
- 没有新建推测性旋钮或重复事实来源；
- 假设、限制和未验收阶段写清楚；
- 相关测试实际执行并记录结果；
- `DEVELOPMENT_STAGES.md` 和 `PROJECT_HANDOFF.md` 已更新；
- 若操作过真实硬件，报告实际读/写范围、原始记录位置和最终确认状态；
- 若执行 Git/同步，报告各位置 commit 和剩余本地修改。

## 12. 通用 commissioning 阶段模板

| 阶段 | 目标 | 完成证据 |
|---|---|---|
| M0 contract | 型号、手册、接线、角色、状态位、限值、cleanup 语义 | 用户确认清单和模块 MD |
| M1 pure policy | 不依赖硬件的限值、扫描、状态机 | 边界/故障注入测试 |
| M2 fake adapter | 精确命令 transcript、解析和失败路径 | fake resource 测试，零真实 I/O |
| M3 target offline | 在 `LK_setup` 的 `lyr` 验证安装/import/config | 命令、Python 路径、测试结果 |
| M4 real read-only | discover、identity、状态和读回 | 用户授权、原始记录、无写命令 |
| M5 smallest write | 最安全基线下的最小单步和完整 cleanup | 写命令清单、前后读回、人工确认 |
| M6 standalone daily | 有界单模块流程和简短日常命令 | 正常/中断/注入失败验收 |
| M7 integration | 组合已验收接口 | accepted-only 端到端数据和 cleanup |

状态词使用 `planned`、`offline complete`、`target offline complete`、
`read-only commissioned`、`write commissioned`。较低级完成不能暗示更高级已完成。

## 13. SMU 模块怎样套用本文

当前具体工作包见 [`modules/THREE_SMU.md`](modules/THREE_SMU.md)。`ThreeSmuSession` 定义
最多三台 Keithley 2400 角色：`smu_bias`、`gate_top`、`gate_bottom`。每次运行的
active hardware path 仅包含 scan plan 中的 `fixed`/`sweep` 角色；`off` 角色不要求硬件表，
也不得被打开、读写、清理或记录。其物理状态仍未知。
为便于暂时停用角色，off 角色的已知扫描字段值可保留但不解析；未知/拼错字段仍拒绝，
改回 `fixed`/`sweep` 时恢复完整严格校验。
配置、CLI 和 Notebook 必须调用同一 strict loader、point generator、adapter 与 cleanup。

本项目确认的 Three-SMU 单一安全事实来源是每台独立的 `max_abs_voltage_v` 与
`max_abs_current_a`。Keithley compliance 从相反物理量的 absolute limit 推导并读回验证；
autorange 是量程选择，不能替代 compliance。hardware TOML 不再保存独立 compliance、
leakage、source min/max、ramp、readback tolerance 或 settle 字段。legacy simulation gate
controller 的这些字段不进入真实 Three-SMU 日常配置。
三个硬件角色必须使用相同的单表格式，不要为 gate 另建 `.smu` 子表。Three-SMU
VISA timeout 在 adapter/monitor 代码中固定为 5000 ms，不作为实验参数出现在 TOML。

正式 target 只写一次，不插入软件 ramp；共享 `delay_s` 后读取并记录实际 source/V/I/R/
output/trip/status。requested/readback 差异保留审计，不单独 rejection；实际 V/I 越界、trip、
非有限值、output/status 异常仍 fail closed。cleanup 直接请求零、等待同一 delay、读回记录、
关闭输出；通信失败要求人工确认。

不要预先填猜测的 SMU 边界。用户需要先提供：确切型号/手册、VISA 地址、三角色接线、
source mode、2/4-wire、guard/ground/common、每台最大绝对 V/I、output-off/zero 语义、
interlock 和容性负载限制。
固件若事先未知，可在 S4 的真实只读 identity 查询中确认；不阻止离线 adapter 开发，
但发现不兼容固件时必须在任何写入前停止。

## 14. 模块交付模板

每次交付使用下面格式，未知项明确写“未执行/未授权”，不要省略：

```text
模块：
目标与最终日常命令：
完成到的阶段：
分支/worktree：
提交号（如已提交）：
修改文件：
配置字段和单一事实来源：
记录/schema 变化：
测试命令与结果：
真实硬件是否连接：
读取/消费状态/写入的授权范围：
实际发送的写命令：
保存的原始记录位置（不得提交）：
最终确认状态及人工核验要求：
本机/GitHub/LK_setup commit 与 dirty 状态：
仍待用户确认的物理参数：
Integration 需要知道的接口或限制：
```

## 15. 明确不要做的事

- 不把一次实验参数复制成其他模块默认值；
- 不让一个独立模块因为无关 DLL、SMU 或 Lock-in 字段为空而无法运行；
- 不用几十个 CLI 参数代替一个严格、可归档的 local TOML；
- 不为同一事实保留两个会冲突的配置项；
- 不因读回量化差异删除数据，也不因此放松独立安全判据；
- 不删除 rejected/interrupted/transition/cleanup 原始记录；
- 不让 Notebook 依赖远端桌面文件对话框或导入硬件驱动；
- 不把 `git pull`、`stash pop` 或有本地修改的文件误报成“与 main 完全相同”；
- 不在通信失败后声称输出、gate 或磁场已经安全；
- 不在 exact model/manual/limits 未确认时编造 vendor 命令或宣称 commissioned；
- 不重新引入 PPMS、MultiPyVu、ETO、SR865A 或 rotator 到 active hardware path。
