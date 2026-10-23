# Optical waveplate rotator

实机Home还返回通用APT ACTIVE0x20000000；此位明确计入moving，即使方向位短暂
消失也不能判为静止。原始位保留，错误/未知位守卫不变。真实Home中断的失败归档
不重写；随后独立只读读回确认静止，才在新的run中继续用户授权扫描。

实机兼容补充：序列号27264719返回原始状态0x90300104。其中0x10000000在通用APT
定义为POWEROK，而KDC101专用表/头文件标为保留。本适配器仅保留该原始信息位，
不以其出现或缺失证明电源正常、Home完成或运动就绪；独立错误、连接、运动、
位置和其他未知位检查仍执行。首次预检失败的归档保持failed/manual、0正式样本。
相关修正增加0条USB请求/写入/等待，本地86项和目标机32项测试通过（重叠不相加）。

2026-10-19部署补充：LK_setup已部署旋转器及相关解析/归档/绘图代码，目标机
lyr硬件阻断测试158项通过，正式目录离线预览11条件通过；未进行实机旋转或开光。
当前用户本机配置为第一台27264719、机械角0..100度步长10度、目标40uW，第二台关闭。
此前CLD8200故障仍需人工核验，部署不表示故障清除或实机验收完成。
下面的“本地离线开发/未部署”描述保留初始开发阶段的历史边界。

2026-10-19：用户授权本地离线开发。两套设备均声明为 Thorlabs KDC101 +
PRM1/MZ8；第一套装 λ/2（π）波片，斩波器后、样品前，PM取光在波片之后。
第二套通常关闭。允许机械坐标0..360°，尚无光学零点标定，序列号待填写。

这只控制光路中的波片，独立于低温样品旋转、磁场坐标及磁体安全规则。
真实USB识别、最小角度转动和整套光学验收仍须按项目阶段分别授权。
未部署到LK_setup，未连接控制器或移动波片，未提交或推送。

## 配置与日常入口

把 [配置片段](../../config/rotator.example.toml) 合入已有光电TOML。
修改其原有combination_scan.order为rotator -> optical -> lockin；可在光学与锁相
之间加入已配置的SMU轴。初版要求off_before_axis_change及abort，不支持
continuous_gate_scan或skip_wavelength。关闭的rotator_2不需要序列号，也不会加载
驱动、增加扫描维度或连接硬件。启用两套时序列号必须不同，第一套角度外层、
第二套内层形成笛卡尔积；单点列表表示固定角度。每只可选择显式列表或线性范围，
step/points二选一，端点包含。0°与360°访问均保留，不自动折叠或走最短路径。

先离线检查，不能用占位序列号启动真实扫描：

```powershell
python -m attodry_control.combination_cli describe-hardware --config config/photonics.local.toml
```

正式run命令与现有Combination相同；只有实机阶段另获授权并通过验收后才运行。
退出Kinesis GUI等其它控制程序，避免同一个USB控制器出现两个命令所有者。

## Home、运动与清理

home_policy=if_needed默认在位置参考未建立时Home；always每次新会话Home；
require_homed不允许软件发Home，缺少有效基准即拒绝。Home会转动波片并建立机械
基准，不等于波片快轴、入射偏振或样品a/b轴的光学零度。optical_zero_deg省略表示
未标定，不能把缺值当作0。当前分析只显示机械角，不自动计算偏振方向。

所有模块先完成身份/状态检查，之后才配置、Home或移动。每次运动及轮询均检查
已拥有的光学OFF证据。速度与加速度沿用控制器已配置值，不猜测或写入新的速度。
运动结束、homed/enable状态、角度容差和连续settle_s全部满足才到位；移动超时
包括稳定等待，最后守卫结束后仍重新读取实际位置。机械角为度，拒绝未知状态、
硬故障、非有限数据、越界、设置/身份变化与通信失败。实际PRM1/MZ8的角度换算、
命令方向、Home路径、USB响应以及速度仍须实机验收，离线测试不认证物理精度。

正常结束保持最终角度，不自动Home或关掉伺服保持。失败尝试正常Stop；无法核验
停止时保留未知状态、最后确认读回和人工检查要求，不把close当作停止证明。
激光先清理，锁相/SMU保护、旋转器停稳和所有会话关闭各自留下审计结果。

## 光学、背景与采样

每个角度先关光并移动/判稳，再重新进入内层光学点，基于波片后的PM读数调功。
PM读数与样品绝对功率的关系仍由实际取光比例/校准决定。变化通知全部锁相的
共享剩余滤波等待，正式采样期间冻结运动。轴入口、调功完成、暗点前后及正式窗
前/读取/后均核验位置，角度漂移使整窗拒收，不借普通功率恢复掩盖运动故障。

启用自动暗背景时要求per_condition；once_before_scan在旋转器组合中离线拒绝，
以免改变原来“一次运行只采一组暗点”的语义。每组暗背景绑定目标角、访问及
到位时实际角度；原始每次暗读数另保存新鲜实际角度。不同角度不能复用背景。
所有被拒尝试和状态保持原样，默认分析继续排除原始无效数据。

## 数据与分析

坐标为requested/actual.rotator_1_angle_deg及rotator_2_angle_deg；关闭的设备没有列。
运行快照保存角色、label、序列号、型号、Home策略、范围及光学零点是否标定。
Unified Plotting的X、Y、筛选和分组都支持两只机械角，带归档波片标签。
绘制一只时另一只仍是独立维度，须筛选或分组，避免混合不同波片位置。
light/dark使用各自采集时的角度；当前TOML不会重命名旧运行。CSV与保存的绘图
设置保留同一来源，旧无旋转器运行不增加伪造角度。

## 验证与阶段边界

相关测试为test_rotator_config、test_rotator_points、test_thorlabs_kdc101、
test_rotator_combination与test_rotator_analysis。使用tools/run_guarded_tests.py阻断
真实VISA/串口/Windows DLL加载。覆盖一/两台、关闭设备、网格/限值、Home策略、
连续到位、超时/Stop、失联/错误响应、暗背景匹配、正式窗漂移拒收、标签及历史兼容。
全套1,967项通过（745.760s），最终旋转器专项84项通过（29.938s），两组重叠不相加；
git diff --check通过。日志与源码哈希在ignored .test-tmp/rotator-development-20261019。
实机序列号、Kinesis版本、控制器现有速度、机械安装及角度精度验收均未完成。

厂商来源：
- [官方运动控制示例与驱动接口](https://github.com/Thorlabs/Motion_Control_Examples)
- [KDC101手册](https://www.thorlabs.com/images/TabImages/ETN017655-D02.pdf)
- [PRM1Z8/PRM1/MZ8产品资料](https://www.thorlabs.com/catalogpages/obsolete/2016/PRM1Z8E_M.pdf)
