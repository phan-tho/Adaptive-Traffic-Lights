# Giao Thức Kết Nối — Hệ Thống Đèn Giao Thông Thích Ứng

> **Mục đích:** Mô tả giao thức truyền thông giữa 3 thành phần: **Pi5** (nhận diện xe) → **Server** (thuật toán điều phối) → **Simulator** (giả lập giao thông). AI implement theo tài liệu này cần đọc toàn bộ trước khi viết code.

---

## 1. Kiến Trúc Tổng Quan

```
[Camera] → [Pi5: xử lý ảnh, đếm xe]
                    │  MQTT JSON (định kỳ)
                    ▼
            [Server: thuật toán thích ứng]
                    │  JSON (điều khiển đèn)
                    ▼
            [Simulator: cập nhật đèn theo lệnh]
        [ESP32: điều khiển đèn thật ở sa hình thực tế]
```

**Tất cả giao tiếp dùng JSON qua MQTT.**

---

## 2. Sơ Đồ Ngã Tư & Lane ID

Giả lập có 4 ngã tư bố cục 2×2. Lane được đánh số theo chiều kim đồng hồ bắt đầu từ 12h (hướng Bắc).

```
┌─────────────────┬─────────────────┐
│  Ngã tư 0 (TL)  │  Ngã tư 1 (TR)  │
│  N=10  E=3      │  N=14  E=2      │
│  S=9   W=0      │  S=13  W=1      │
├─────────────────┼─────────────────┤
│  Ngã tư 2 (BL)  │  Ngã tư 3 (BR)  │
│  N=11  E=7      │  N=15  E=6      │
│  S=8   W=4      │  S=12  W=5      │
└─────────────────┴─────────────────┘
```

### Bảng mapping đầy đủ

| Intersection ID | Vị trí | Lane Bắc (N) | Lane Đông (E) | Lane Nam (S) | Lane Tây (W) |
|:-:|:-:|:-:|:-:|:-:|:-:|
| 0 | Top-Left     | 10 | 3  | 9  | 0  |
| 1 | Top-Right    | 14 | 2  | 13 | 1  |
| 2 | Bottom-Left  | 11 | 7  | 8  | 4  |
| 3 | Bottom-Right | 15 | 6  | 12 | 5  |

**Quy ước hướng:** N = xe đang ở hướng Bắc chờ vào ngã tư, tương tự với E/S/W.

### Đèn tín hiệu mỗi làn

Mỗi làn có **2 đèn độc lập**:
- `straight` — đèn đi thẳng
- `left` — đèn rẽ trái

---

## 3. Pi5 → Server: Báo Cáo Số Lượng Xe

### Endpoint

```
TOPIC: traffic/telemetry
```

Pi5 gửi định kỳ (khuyến nghị mỗi 0.5 giây). Mỗi gói chứa số xe đếm được tại thời điểm đó cho tất cả lanes.

### Payload

```json
{
  "timestamp": 1778784448,
  "device_id": "pi5_intersection_master",
  "data": [
    { "lane": 0, "cars": 0, "bikes": 2 },
    { "lane": 1, "cars": 4, "bikes": 34 },
    { "lane": 2, "cars": 0, "bikes": 4 },
    { "lane": 3, "cars": 2, "bikes": 5 },
    { "lane": 4, "cars": 0, "bikes": 2 },
    { "lane": 5, "cars": 0, "bikes": 33 },
    { "lane": 6, "cars": 1, "bikes": 2 },
    { "lane": 7, "cars": 10, "bikes": 33 },
    { "lane": 8, "cars": 1, "bikes": 1 },
    { "lane": 9, "cars": 0, "bikes": 3 },
    { "lane": 10, "cars": 0, "bikes": 2 },
    { "lane": 11, "cars": 0, "bikes": 9 },
    { "lane": 12, "cars": 0, "bikes": 3 },
    { "lane": 13, "cars": 3, "bikes": 11 },
    { "lane": 14, "cars": 0, "bikes": 2 },
    { "lane": 15, "cars": 0, "bikes": 3 }
  ]
}
```

---

## 4. Server → Simulator: Điều Khiển Đèn

### Endpoint (Simulator lắng nghe)

```
TOPIC: traffic/lights
```

Server chỉ gửi khi thuật toán **quyết định thay đổi**. Không gửi định kỳ nếu không có thay đổi.

### Payload

```json
{
  "timestamp": 1778784448,
  "command_id": "cmd-00342",
  "intersections": [
    {
      "intersection_id": 0,
      "lanes": [
        {
          "lane_id": 10,
          "straight": { "state": "green", "duration": 45 },
          "left":     { "state": "red",   "duration": 45 }
        },
        {
          "lane_id": 3,
          "straight": { "state": "green", "duration": 45 },
          "left":     { "state": "green", "duration": 20 }
        }
      ]
    },
    {
      "intersection_id": 2,
      "lanes": [
        {
          "lane_id": 7,
          "straight": { "state": "red", "duration": 30 },
          "left":     { "state": "red", "duration": 30 }
        }
      ]
    }
  ]
}
```

### Các trường chính

| Trường | Kiểu | Mô tả |
|--------|------|-------|
| `intersections` | array | Chỉ liệt kê ngã tư **có thay đổi** |
| `lanes` | array | Chỉ liệt kê làn **có thay đổi** trong ngã tư đó |
| `straight` / `left` | object | Trạng thái đèn đi thẳng / rẽ trái |
| `state` | string | `"green"`, `"red"`, hoặc `"yellow"` |
| `duration` | integer (giây) | Thời gian của trạng thái này |

Lệnh có hiệu lực **ngay khi Simulator nhận được**.

### Khi khởi tạo/Sau khi đèn chuyển màu/Không có tín hiệu điều khiển

- **Khi khởi tạo**: Các ngã tư sẽ khởi tạo mô phỏng một hệ thống đèn bình thường (2 hướng xanh, 2 hướng đỏ) với thời gian đếm ngược mặc định là **20 giây**. Cụ thể:
  - Ngã tư 0 và 3: Làn Nam, Bắc có đèn `green` (xanh - cả đi thẳng và rẽ trái); Làn Đông, Tây có đèn `red` (đỏ).
  - Ngã tư 1 và 2: Làn Đông, Tây có đèn `green` (xanh); Làn Nam, Bắc có đèn `red` (đỏ).
- **Hết thời gian đếm ngược (Không có tín hiệu điều khiển mới)**: Nếu thời gian (`duration`) đếm ngược về 0 mà Simulator chưa nhận được lệnh điều khiển mới từ Server, hệ thống sẽ tự động luân phiên trạng thái (giống hệ thống đèn cố định): Đèn đang `red` (đỏ) sẽ tự động chuyển sang `green` (xanh) với thời gian 20 giây; Đèn đang `green` hoặc `yellow` sẽ tự động chuyển sang `red` (đỏ) với thời gian 20 giây. Điều này đảm bảo giao thông vẫn hoạt động bình thường kể cả khi mất kết nối.
- **Lưu ý**: Simulator đóng vai trò lắng nghe. Server phải gửi lệnh mới trước khi đèn xanh/vàng hiện tại đếm ngược về 0 nếu không muốn bị ngắt quãng luồng giao thông.

## 5. Server → ESP32: Điều Khiển Đèn Sa hình thực tế
ESP32 giả lập 8 đèn ở ngã tư số 0
### Endpoint (ESP32 lắng nghe)
```
TOPIC: traffic/lights/esp32
```

Server chỉ gửi khi thuật toán **quyết định thay đổi**. Không gửi định kỳ nếu không có thay đổi.

### Payload

```json
{
  "l": [
    {
        "i": 0,
        "s": {
            "c": "r",
            "d": 10
        },
        "l": {
            "c": "r",
            "d": 10
        }
    },
    {
        "i": 3,
        "s": {
            "c": "r",
            "d": 2
        },
        "l": {
            "c": "r",
            "d": 3
        }
    },
    {
        "i": 10,
        "s": {
            "c": "g",
            "d": 3
        },
        "l": {
            "c": "g",
            "d": 5
        }
    },
    {
        "i": 9,
        "s": {
            "c": "g",
            "d": 1
        },
        "l": {
            "c": "g",
            "d": 3
        }
    }
  ]
}
```
Chú thích: "l" ở ngoài cùng là lanes, "i" là id, "s" là straight, "l" là left, "c" là color, "d" là duration.


## 6. Cấu Hình Kết Nối

**Broker MQTT:**
```
IP=3.107.18.217
PORT=1883
```

## 7. Pipeline
Khi chạy phần mềm giả lập, màn hình hiện lên giả lập 4 ngã tư. Tôi dùng camera pi5 để quay màn hình giả lập. pi5 gửi số lượng xe mỗi làn lên server. Server có thuật toán chạy và gửi json điều khiển đèn. Sau đó giả lập lắng nghe tín hiệu điều khiển để điều chỉnh đèn đỏ

<!-- ## 6. Thứ Tự Khởi Động

1. **Simulator** — khởi động trước, lắng nghe port
2. **Server** — khởi động sau
3. **Pi5** — bật camera, bắt đầu gửi dữ liệu -->
