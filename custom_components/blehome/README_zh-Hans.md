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
- **[Home Assistant 集成 (ha)](https://github.com/blehome/ha)**：将 BLEHome 设备接入全球领先智能家居操作系统的官方桥梁。
- **[嵌入式固件 (sdk)](https://github.com/blehome/sdk)**：为主流芯片（ESP32、WCH CH58x/CH59x 等）提供的开发 SDK。
- **[配网与升级 App (app)](https://github.com/blehome/app)**：用于设备一键配网、Mesh 组网管理及固件升级的移动端工具。

## 技术规格

- **IoT Class**: Local Push (本地推送)
- **集成类型**: Device (设备)
- **基础协议**: BLE GATT (面向连接)
- **组网技术**: BLE Mesh (自愈、多跳)
- **安全性**: 纯本地加密，利用标准蓝牙安全特性

## 安装方法

1. 将 `blehome` 文件夹拷贝到您的 `custom_components` 目录。
2. 重启 Home Assistant。
3. 前往“设置 > 设备与服务 > 添加集成”，搜索 "BLEHome" 进行添加。

## 联系与社区

- **官方网站**: [blehome.org](https://blehome.org)
- **GitHub**: [blehome/ha](https://github.com/blehome/ha)
- **协议标准**: BLEHome Mesh 控制协议 v1.0
- **开源协议**: [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
- **官方邮箱**: [admin@blehome.org](mailto:admin@blehome.org)

## 鸣谢

BLEHome 本地控制标准的一部分。
