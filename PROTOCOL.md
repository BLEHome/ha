# BLEHome BLE GATT Protocol

**Version:** 1.0 | **Domain:** blehome | **Integration:** BLEHome HA v1.8.0

This document describes the BLE GATT communication protocol between Home Assistant and BLEHome Mesh gateways.

---

## 1. GATT Service

| Parameter | UUID |
|-----------|------|
| Service UUID | `0000ffe0-0000-1000-8000-00805f9b34fb` |
| Characteristic (Write) | `0000ffe1-0000-1000-8000-00805f9b34fb` (write-without-response) |
| Characteristic (Notify) | Same UUID — auto-detected by notify property |

Bluetooth advertisement matching:
- Service UUID `0000ffe0-...`, or
- Manufacturer ID `2007` (0x07D7)

---

## 2. Frame Formats

### 2.1 Fixed-Length Frame (7 bytes)

Used for control, query, and heartbeat.

```
Byte  [0]       [1]           [2]           [3]       [4]       [5]         [6]
      0xA5      addr_low      addr_high     cmd_high  cmd_low   on/off     duty(0-100)
      HEADER    destination   destination   command   command   0x00=off   0-100
                                          (big-endian)         0x01=on    gamma-corrected
```

### 2.2 Variable-Length Frame

Used for provisioning, version query, OTA.

```
Byte  [0]       [1]           [2]           [3...]
      cmd       addr_low      addr_high     payload
```

No length field, no checksum — parsed by command byte and expected length.

---

## 3. Command Codes

| Constant | Code | Direction | Description |
|----------|------|-----------|-------------|
| `QUERY_CMD` | `0x8201` | HA → Device | Poll sub-device state |
| `CONTROL_CMD` | `0x8202` | HA → Device | Set on/off + brightness |
| `BTHOME_PROXY_CMD` | `0x8203` | Device → HA | Proxied BTHome advertisement |
| `CMD_DELETE_NODE` | `0xA2` | HA → Device | Unprovision/delete mesh node |
| `CMD_DELETE_NODE_ACK` | `0x82` | Device → HA | Delete ACK |
| `CMD_GET_VERSION` | `0xAB` | HA → Device | Query firmware version |
| `CMD_GET_VERSION_ACK` | `0x8B` | Device → HA | Version response |
| `CMD_IMAGE_INFO` | `0xA6` | HA → Device | Query OTA image info |
| `CMD_IMAGE_INFO_ACK` | `0x86` | Device → HA | OTA image info response |
| `CMD_OTA_UPDATE` | `0xA7` | HA → Device | OTA write data block |
| `CMD_OTA_UPDATE_ACK` | `0x87` | Device → HA | OTA write ACK |
| `CMD_OTA_VERIFY` | `0xA8` | HA → Device | OTA verify data block |
| `CMD_OTA_VERIFY_ACK` | `0x88` | Device → HA | OTA verify ACK |
| `CMD_OTA_END` | `0xA9` | HA → Device | OTA finalize |

---

## 4. Control Flow

### 4.1 Heartbeat (Keep-Alive)

Sent every 30 seconds to maintain connection.

```
A5 00 00 00 00 00 00
```

Address 0x0001 (gateway).

### 4.2 Query Sub-Device State

```
A5 [addrL] [addrH] 82 01 00 00
```

### 4.3 Control Sub-Device

```
A5 [addrL] [addrH] 82 02 [on] [duty]
```

- **on:** `0x00` = off, `0x01` = on
- **duty:** 0–100, gamma 2.2 corrected:
  ```
  duty = round(100.0 × ((brightness / 255.0) ^ 2.2))
  ```

### 4.4 State Notification (Device → HA)

```
A5 [addrL] [addrH] 82 [01|03] [on] [duty]
```

HA applies inverse gamma 2.2:
```
if duty ≤ 0:       brightness = 0
elif duty ≥ 100:   brightness = 255
else:              brightness = round(255.0 × ((duty / 100.0) ^ (1.0 / 2.2)))
```

---

## 5. Provisioning

### 5.1 Delete Node (Unprovision)

```
Send:  A2 [addrL] [addrH]
ACK:   82 [...]                    (first byte 0x82)
```

Timeout: 8.0 seconds.

---

## 6. Version Query

```
Send:  AB [addrL] [addrH]
ACK:   8B [addrL] [addrH] [maj] [min] [status]
```

- 6 bytes fixed length
- `maj` = version major, `min` = version minor
- `status` = `0x00` on success
- Timeout: 5.0 seconds (silent skip on older devices)

---

## 7. BTHome Proxy Injection

Proxied BTHome advertisements arrive as notifications:

```
Byte  [0]    [1-2]     [3-4]     [5]      [6-11]        [12]      [13+]
      0xA5   addr=0    cmd=0x8203 RSSI     MAC (6 bytes)  len       BTHome payload
```

- RSSI: signed int8 (values > 127 adjusted by -256)
- Minimum length: 13 bytes

The integration reconstructs a BTHome advertisement:
- Service data UUID: `0000fcd2-0000-1000-8000-00805f9b34fb`
- Local name: `BTHome` + MAC last 4 chars
- Injected into HA Bluetooth manager

---

## 8. OTA Firmware Update

### Phase 1: Query Image Info

```
Send:  A6 [addrL] [addrH]
ACK:   86 [addrL] [addrH] [imageSize(4)] [blockSize(2)] [chipType(2)] [status]
```

- `imageSize`: 4 bytes LE — max firmware size
- `blockSize`: 2 bytes LE — optimal block size
- `chipType`: 2 bytes BE
- `status`: offset 11, `0x00` = success
- Timeout: 15.0 s

### Phase 2: Write Data (UPDATE)

```
Send:  A7 [addrL] [addrH] [flashAddrL] [flashAddrH] [data...]
ACK:   87 [addrL] [addrH] [flashAddrL] [flashAddrH] [status]
```

- `flashAddr` = (start_addr / 8) + (offset / 8)
- Frame head = 5 bytes
- Max data per chunk: `min(221, MTU - 3)`, 8-byte aligned
- Default MTU: 247
- Timeout: 120.0 s per block, 5 retries

### Phase 3: Verify Data (VERIFY)

```
Send:  A8 [addrL] [addrH] [flashAddrL] [flashAddrH] [data...]
ACK:   88 [addrL] [addrH] [flashAddrL] [flashAddrH] [status]
```

Same structure and timing as UPDATE. Best-effort — proceeds to END on failure.

### Phase 4: Finalize (END)

```
Send:  A9 [addrL] [addrH]
```

No ACK expected. Followed by 1.0 s delay and notification re-subscription.

---

## 9. Addressing

| Device | Address |
|--------|---------|
| Gateway | `0x0001` (default) |
| Sub-devices | `0x0001` – `0xFFFF` (16-bit) |

Sub-devices are auto-discovered when a notification arrives from an unknown address, firing a `blehome_new_subdevice_found` event.

---

## 10. Firmware Files

Supported formats:
- **.bin:** Start address = `0x1000`
- **.hex:** Intel HEX format, start address = minimum address in file

Intel HEX record types supported:
| Type | Description |
|------|-------------|
| `0x00` | Data |
| `0x01` | End of file |
| `0x02` | Extended segment address |
| `0x04` | Extended linear address |
| `0x03`, `0x05` | Ignored |
