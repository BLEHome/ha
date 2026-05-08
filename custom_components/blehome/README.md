# BLEHome: The Missing Half
[English] | [简体中文](./README_zh-Hans.md)

**BTHome is the UDP (broadcast) of the Bluetooth world; BLEHome is the TCP (connection).**

**Beyond Broadcast. The standard for local BLE control and Mesh networking.**

BLEHome is the connection-based counterpart to the broadcasting world of Bluetooth. Inspired by the simplicity and efficiency of [BTHome](https://bthome.io/), we created BLEHome to address the connection-oriented and Mesh-networking side of the local Bluetooth ecosystem — **effectively eliminating the range limitations of traditional broadcasting sensors.**

We pay tribute to BTHome for setting the standard for local BLE sensors; BLEHome acts as a powerful Mesh-based Range Extender, bringing standardization to controllable devices. Together, they form the complete "TCP/UDP" stack of the local Bluetooth world.

> **If you just want to read sensor data, please use [BTHome](https://bthome.io/). If you need to control bulbs, sockets, or solve BTHome connectivity issues caused by distance, you have come to the right place. This is BLEHome.**

## Why BLEHome Mesh?

While traditional BLE is limited by distance and point-to-point connections, **BLEHome Mesh** unleashes the full potential of your smart home:

- **Unlimited Coverage**: Every device acts as a node, relaying commands to extend the network far beyond the reach of a single gateway.
- **Self-Healing Reliability**: Multiple paths for every command ensures that even if one node goes offline, the network stays robust.
- **BTHome Range Extender**: Effectively solves the short-range limitation of BTHome. BLEHome nodes capture broadcasts from distant sensors and relay them through the Mesh fabric, ensuring 100% coverage even in large homes.
- **High-Density Control**: Seamlessly manage hundreds of devices (lights, switches, etc.) as a single, unified local fabric.
- **Low Latency**: Optimized for real-time control, ensuring your "TCP of Bluetooth" remains responsive and snappy.

## Features

- **Beyond Broadcast**: Reliable, connection-oriented control that moves past simple advertising.
- **Mesh-First Architecture**: Built from the ground up to support massive BLE Mesh deployments.
- **The Standard for Control**: Specifically designed for bidirectional devices like lights, switches, and sockets.
- **Perfect Complement**: Works alongside BTHome to provide a complete local Bluetooth ecosystem.
- **Automatic Discovery**: Seamlessly identifies BLEHome Mesh gateways via Home Assistant.

## Architecture & Protocol

BLEHome is built on a multi-layer architecture designed for reliability and scale:

- **Transport Layer (The TCP of Bluetooth)**: Unlike simple beacons, BLEHome utilizes stable GATT connections. This ensures every command is acknowledged (ACK), providing the same reliability for control that TCP brings to the internet.
- **Network Layer (The Mesh Fabric)**: BLEHome implements a sophisticated Mesh topology. Instructions seamlessly hop from node to node, allowing a single gateway to control devices across floors and through walls.
- **Application Layer (Standardized Control)**: A unified data format for lights, switches, and sensors, making local BLE control as standardized as Zigbee or Z-Wave but with the ubiquity of Bluetooth.
- **Privacy First**: 100% local communication. No cloud accounts, no data tracking, and no internet required for your home to function.

## The BLEHome Ecosystem

BLEHome is more than just an integration; it is a full-stack open standard for local control:

- **[spec](https://github.com/blehome/spec)**: The high-efficiency, multi-hop Mesh control protocol specification.
- **[framework](https://github.com/blehome/framework)**: Cross-platform logic libraries implementing the BLEHome standard.
- **[ha](https://github.com/blehome/ha)**: The official Home Assistant integration (this repository).
- **[sdk](https://github.com/blehome/sdk)**: Ready-to-use SDKs for popular chips (ESP32, WCH CH58x/59x, etc.).
- **[app](https://github.com/blehome/app)**: Mobile application for seamless Provisioning, Mesh management, and OTA updates.

## Technical Details

- **IoT Class**: Local Push
- **Integration Type**: Device
- **Base Protocol**: BLE GATT (Connection-oriented)
- **Networking**: BLE Mesh (Self-healing, multi-hop)
- **Security**: Local-only, utilizing standard BLE security features.

## Installation

1. Copy the `blehome` folder to your `custom_components` directory.
2. Restart Home Assistant.
3. Go to Settings > Devices & Services > Add Integration and search for "BLEHome".

## Contact & Community

- **Website**: [blehome.org](https://blehome.org)
- **GitHub**: [blehome/ha](https://github.com/blehome/ha)
- **Standard**: BLEHome Mesh Control Protocol v1.0
- **License**: [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
- **Email**: [admin@blehome.org](mailto:admin@blehome.org)

## Changelog

### v1.7.0
- **OTA Reliability for Distant Devices**: Increased per-chunk timeout to 120s with 5 retries. Retry on both timeout and non-zero status (status=1). Distant mesh devices can now complete OTA reliably.
- **OTA Verification Progress**: Verification phase now shows actual percentage progress instead of stuck at 100%. Notification label changes to "OTA 验证" during verify.
- **Stale Buffer Cleanup**: Re-subscribes to BLE notifications after OTA to clear accumulated buffer state, improving success rate for subsequent commands.
- **Config Flow Refinements**: Removed redundant provisioning parameters, simplified to firmware selection only.

### v1.6.0
- **OTA Update via Gateway**: Mesh sub-devices can now be firmware-updated through the gateway via the `ota_update` service. Includes progress notifications, file picker for `.bin/.hex` firmware files.
- **Sub-device Unprovisioning**: The `remove_subdevice` service now sends an unprovision command (0xA2) to the mesh before cleanup, ensuring proper mesh network hygiene.
- **Device Version Display**: Sub-device firmware version numbers now appear in the Home Assistant device info panel.
- **Config Flow Cleanup**: Removed redundant provisioning parameters from the gateway options flow. Simplified to firmware selection only.
- **OTA Diagnostics**: Enhanced logging and timeout diagnostics for OTA operations, including per-chunk ACK tracking and last-notification capture.
- **Bluetooth Proxy Registration**: The gateway automatically registers as a Bluetooth proxy in Home Assistant, enabling seamless BTHome sensor range extension.
- **Performance & Stability**:
  - Adaptive OTA frame sizing based on BLE MTU
  - Non-blocking firmware file parsing (`asyncio.to_thread`)
  - Best-effort VERIFY phase with graceful fallback
  - Persistent reconnection with exponential backoff

### v1.5.0
- Initial BLEHome integration release
- Mesh gateway discovery via Bluetooth
- Light control (on/off, brightness) for mesh sub-devices
- BTHome proxy advertisement injection
- OTA firmware update for direct-connected devices

## Credits

Part of the BLEHome local control standard.
