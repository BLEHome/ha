# BLEHome: 缺失的那一半 (The Missing Half)
**BTHome 是蓝牙世界的 UDP（广播），BLEHome 是蓝牙世界的 TCP（连接）。**

**Beyond Broadcast. 本地蓝牙控制与 Mesh 组网的新标准。**

BLEHome 是蓝牙广播世界（Broadcasting）向连接世界（Connection-based）演进的必然产物。受 [BTHome](https://bthome.io/) 简洁与高效的启发，我们创建了 BLEHome，旨在解决本地蓝牙生态中长期缺失的“双向连接与 Mesh 控制”环节，**并彻底终结 BTHome 传感器因广播距离有限导致的掉线难题。**

我们向 BTHome 致敬，它为本地蓝牙传感器设立了行业标杆；而 BLEHome 则通过 Mesh 技术作为其距离扩展器（Range Extender），为控制类设备提供同等水平的开放性与标准化。两者共同构成了本地蓝牙 world 完整的“TCP/UDP”协议栈。

> **如果你只是想读取传感器数据，请使用 [BTHome](https://bthome.io/)；如果你需要控制灯泡、插座，或者需要解决 BTHome 传感器距离过远导致的掉线问题，你来对地方了。这就是 BLEHome。**

## 为什么选择 BLEHome Mesh？

传统蓝牙受限于距离和点对点连接，而 **BLEHome Mesh** 释放了智能家居的全部潜力：

- **无限覆盖**：每个设备都是一个节点，通过指令接力实现远超单个网关覆盖范围的网络扩展。
- **自愈可靠**：每条指令拥有多路径传输能力，即使某个节点掉线，网络依然稳健。
- **BTHome 距离扩展器**：彻底解决 BTHome 传感器的短距离传输痛点。BLEHome 节点能够捕捉远端传感器的广播信号，并通过 Mesh 网络进行接力转发，即使在大户型或跨楼层环境下也能确保 BTHome 数据稳定在线。
- **高密度控制**：像管理单一织物网络一样，无缝掌控数百个本地设备（灯具、开关等）。
- **极低延迟**：专为实时控制优化，确保您的“蓝牙 TCP”连接响应如丝般顺滑。

## 核心特性

- **超越广播**：基于稳定、面向连接的控制，彻底告别简单广播的不确定性。
- **Mesh 原生架构**：从底层开始为大规模 BLE Mesh 部署而设计。
- **控制标准**：专为灯具、开关和插座等双向交互设备定义的标准化数据格式。
- **完美互补**：与 BTHome 协同工作，提供完整的本地蓝牙生态系统方案。
- **自动发现**：通过 Home Assistant 自动识别并集成 BLEHome Mesh 网关。

## 架构与协议

BLEHome 采用多层架构，专为可靠性和扩展性而生：

- **传输层 (蓝牙 TCP)**：不同于简单的 Beacon 广播，BLEHome 利用稳定的 GATT 连接。这确保了每条指令都有确认回执 (ACK)，为蓝牙控制带来了如同 TCP 协议般的可靠性。
- **网络层 (Mesh Fabric)**：BLEHome 实现了精密的 Mesh 拓扑。指令在节点间无缝跳跃，允许单个网关跨楼层、穿墙控制设备。
- **应用层 (标准化控制)**：为灯具、开关和传感器提供统一的数据格式，使本地蓝牙控制像 Zigbee 或 Z-Wave 一样标准化。
- **隐私至上**：100% 本地通信。无需云端账号，无数据追踪，无需互联网即可运行。

## BLEHome 生态版图

BLEHome 不仅仅是一个集成插件，它是为本地控制而生的全栈开放标准：

- **[协议规范 (spec)](https://github.com/blehome/spec)**：专为蓝牙优化的、支持多跳接力的可靠 Mesh 控制协议。
- **[核心框架 (framework)](https://github.com/blehome/framework)**：跨平台实现的底层逻辑库，确保不同设备间的互操作性。
- **[Home Assistant 集成 (ha)](https://github.com/BLEHome/ha)**：将 BLEHome 设备接入全球领先智能家居操作系统的官方桥梁。
- **[嵌入式固件 (sdk)](https://github.com/blehome/sdk)**：为主流芯片（ESP32、WCH CH58x/CH59x 等）提供的开发 SDK。
- **[配网与升级 App (app)](https://github.com/blehome/app)**：用于设备一键配网、Mesh 组网管理及固件升级的移动端工具。

## 技术规格

- **IoT Class**: Local Push (本地推送)
- **集成类型**: Device (设备)
- **基础协议**: BLE GATT (面向连接)
- **组网技术**: BLE Mesh (自愈、多跳)
- **安全性**: 纯本地加密，利用标准蓝牙安全特性

## 安装方法

### 方式一：HACS（推荐）
1. 打开 HACS，点击右上角三个点，选择**自定义存储库**。
2. 添加 `https://github.com/BLEHome/ha`，类别选择**集成**。
3. 搜索 **BLEHome** 并安装。

### 方式二：手动安装
1. 将 `blehome` 文件夹拷贝到您的 `custom_components` 目录。
2. 重启 Home Assistant。
3. 前往”设置 > 设备与服务 > 添加集成”，搜索 “BLEHome” 进行添加。

## 联系与社区

- **官方网站**: [blehome.org](https://blehome.org)
- **GitHub**: [BLEHome/ha](https://github.com/BLEHome/ha)
- **协议标准**: BLEHome Mesh 控制协议 v1.0
- **开源协议**: [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
- **官方邮箱**: [admin@blehome.org](mailto:admin@blehome.org)

## 更新日志

### v1.9.0
- **协议文档**: 添加完整的 BLE GATT 通信协议规范。
- **HACS CI**: 添加 HACS 和 Hassfest 验证工作流。
- **品牌图标**: 添加 `brand/` 目录，支持 HA 2026.3+ 本地品牌代理 API。
- **代码清理**: 移除死代码，补全翻译，优化控制器查找逻辑。

### v1.8.0
- **Gamma 2.2 亮度校正**: 线性亮度曲线现在符合人眼感知，实现平滑自然的调光效果。
- **接收端逆 Gamma 校正**: 修复了 Home Assistant 和设备之间的 Gamma 校正不一致问题，确保亮度双向一致。
- **品牌图标**: 添加品牌图标，支持 HA 2026.3+ 本地品牌代理 API。
- **远程设备 OTA 可靠性提升**: 每块超时增加至 120s，最多重试 5 次。对超时和 status=1 均进行重试，远距离 Mesh 设备也能稳定完成 OTA 升级。
- **OTA 验证进度显示**: 验证阶段现在显示真实百分比进度，不再卡在 100%。通知标题在验证阶段显示为"OTA 验证"。
- **清除 BLE 缓冲区残留**: OTA 完成后重新订阅通知，清除累积的 BLE 缓冲区状态，提高后续命令的成功率。
- **配置流程优化**: 移除了冗余配网参数，仅保留固件选择。

### v1.6.0
- **网关转发 OTA 升级**: 支持通过网关对 Mesh 子设备进行固件 OTA 升级，包含进度通知和 `.bin/.hex` 固件文件选择器。
- **子设备解除配网**: `remove_subdevice` 服务现在会在清理配置前向 Mesh 发送解除配网命令 (0xA2)，确保 Mesh 网络的正确管理。
- **设备版本显示**: 子设备固件版本号现在会显示在 Home Assistant 的设备信息面板中。
- **配置流程优化**: 移除了网关选项中的冗余配网参数，仅保留固件选择。
- **OTA 诊断增强**: 增强了 OTA 操作的日志记录和超时诊断，包括逐块 ACK 追踪和最后通知捕获。
- **蓝牙代理注册**: 网关自动注册为 Home Assistant 的蓝牙代理，实现 BTHome 传感器距离扩展。
- **性能与稳定性**:
  - 基于 BLE MTU 的自适应 OTA 帧大小
  - 非阻塞固件文件解析 (`asyncio.to_thread`)
  - VERIFY 阶段尽力执行 + 优雅回退
  - 指数退避的持久化重连机制

### v1.5.0
- 初始 BLEHome 集成版本发布
- 通过蓝牙发现 Mesh 网关
- Mesh 子设备灯光控制（开关、亮度）
- BTHome 代理广播注入
- 直连设备 OTA 固件升级

## 通信协议

详见 [PROTOCOL.md](../../PROTOCOL.md) 完整的 BLE GATT 通信协议规范。

## 鸣谢

BLEHome 本地控制标准的一部分。
