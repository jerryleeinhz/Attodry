# 冷台位移台与焦面跟随工作包

## 2026-10-24 Station deployment and repository reconciliation

The SDK-arrival/output-OFF policy from a1fcd65 was deployed with file backups,
hash verification, the complete station lease pool and an idle-station gate.
The target shadow tree passed 194 hardware/network-blocked regressions; the
installed configuration passed offline parsing. This did not start a scan or
establish current instrument position/output state.

Station-only operation records, private configuration and notebook execution
outputs are retained in the ignored reconciliation backup. Published notebooks
retain their source edits with execution outputs removed. Repository alignment
preserves the deployed acquisition source and private parameter files.

Reconciliation validation: 303 related offline regressions passed with real
instrument and network access blocked; notebook code cells passed syntax checks.

## 当前实现与使用方法（2026-10-24；到位策略为本地离线更新）

已实现 ANC350 RES v4 的窄接口、XYZ 绝对/相对点序列、XY 网格、多点焦面拟合和
逐轴 Z 跟随策略，并接入 Combination、位置证据及分析上下文。以下为生产配置
契约；后面的原始批准计划和发现历史保留，用于区分设计、离线验证和实机验收。
部署/真实运行状态以 PROJECT_HANDOFF 当前阶段为准；本次启动 OFF 仅本地修改，
未部署到当前 LK_setup 扫描，不代表完成该阶段实机验收。原用户确认
的 X/Y/Z 映射与当前预设/LUT 基准保持，程序不自动修复预设或 LUT。

### 2026-10-24 Positioner SDK 到位后输出 OFF（本地离线）

操作者明确选择到位后关闭输出：在 [positioner] 显式设置
arrival_policy="target_reached_output_off"（仅 manual XYZ），每个新目标轴先保持关光，
按原幅频/范围准备并发一次目标；首次读取 SDK target_reached=true 后锁定该次证据，
正常关闭 auto，再设置输出 OFF 并读取完整 XYZ 确认三轴 OFF，才进入光学/锁相采集。
不再用之后的 moving、目标误差、target 位回落或连续稳定窗口否决本次已到位目标；
实际位置继续原样归档，不替换成计划坐标，也不认证到位后持续精度或保持能力。

只在目标变化时重新运动；首点为三轴分别取得真实 SDK 到位及 OFF 证据，同位置不同
功率不因传感器噪声重新追目标。每次只使当前自有轴 ON，其余轴须 OFF。正常 hold
保留最后点且输出 OFF；显式 return_start 按相同策略返回并确认 OFF。故障独立执行
尚未尝试的自有 normal-stop/OFF 各一次；不重放结果不确定的命令，所有原始错误、
审计和 manual/unknown 保留，不能把最终读回 OFF 冒充整次成功。

身份/有限性/安装行程/单步硬界、传感器/EOT、幅频漂移、未知/通信、审计、光 OFF/
外层环境守卫和共享截止时间仍严格失败。启动时 OFF 后完整稳定原点及全部路径校验
仍保留，settle_s 不再用于该策略的目标到位后判稳（原点仍用 settle_s）。
旧配置缺省 arrival_policy="stable_hold"，保持原 moving=false、目标误差/全窗稳定、
焦面及保持输出规则；新策略不用于 focus_plane，历史失败和证据不重解释。
正式位置状态及背景/功率参考上下文保存 arrival_policy 和逐轴到位/OFF 证据；
分析匹配区分新旧策略，缺失字段按历史 stable_hold 解释，不混用资格。
本次仅本地代码与离线验收，未部署、连接或重启 LK_setup。

离线验收：精确待提交候选树194项相关回归通过（44.312 s），覆盖实际驱动/fake SDK、会话、配置、光学/锁相组合、焦面、归档/分析/绘图与硬件编排。仪器 DLL 加载被阻断，未部署、连接或重启 LK_setup。

### 启动时直接设置输出 OFF（2026-10-24；本地离线）

离线验收：精确待提交候选树 172 项回归通过（41.719 s），覆盖驱动、会话、光学/锁相组合、配置、焦面、分析/绘图和硬件编排；仪器 DLL 被阻断。未部署或操作 LK_setup。

open/preflight 保持只读，允许记录静止、健康轴已有的 ON 输出，初始运动或任何身份、
传感器/EOT、位置范围/有限性、未知状态仍拒绝。正式 begin 在独占控制器和新鲜光 OFF/
环境守卫通过后，对基线 ON 轴正常关闭 auto 一次，再调用 ANC_setAxisOutput(axis,0,1)
一次并立即核验 OFF/静止。每个命令前新读三轴状态，最终完整 XYZ 复核；已 OFF 轴不重写。
SDK 无 auto getter，因此保留正常 auto-off ACK，不以 moving=false 冒充反馈已关闭。

关闭保持可能使位置变化，之后必须重新完整判稳、冻结原点与全路径校验，才执行下节
幅频初始化、fresh actual target 和输出准备。共享原 move_timeout_s，不新增 TOML 开关；
正常 finish=hold 仍保留输出。命令/读回、守卫、审计和超时失败均终止，不重试/回滚/重开，
原始 partial/unknown/manual 保留。事件 positioner_outputs_off_requested/positioner_outputs_off
分别记录请求及实际成功。此修改只在本地，未部署或干扰当前 LK_setup 扫描，不结束其他
软件或抢占控制器。以下旧人工 OFF 流程适用于旧部署，新代码由本阶段正常释放后再判稳。

### 启动时按 TOML 初始化幅频（2026-10-24）

完成上述启动 OFF 后，三轴均 OFF、静止且传感器/EOT 正常，先完成原有完整原点
稳定窗、冻结本次起点并验证全部目标、逐轴路径、重复循环连接和显式返回路径，
再进入一次启动幅频初始化。`open/preflight` 及此前原点资格仍只读；启动私有读取
允许记录有限、完整的已有幅频差异，身份、坐标和状态保护不放宽。公开 `read_state`
与后续运行读回始终严格比较 TOML，不把初始差异许可扩散到正式扫描。

初始化重新读取完整 XYZ 身份/位置/状态，确认三轴仍 OFF、静止且健康。仅对与 TOML
不同的 `amplitude_v`、`frequency_hz` 各写一次，三轴最多六次 setter；已匹配的参数
不重写。每次 setter 前执行光 OFF/外层环境守卫和共享截止时间检查，再新鲜检查
三轴状态；每次 setter 后立即读取同一参数并严格核对。最后再取得完整 XYZ 严格
读回和三轴 OFF/静止核验，全部通过后才允许原有 fresh-actual target 预装载、输出
准备及完整原点判稳。`prepare_axis` 本身也拒绝尚未成功初始化的会话。

使用 SDK 的 V/Hz 单位（厂商声明分辨率为 1 mV/1 Hz）；不对 TOML 静默取整，
读回不符即终止，也不猜测新的通用幅频上限。任何写入、读回不符、审计、守卫或截止时间错误均终止本次
会话；不重试 setter、不回滚、不在运行中修复幅频漂移，不改预设、LUT、DC、Home、
坐标或 flash（不调用 `ANC_saveParams`）。部分成功仍保留原始命令、读回和未知状态，
失败不自动重开扫描，也不清除旧失败归档的 manual/unknown 标记。

新增会话事件 `positioner_drive_initialization_requested`、`positioner_drive_initialized`
及底层逐次原始审计，区分请求、实际读回和初始化成功。该阶段计入已有
`move_timeout_s`，不延长预算或缩短原有稳定窗。启动错误写入清理报告，
后续静止读回不能掩盖失败；157项相关离线回归通过，未认证实机设置。

### 配置入口与离线预览

完整格式见 [`config/positioner.example.toml`](../../config/positioner.example.toml)。模板默认
`enabled=false`，若仍将 positioner 选为组合轴，加载会明确拒绝；没有设备会话。
模板中的坐标、安全范围、幅频、容差和次序全部为合成示例，身份及 DLL 为占位值。
可复制为 ignored 私有/临时 TOML，**仅为离线预览**改 `enabled=true` 后执行：

```powershell
python -m attodry_control.combination_cli describe-hardware --config config/positioner.local.toml
```

`describe-hardware` 解析/检查并输出计划及焦面诊断，不加载 SDK、不连接设备、
不移动、不建立实际原点。真正执行仍使用已有 Combination `run` 入口；运行命令
意味着向所选设备写入，必须先完成独立实机运动阶段授权和安全参数确认。
本文没有为未确认设备提供可直接启动的私有运动配置。

位移台全部配置置于 `[positioner]` 及其 nested `axes`、`scan`、`focus_plane` 中，
不新增顶层 `positioner_scan`。启用时下列字段必填，没有隐含实机行程默认值：

| 配置 | 含义 |
|---|---|
| `controller_serial_number` | SDK 原始精确序列字符串，保留其空格，不能用 GUI 槽号代替 |
| `expected_firmware_raw` | 已确认的原生固件整数，不猜版本字符串 |
| `dll_path`、`dll_sha256` | 明确路径及实际 Win64 DLL 字节 SHA256；加载前检查 PE 位数 |
| `coordinate_frame`、`coordinate_frame_confirmed=true` | 操作者确认的同一绝对坐标基准，标签本身不能检测基准漂移 |
| `axes.x/y/z.index` | 固定 `0/1/2`，对应 Daisy Axis 1/2/3 |
| 各轴 `min_position_um/max_position_um` | 安装允许范围，包含起点、每个终点和中间路径 |
| 各轴 `max_move_um` | 一次逐轴运动上限；超限拒绝，不自动拆步绕过 |
| 各轴 `position_tolerance_um` | SDK target range；stable_hold 另用于目标误差/稳定窗口；新策略到位后不再比较 |
| 各轴 `expected_actuator_name/expected_lut_name` | 当前精确预设/LUT 读回，名称不能替代机械铭牌认证 |
| 各轴 `amplitude_v/frequency_hz` | 本次启动的期望幅频；完整原点/路径合格且三轴 OFF/静止后仅设置差异项并读回，之后严格监测 |
| `move_order` | 显式排列 `x/y/z` 各一次；需要操作者按间隙确认 |
| `settle_s/poll_interval_s/move_timeout_s` | 全窗稳定/轮询/共享超时，须 `0<poll<=settle<timeout` |
| `arrival_policy` | 缺省 stable_hold；manual XYZ 可显式 target_reached_output_off，锁定 SDK 到位后关闭输出并保留读回 |
| `finish` | hold 留在最后点；return_start 明确返回冻结起点；输出保持还是关闭取决于 arrival_policy |
| `external_light_off_confirmed` | 无受控 optical 时的外部光路 OFF 声明；有 optical 时仍使用实时 OFF 守卫 |

开启闭环运动时，驱动显式设置 target-ground=false、SDK target range 为配置的位置
容差（μm 转米），并启用输出及自动移动能力。target-ground/range 没有对应 getter，
仅记录写入 ACK，不宣称读回认证；实际运动独立检查完整 XYZ 和状态。stable_hold 不使用单一 target 位代替位置判稳。新策略每次先设置正的 TOML target range，
显式按 SDK 到位位锁定，再关输出并核验 OFF，不证明后续持续位置精度。
幅频仅在上述一次启动阶段设置；不执行 Home、清零、选择执行器、加载 LUT、flash 保存或步进命令。

新代码先在正式 begin 一次关闭旧 auto/保持输出，再要求三轴 OFF/静止及完整原点判稳。
默认 stable_hold 在全路径和幅频初始化通过后，在每轴 OFF 时先将新鲜实际位置写成初始 target，再设置
ground/range、开启输出并完整复验原点，避免追逐旧目标。target 没有 getter，只记录
命令及 ACK；独立实际位置核验不等于目标寄存器读回。正常 hold 保留输出，下一轮由
新代码启动阶段释放；旧部署仍遵守原人工 OFF 准入。

### 点序列、相对原点与焦面

`[positioner.scan].mode` 为 `absolute` 或 `relative`；`z_mode` 为 `manual` 或
`focus_plane`。数值一律为 μm，只有 ANC350 adapter 边界转换为 double 米。

显式路径：`points=[{x_um=...,y_um=...,z_um=...}, ...]`。manual 模式每点必须完整
XYZ；只移动 X 时仍明确写入固定 Y/Z。focus_plane 模式每点只写 XY，配置中出现
独立 Z 即拒绝；绝对模式离线求得 Z，相对模式在冻结原点后用绝对 XY 求 Z。
重复点和往返访问原样保存，不排序或去重。

网格：每轴选择 `x_values_um/y_values_um` 列表，或 `x_ranges_um/y_ranges_um`
范围列表，不能同时提供；范围语法为 `{min,max,scale="linear",points}` 或
`{min,max,scale="linear",step}`，也支持已有严格正值 log 语法。端点包含，返回
访问用显式列表。manual 网格另需固定 `z_um`，相对模式下它表示 Z 偏移；焦面
网格禁止该字段。`order="xy"` 表示 X 外/Y 内，`yx` 相反；`serpentine=true`
每隔一行反转内层访问。最多 10000 个访问点，先检查数量再构建笛卡尔积。

相对起点在首次 positioner 轴入口、外层温度/磁场资格完成且三轴实际位置完整稳定后
冻结一次；所有偏移都加
该起点，不逐点累加、不逐行重新取零。relative focus 只提供 XY 偏移，Z 始终是
焦面绝对高度加显式 `z_offset_um`，不采用起始实际 Z 作离焦偏移。尚未读原点的
离线预览只展示偏移及待验证范围；全路径在输出写入前用实际冻结起点再次校验。

焦面字段及数学契约：

```text
model = plane
points = 至少三个不同、不共线的绝对XYZ聚焦坐标
z_focus(x,y) = z0 + slope_x*(x-x0) + slope_y*(y-y0)
z_target = z_focus(x,y) + z_offset_um
```

`focus_plane.coordinate_frame` 必须与主模块相同。`max_residual_um`、
`rms_residual_um` 为显式有限正数阈值；`focus_tolerance_um` 是实际采集的焦面误差
要求，三者含义不同。标定采用全部点的中心化、共同尺度归一化最小二乘；归一化
设计矩阵条件数须 <=1e6，拒绝共线/近共线、重复 XY、非有限值及残差超限。
不剔除异常点。保存各点残差、RMS/最大残差、秩、条件数、凸包、完整输入哈希；
三点恰好拟合不提供冗余误差证据。拟合质量不是光学分辨率或焦点正确性证明。

只支持 `extrapolation_policy="reject"`：所有目标及逐轴中间 XY 必须在标定点的
凸包内（含边界）；包围矩形不替代凸包。同一对内部终点的逐轴中间角点也可能
越界，遇到时拒绝当前次序。终点在焦面上不证明机械路径无碰撞。

可成对声明 `calibration_temperature_k` 和 `temperature_tolerance_k`，有实际温度
证据时核验适用范围；未知/不适用不能冒充通过。`calibration_conditions` 保存
安装/光路等人工说明。未声明温度条件不认证跨温度补偿，平面不自动响应热漂移。
标定和起点只绑定本次确认坐标系；重连、重新参考或改变安装后须重新确认。

### 运动质量、组合与失败处理

每次逐轴动作前后保持光源 OFF 保护；未知命令结果不重发。全部完整 XYZ 位置
有限、在限位内，传感器连通、无传感器/EOT故障、未运动、输出开启，且目标误差
及稳定全窗范围合格才到位。焦面误差使用**实际 XY**：
`actual_z - plane.evaluate(actual_x,actual_y)`，其中 evaluate 已包含 Z 偏移。
采集前后仍读位置保护，正式窗内不发纠偏动作；漂移/未知/故障拒收并终止。
轮询、guard、写入及完整稳定窗属于同一运动预算，不因重试延长超时。

组合轴可用 `positioner` 单独验证，或放在温度/磁场外层之后、光学/锁相内层之前；
沿用 `illumination_policy="off_before_axis_change"`，不启用 continuous_gate_scan。
光学暗背景使用 per_condition；位置、访问序号、坐标系和焦面标识保留在背景及
功率参考分组。外层锁相量程规则可使用位置坐标，仍受原完整覆盖/唯一性检查。

正常 finish=hold 保留最后位置与保持状态，不自动关输出。return_start 只在显式
选择且来回路径均有效时执行，返回的是冻结实际 XYZ，不擅自移动到“焦面原点”。
存在 optical 时先新鲜核对 OFF，保留其会话读回能力；电气保护完成后才返回，最后
结束光路会话。任一采集/清理/审计失败阻断成功返回，仍独立尝试本会话停止/断开。
异常独立尝试本会话运动的正常停止并核验，记录 primary/stop/close 各自失败。
通信失败保留最后确认位置及当前未知状态，disconnect 成功不等于停稳成功。
不自动恢复励磁、换驱动参数、重开扫描或创建定时检查。

实际 XYZ 为顺序 SDK 读回及前后状态括号，不是同时或连续测量；原始调用有时间证据，
正式记录另带该次位置状态/时间。同步原生 SDK 无取消接口，截止时间可拒绝迟到结果，
不能保证抢断阻塞调用。断开失败或返回空连接句柄时保留未知；同一进程后续新会话
禁止再次 discover，须人工核验，不自动重启或重试断开。纯 `simulate` 的相对点只使用
明确合成的零原点演示坐标，不运行焦面硬件模型，也不是实机位置或聚焦证明。

离线分析分开显示请求坐标、相对偏移、绝对实际 XYZ、焦面误差和访问身份；旧
归档不补零位置，不从当前 TOML 为旧运行补标定。位置维度可用于曲线和 XY 图，
缺测/拒收保留原始审计并从默认有效分析排除。

### 独立验收前仍需确认

本地 fake/故障测试和 describe 不能替代小距离实机验收。每轴允许范围、最大小步、
容差、机械间隙与移动次序尚须操作者确认；此前静止读回差异也需
在真实反馈资格中查明。下一次实机阶段应先独占只读复核，再获授权后小范围
单轴/往返/焦面跟随，最后进入照光与锁相组合。未经该阶段，不将模板数值或
输出 enable 的软件 ACK 解释为该样品的安全运动认证。

本地验收记录：361 项相关硬件阻断回归通过（330.462 s），最终修补后 108 项受影响
测试复验通过（36.998 s，有重叠）；合成模板经真实 describe-hardware 入口离线展开
9 点。测试及预览未连接仪器，不能视为目标机或实机运动验收。

## 原始批准计划与发现历史（以下状态保留原时点）

状态：**planned，2026-10-23 用户批准的设计计划**。本文件定义后续实现与验收，
不表示已有可运行的 positioner 配置解析器、运动驱动或实机验收。当前授权范围为
本地文档，以及经 SSH 对 LK_setup 做只读身份发现；尚未授权真实移动、Home、
坐标清零、驱动参数写入、闭环启用或扫描。

本轮 SSH 主机/文件发现及后续真实只读核对已完成：系统随附规格书第 5 页记录 ANC350/3RES、
X/Y 为 ANPx101/RES/LT、Z 为 ANPz102/RES/LT；第 9 页区分其粗定位与
ANC300 驱动的 ANS 精扫器。13:40 UTC 真实读到 RES 控制器及三轴状态，且成功断开；
未发运动/设置命令。端口 0 当前 LUT 编号与交付 X 轴不同，端口 2 预设名 ANPx101
与其 ANPz102 LUT 不同，位置读回也未经过稳定资格。这些差异及机械轴/安全范围
仍待核验，不能把通信成功当作运动就绪。详细结果与下一步见
本地 ignored `.test-tmp/positioner-discovery-20261023/` 证据。

2026-10-23 操作者后续确认：SDK 轴索引 0/1/2（Daisy Axis 1/2/3）分别对应物理
X/Y/Z，并指定提供的 Daisy Settings 截图为当前配置基准。此映射作为已人工确认
的事实进入后续实现，不再因零基/一基编号差异而列为未知。截图中的三个 Type 均为
ANPx101，各轴 LUT 以操作者确认的当前读回为准，唯一编号只保存在私有证据中；
保留交付表差异作为历史证据，不自动替换当前预设或 LUT。映射确认不代替校准精度、
正方向/安装间隙、允许行程及位置判稳验收。下一阶段为离线实现/测试，真实移动尚未授权。

本模块控制冷台中的 X/Y/Z 平移轴，与光路中的 KDC101 波片 rotator、磁场轴及
温控分别建模。遵守 [硬件与安全规则](../HARDWARE_AND_SAFETY.md)、
[模块开发约定](../PROJECT_MODULE_DEVELOPMENT_GUIDE.md) 和
[测量性能规则](../MEASUREMENT_PERFORMANCE.md)。

## 1. 目标、阶段和当前边界

目标是支持单轴定点、绝对/相对点序列、XY 路径和二维网格，并以人工提供的
多个已聚焦绝对 XYZ 坐标拟合样品焦面。后续改变 XY 时，程序按冻结的焦面
计算 Z，移动并判稳，再进入内层光学/锁相采集。

“焦面跟随”是几何补偿：初版不读取相机或优化光强来寻焦，不在运行中自行更新
标定，也不将平面拟合残差解释为光学焦点准确度。曲面样品、热变形或整体漂移
可能使平面模型不适用。

按以下顺序交付，不能用前一阶段替代后一阶段：

1. `planned`：本批准计划、硬件待确认项、配置与数据契约。
2. 只读身份发现：先查主机设备、软件、SDK、已有配置/日志；确认查询接口无
   写入或状态清除副作用且设备无其他所有者后，才在当前只读授权范围内识别。
3. `offline complete`：纯计算、fake adapter、配置、审计、分析和故障测试。
4. `target offline complete`：目标机 lyr 环境的硬件阻断测试与配置预览。
5. 单独授权的实机验收：只读能力/坐标核验，小距离单轴，往返路径，最后焦面跟随。
6. 单独授权的组合验收：独立运动可靠后才进入 positioner/optical/lockin。

发现期间不扫描未知串口、不猜 IP/端口命令，不为取得型号而运行有副作用的初始化，
不结束其他 GUI、采集或后台进程。现有活动所有者阻断新的设备会话。不能通过
协议读取的执行器型号或机械间隙须明确记为未知，留待设备标签、安装记录或人工确认。

## 2. 型号、轴映射、反馈和坐标基准

| 语义角色 | SDK axisNo | Daisy 界面 | 确认来源 |
|---|---|---|---|
| X | 0 | Axis 1 | 操作者明确确认 |
| Y | 1 | Axis 2 | 操作者明确确认 |
| Z | 2 | Axis 3 | 操作者明确确认 |

实现前记录控制器型号/序列号、固件与 SDK 版本、每轴执行器型号、X/Y/Z 物理
方向、位置传感器类型、反馈有效性、行程和安装允许范围。设备原生单位在 adapter
边界明确转换；计划/显示使用 μm，不能仅从字段名称猜测原生单位。

闭环绝对位置扫描和焦面跟随要求每个参与轴有可验证的位置反馈。若设备只能开环
步进，可另提供“指定步数”功能，但累计步数不是绝对位置证据，不能启用本焦面模式。
软件坐标零点、控制器参考点和样品中心分别记录，不能互相默认为相等。

当前候选 backend 是 ANC350 RES 粗定位；ANC300/ANS 精扫器是独立执行链，
本版不隐式将两者坐标相加或混用。若精扫器影响焦点，焦面标定必须绑定其确认的
保持状态/偏置，变化后重新确认适用性。规格书的行程栏为 N.A.，不能据此设置
软件行程上限或证明安装间隙。真正安全范围仍须取得有效规格与安装确认。

已审阅的 ANC350 C API 使用 `ANC_*`、Windows `__stdcall`、32 位整数/布尔、
指针句柄，线性位置用 double 米；桌面旧 `pyanc350.v2` GUI 按 nm 换算且其 DLL
为 x86。不得混用单位、签名、位数或仅凭文件名推断固件兼容。本轮已验证白名单
只读接口与当前设备通信，运动/设置接口及完整驱动行为尚未验收。
`ANC_getActuatorName/Type` 返回当前选择的执行器预设，并非机械铭牌自动识别；
`ANC_getRefPosition(valid)` 仅 NUM 支持，不能要求 RES 提供该接口来认证坐标。
头文件没有目标位置/到位范围/target-ground 的对应 getter，计划不得伪造这些参数
完整读回；运行保护需按真实能力设计，并以实际位置和状态作独立核验。

标定点与后续目标必须属于同一坐标系。归档不只保存用户的 coordinate_frame 标签，
还要绑定控制器/轴身份、映射、单位和实际参考状态；发生坐标清零、Home/重新参考、
轴映射/传感器改变或基准不明时，必须重新确认标定适用性。重连不能自动证明基准
未变，也不能悄悄重新取零；无法核验则阻断后续运动。

## 3. 绝对、相对和多轴扫描语义

| 用法 | 目标生成方式 | 示例 |
|---|---|---|
| 绝对位置 | 已确认坐标系中的目标 | X=100、105、110 μm |
| 相对位置扫描 | 本次冻结起点加各点偏移 | X₀ + (−5、0、+5) μm |
| 单次相对移动 | 当前新鲜有效位置加一次增量 | 当前 X + 1 μm |
| XY 路径 | 按给定点的访问顺序 | (X₁,Y₁) → (X₂,Y₂) |
| XY 网格 | 明确 X/Y 外内层及方向 | 逐行或蛇形扫描 |

显式列表与 start/stop/points 网格分别支持，端点包含；往返、重复点和访问序号
完整保留。相对扫描起点在本次运行开始时核验并冻结，不逐行、逐重复或在重试时
重新取值。恢复前验证原起点、配置和坐标基准；未知命令结果不通过重复相对移动
来“补做”，避免偏移被执行两次。

多轴初版使用明确的逐轴移动顺序，不默认控制器支持同步轨迹。轴方向、间隙和
驱动能力尚未确认前，不选定运动顺序或假设“升 Z 就更安全”。

## 4. TOML 多点焦面标定

用户输入多个绝对坐标 (x_um, y_um, z_um)，z_um 是该 XY 位置经人工确认聚焦时
的高度。加载这些点只建立标定模型，不自动移动到标定点、不自动寻找焦点。

以下是**拟议配置格式**，演示数字不代表实机位置/安全范围；当前解析器尚不支持，
不能直接用此片段启动运行。正式字段与设备能力确认后，在实现阶段固定契约。

```toml
[positioner]
enabled = true
z_mode = "focus_plane" # 与 manual 互斥

[positioner.focus_plane]
model = "plane"
coordinate_frame = "sample_mount_01"
z_offset_um = 0.0
extrapolation_policy = "reject"
# 还需填写按实际聚焦要求确认的拟合残差阈值；示例不猜测允许值。

[[positioner.focus_plane.points]]
x_um = 0.0
y_um = 0.0
z_um = 100.0

[[positioner.focus_plane.points]]
x_um = 100.0
y_um = 0.0
z_um = 102.0

[[positioner.focus_plane.points]]
x_um = 0.0
y_um = 100.0
z_um = 99.0

[[positioner.focus_plane.points]]
x_um = 100.0
y_um = 100.0
z_um = 101.0
```

轴地址/序列号、运动参数和实际标定坐标保存在本机私有 TOML；示例只提供格式，
不把本机地址和实验数据提交版本库。安全限位、驱动电压/频率/幅度等参数根据确切
型号和用户确认值制定，不提供猜测的通用数值。

### 4.1 拟合与标定质量

采用中心化平面：

```text
z_focus(x,y) = z0 + a*(x-x0) + b*(y-y0)
z_target    = z_focus(x_target,y_target) + z_offset
```

x0/y0 为标定点的中心，a/b 为两方向高度斜率。至少三个不同且在 XY 上不共线
的点；建议四个或更多点覆盖实际扫描区域。多点用最小二乘拟合，在离线阶段输出
每点预测值/残差、RMS、最大绝对残差、矩阵秩及中心化/尺度化后的病态程度。
数值秩/条件判定方式和阈值须在实现中明确并测试，不能用未归一化的坐标大小决定
标定是否有效。拒绝非有限值、退化或病态分布，以及超过配置残差阈值的拟合。

不静默剔除异常点或自动改用另一个模型；点之间不一致须显示原因供用户修正。
恰好三点可形成零残差，但没有冗余点检验误差，不能据此宣称实际聚焦精度为零。

默认只允许 XY 目标在标定点凸包内（含边界），不以包围矩形代替凸包。超出即
拒绝计划。首版不开放无界外推；若后续确有需要，另设计显式外推距离限制和验收。
凸包检查与 XYZ 软限位、机械避让路径是不同检查，互不替代。

### 4.2 Z 模式与标定有效期

- `manual`：使用显式 Z 目标；焦面不参与驱动。
- `focus_plane`：由绝对 XY 计算 Z，再加统一 z_offset_um；禁止同时启用独立 Z 扫描。

z_offset 默认 0，用于显式整体离焦；起始实际 Z 不会被隐式转换为新的偏移。
相对 XY 扫描先将偏移加冻结起点，得到绝对 XY，再求绝对 Z。标定点、系数、
坐标系和偏移在 run 开始时冻结；编辑 TOML 不能改变活动轨迹。

记录标定适用的温度/安装/光路条件。平面只补偿标定时几何倾斜，不能保证跨温度
或磁场/机械条件改变后仍聚焦。需要明确标定适用范围并重新确认，初版不推断温度
补偿、不根据光强变化自动重拟合。坐标参考有效也不代表热漂移后的焦面仍有效。

## 5. 路径、到位和异常状态

离线预览检查所有目标 XYZ、相邻步长、焦面覆盖范围和已知路径约束；开始运行
后使用新鲜真实起点再次验证从起点进入首点的路径。相对模式在尚未读取起点前，
只预览偏移范围，不宣称绝对路径已经安全。

终点在焦面上不证明中间轨迹安全。逐轴次序、所需避让高度及分段策略须依据
安装几何与设备能力确认，检查完整路径上的每个动作和限位；不能把拟合面当作
障碍物模型。若需要安全避让高度，必须显式配置并核验沿途范围，不猜测正 Z
方向或默认“先升后横移”。

以下完整判稳说明适用于默认 stable_hold；显式 target_reached_output_off 采用本文开头的新契约。
默认到位要求新鲜、有限、无故障的 XYZ 位置和设备状态；设备支持的运动完成状态、
各轴目标误差、连续稳定窗口和焦面误差同时满足：

```text
e_focus = z_actual - (z_focus(x_actual,y_actual) + z_offset)
```

焦面误差用实际 XY 求值，避免各轴单独在容差内却因斜率而显著离焦。若实际 XY
已出标定凸包或位置证据未知，也不能判焦面合格。初版不在超时之外无限微调；
命令 ACK 不是到位证明，稳定窗口必须来自实际读回，I/O 与等待计入同一运动预算。

正常结束默认保持最终位置和必要的保持状态。返回起点是显式选项，需验证完整
返回路径和最终到位。Home、坐标清零及关闭伺服/输出保持均不是默认 cleanup。
故障尝试设备支持的正常停止并独立核验；通信失败时保存最后确认位置和当前未知
状态，不能把 close 当停止成功、不能自动回零或重发未知结果的移动命令。保留
primary/stop/close 各自失败及 manual-review 状态。

## 6. Combination、光路、量程与测量质量

计划支持 temperature → positioner → optical → lockin、magnetic → positioner →
optical → lockin，以及 positioner → lockin 等显式次序。位置入口的运动/判稳完成
后才进入内层；不同轴序和组合能力由离线配置明确校验。

存在项目可控光源时，移动前须新鲜确认光源 OFF，运动期间保持该保护，到位后
重新调功、执行锁相剩余滤波等待，再采样。独立/无光学组合须声明并确认外部光路
条件，不能因配置未含 optical 而自动证明无光。正式采集窗内不发运动或焦面修正。
正式窗前后保存位置/焦面证据；漂移超限默认拒收并终止，初版不隐式套用温度的
record_continue 或把漂移当成普通功率恢复。

位置、访问序号、坐标基准和焦面标定标识进入暗背景及 light-power-reference 的
分组上下文，防止不同样品位置/访问的背景或最低功率参考混用。光源 OFF 证明和
锁相正式设置/参考/相位等既有匹配条件继续有效。需要换量程时扩展现有外层坐标
规则，使用已编译目标/访问身份选择，不能通过额外仪器轮询猜测所在位置条件。

## 7. 记录、预览、分析与导出

run 快照保存原始标定点、坐标系/设备/轴身份、拟合中心/系数、残差诊断、内容
哈希、适用实验条件与偏移。逐访问保存请求坐标、冻结起点、绝对目标、实际 XYZ、
误差、焦面误差、轨迹步骤、时间、读回状态及失败原因；保留未完成/拒收原始记录，
默认分析排除。planned/commanded/confirmed actual 三种位置不能相互替代。

离线 describe 预览点数、访问顺序、轨迹、XYZ 范围、最大相邻位移、覆盖检查和
拟合质量。分析支持单轴曲线、XY 信号图、Z/焦面误差图；实际位置和请求位置
分别选择，重复/往返访问不折叠，缺测保留空缺，不插值冒充测量。
CSV/统计/绘图 manifest 保留同一坐标基准、标定及来源证据；旧无 positioner
归档不增加伪造的零位置或套用当前 TOML。

## 8. 拟议文件边界与验证

仅按已确认需求增加文件，实际名称在实现时与现有结构核对：

| 边界 | 拟议修改 |
|---|---|
| 配置与点序列 | positioner_config.py、positioner_points.py，严格单位/互斥/路径校验 |
| 纯计算 | positioner_focus_plane.py，拟合/覆盖/焦面误差，无硬件导入 |
| 窄接口与设备 | positioner 控制策略与已确认型号 adapter，资源独占、状态与停止 |
| 组合 | 现有 registry/hardware/plan/archive 中接入位置轴和保护 |
| 分析 | 现有加载、筛选、分组、绘图、背景和功率参考契约扩展 |
| 文档与示例 | 本工作包、示例 TOML、验收后的操作指南和当前阶段记录 |

计划用硬件阻断的 fake/故障注入测试覆盖：已知平面和多点噪声、共线/近共线、
重复/非有限坐标、残差阈值、凸包边界/外推、单位和坐标基准失效、模式冲突、
起点冻结/恢复、完整中间路径/限位、到位和实际 XY 焦面误差、超时、断线、
未知写结果、正常停止失败、独立 cleanup、关光移动、采集漂移、量程/背景/
功率参考匹配、归档及旧数据兼容。真实小范围测试不能由 fake 测试代替。

性能验收记录每次轴入口/路径段的请求与读回数、完整稳定窗口、剩余锁相等待、
串行响应所有者、超时/停止/失败成本。平面在连接前拟合并冻结，每点只求值；
不得为了减少查询取消到位、状态或正式窗证据。本特性不扩展到未批准的采集速度
优化，不以离线 CPU 时间宣称实机扫描提速。

## 9. 本轮需确认的硬件信息

- 控制器/执行器的确切型号、轴数量和序列身份，SDK 与当前会话所有者。
- XYZ 映射/正方向、各轴原生单位、反馈能力、参考状态与绝对坐标的可恢复性。
- 设备行程、样品安装允许范围、单次移动量、驱动允许参数和安全路径。
- 焦点/到位容差、标定温度/适用范围、光路关光方式及必要避让。

只读发现能证明什么就记录什么：SDK 的控制器名称不自动等于执行器型号，配置
文件中的型号是声明而非硬件读回，主机枚举未发现设备也不证明硬件未安装。
无法确认的项保留为待确认，不据此生成真实移动命令。
