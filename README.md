# BLEHome: The Missing Half
[English] | [简体中文](./custom_components/blehome/README_zh-Hans.md)

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

### Method 1: HACS (Recommended)
1. Open HACS in Home Assistant.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Add `https://github.com/blehome/ha` with category **Integration**.
4. Search for **BLEHome** and install.

### Method 2: Manual
1. Copy the `custom_components/blehome` folder to your `custom_components` directory.
2. Restart Home Assistant.
3. Go to Settings > Devices & Services > Add Integration and search for "BLEHome".

## Contact & Community

- **Website**: [bthome.org](https://bthome.org)
- **GitHub**: [blehome/ha](https://github.com/blehome/ha)
- **Standard**: BLEHome Mesh Control Protocol v1.0
- **License**: [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
- **Email**: [admin@blehome.org](mailto:admin@blehome.org)

## Credits

Part of the BLEHome local control standard.
