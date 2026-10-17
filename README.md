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
- **[ha](https://github.com/BLEHome/ha)**: The official Home Assistant integration (this repository).
- **[sdk](https://github.com/blehome/sdk)**: Ready-to-use SDKs for popular chips (ESP32, WCH CH58x/59x, etc.).
- **[app](https://github.com/blehome/app)**: Mobile application for seamless Provisioning, Mesh management, and OTA updates.

## Technical Details

- **IoT Class**: Local Push
- **Integration Type**: Device
- **Base Protocol**: BLE GATT (Connection-oriented)
- **Networking**: BLE Mesh (Self-healing, multi-hop)
- **Security**: Local-only. Traffic inside the mesh uses BLE Mesh encryption; the GATT link between Home Assistant and the gateway and the provisioning exchange are not encrypted.

## Installation

### Method 1: HACS (Recommended)
1. Open HACS in Home Assistant.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Add `https://github.com/BLEHome/ha` with category **Integration**.
4. Search for **BLEHome** and install.

### Method 2: Manual
1. Copy the `custom_components/blehome` folder to your `custom_components` directory.
2. Restart Home Assistant.
3. Go to Settings > Devices & Services > Add Integration and search for "BLEHome".

## Changelog

### v2.1.2
- **Multiple meshes**: Provisioning first asks which mesh to join: an existing gateway's mesh or a new one with this device as gateway. Network keys are remembered per gateway; the node address defaults to one above the highest address in the chosen mesh

### v2.1.1
- **Provisioning defaults**: The node address defaults to one above the highest known address, starting at 0x0001 (0 is rejected). The mesh network key is remembered after a successful provisioning and filled in next time; a brand-new mesh gets a random key. The key field is a password field

### v2.1.0
- **Connection recovery**: The gateway reconnects even if the link drops while a reconnect is finishing; a late disconnect callback from an old connection no longer tears down a new one
- **Clean unload and shutdown**: Reloading or removing an entry while the gateway is offline no longer leaves a background reconnect running; loops stop when Home Assistant stops
- **Startup**: The heartbeat and mesh scan keep running after a Home Assistant restart; an offline gateway no longer holds startup for about a minute
- **Connections** use `bleak-retry-connector`; light commands fail fast with an error while the gateway is offline instead of hanging
- **OTA safety**: ACKs are matched by node and flash address; a verify mismatch aborts without finalizing the image; one OTA at a time per gateway; short ACKs no longer crash the update; group and broadcast addresses are rejected
- **OTA input**: Services are admin-only and need `entry_id` or `mac`; local firmware must be bundled or in `allowlist_external_dirs`; downloads and images are size-limited; HEX record checksums are verified
- **Latest firmware** is chosen by version number (V3.10 after V3.4); the options flow offers "Auto"
- **Sub-devices**: Only state reports from unicast addresses create sub-devices; state updates are scoped to their gateway
- **Lights**: The lowest brightness levels no longer turn a light on at zero duty
- **Relayed BTHome**: Frame length, MAC and RSSI are validated; the scanner cache is bounded; per-packet logs moved to debug
- **Registry**: Uses `async_get_device_by_identifier` and `via_device_id` on HA 2026.8+, falling back on older cores
- **Debug entities** (raw write, BTHome mock) are disabled by default for new installs

### v2.0.2
- Fix Bluetooth stall: the BLEHome scanner implements `get_discovered_device_advertisement_data`

### v2.0.1
- Fix translation placeholder conflict and remove misleading "LED blinking" text

### v2.0.0
- TLED_XXXX BLE broadcast name (MAC suffix for device identification)
- Auto-select latest firmware in options
- Auto-route to gateway setup after provisioning
- Periodic mesh scan loop for reliable device discovery
- BLE connection stability: race condition fixes and state cleanup
- Translation updates and documentation improvements

### v1.9.0
- Provisioning, OTA through gateway, and enhanced device management
- Brand icons and gamma correction
- OTA reliability for distant mesh devices

## Contact & Community

- **Website**: [blehome.org](https://blehome.org)
- **GitHub**: [BLEHome/ha](https://github.com/BLEHome/ha)
- **Standard**: BLEHome Mesh Control Protocol v1.0
- **License**: [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
- **Email**: [admin@blehome.org](mailto:admin@blehome.org)

## Credits

Part of the BLEHome local control standard.
