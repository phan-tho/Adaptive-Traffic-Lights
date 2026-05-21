import paho.mqtt.client as mqtt
import time
import json
import uuid

# ==============================================================================
# !!! HẰNG SỐ CẤU HÌNH KẾT NỐI MQTT !!!
# ==============================================================================
BROKER_IP = "3.107.18.217"
BROKER_PORT = 1883
TOPIC_TELEMETRY = "traffic/telemetry"
TOPIC_LIGHTS = "traffic/lights"

# ==============================================================================
# !!! BỐ CỤC LÀN ĐƯỜNG CỦA NGÃ TƯ SỐ 3 (CHỈ XÉT NGÃ TƯ NÀY) !!!
# ==============================================================================
# Bố cục 4 hướng của Ngã tư 3 theo PROTOCOL.md
LANE_N = 15  # Làn hướng Bắc
LANE_E = 6   # Làn hướng Đông
LANE_S = 12  # Làn hướng Nam
LANE_W = 5   # Làn hướng Tây

ACTIVE_LANES = {LANE_N, LANE_E, LANE_S, LANE_W}

# ==============================================================================
# !!! THAM SỐ THUẬT TOÁN (BẠN CÓ THỂ ĐIỀU CHỈNH THOẢI MÁI Ở ĐÂY) !!!
# ==============================================================================
TOTAL_CYCLE_GREEN = 40.0  # Tổng thời gian xanh cơ sở cho một chu kỳ (giây)
GREEN_MIN = 12.0          # Thời gian xanh tối thiểu cho một trục (giây)
GREEN_MAX = 30.0          # Thời gian xanh tối đa cho một trục (giây)
LEFT_RATIO = 0.60         # Tỷ lệ thời gian rẽ trái trên tổng thời gian xanh (60%)

# Trọng số xe dùng để quy đổi lưu lượng
CAR_WEIGHT = 1.0          # Xe ô tô = 1.0
BIKE_WEIGHT = 0.75        # Xe máy = 0.75

# ==============================================================================
# !!! BỘ NHỚ LƯU TRỮ XE THỜI GIAN THỰC !!!
# ==============================================================================
latest_counts = {
    LANE_N: {"cars": 0, "bikes": 0},
    LANE_E: {"cars": 0, "bikes": 0},
    LANE_S: {"cars": 0, "bikes": 0},
    LANE_W: {"cars": 0, "bikes": 0}
}

client = None

# Các biến phục vụ đồng bộ hóa thời gian thực tế với Simulator
last_sim_time = None
last_telemetry_time = time.time()
phase_elapsed = 0.0

# ==============================================================================
# !!! HÀM GHI & ĐỌC FILE JSON (ĐƯỢC GỌI NGAY TRƯỚC KHI TRỤC LÊN XANH) !!!
# ==============================================================================
def save_and_load_volumes(axis):
    """
    Cập nhật lượng xe của trục chuẩn bị xanh vào file pending_volumes.json,
    (giữ nguyên thông tin của trục còn lại), sau đó đọc ngược lại để tính toán.
    """
    if axis == 'NS':
        lane1_id, lane2_id = LANE_N, LANE_S
    else:
        lane1_id, lane2_id = LANE_E, LANE_W
        
    lane1_raw = latest_counts[lane1_id].copy()
    lane2_raw = latest_counts[lane2_id].copy()
    
    # NẾU LÀN TRỐNG (XE = 0): Mặc định an toàn là 2 xe máy, 1 ô tô
    if lane1_raw["cars"] == 0 and lane1_raw["bikes"] == 0:
        lane1_raw = {"cars": 1, "bikes": 2}
    if lane2_raw["cars"] == 0 and lane2_raw["bikes"] == 0:
        lane2_raw = {"cars": 1, "bikes": 2}
        
    axis_data = {
        "lane1": {"id": lane1_id, "cars": lane1_raw["cars"], "bikes": lane1_raw["bikes"]},
        "lane2": {"id": lane2_id, "cars": lane2_raw["cars"], "bikes": lane2_raw["bikes"]}
    }
    
    # CỐ GẮNG ĐỌC FILE JSON CŨ ĐỂ GIỮ LẠI THÔNG TIN TRỤC KIA
    payload = {"intersection_id": 3}
    try:
        with open("pending_volumes.json", "r") as f:
            old_payload = json.load(f)
            # Khôi phục thông tin nếu có
            if "NS" in old_payload:
                payload["NS"] = old_payload["NS"]
            if "WE" in old_payload:
                payload["WE"] = old_payload["WE"]
    except Exception:
        pass # Bỏ qua nếu file không tồn tại hoặc lỗi định dạng
        
    # CẬP NHẬT TRỤC HIỆN TẠI
    payload[axis] = axis_data
    
    # GHI LẠI FILE JSON VỚI CẢ 2 TRỤC
    try:
        with open("pending_volumes.json", "w") as f:
            json.dump(payload, f, indent=4)
    except Exception as e:
        print(f"[JSON ERROR] Ghi file JSON lỗi: {e}")
        
    # TRẢ VỀ DỮ LIỆU CỦA TRỤC ĐANG XÉT ĐỂ THUẬT TOÁN TÍNH TOÁN
    return payload[axis]

# ==============================================================================
# !!! THUẬT TOÁN TÍNH TOÁN PHA THÍCH ỨNG CHO NGÃ TƯ 3 !!!
# ==============================================================================
def calculate_new_phase(current_axis):
    """
    Đọc từ file JSON vừa ghi để tính toán tổng thời gian xanh T
    và ranh giới thời gian cho 3 Stage hoạt động của trục xanh.
    """
    # 1. Đọc dữ liệu từ file JSON
    loaded = save_and_load_volumes(current_axis)
    
    # Tính volume của 2 làn trên trục hoạt động
    V1 = loaded["lane1"]["cars"] * CAR_WEIGHT + loaded["lane1"]["bikes"] * BIKE_WEIGHT
    V2 = loaded["lane2"]["cars"] * CAR_WEIGHT + loaded["lane2"]["bikes"] * BIKE_WEIGHT
    V_active = V1 + V2
    
    # 2. Lấy volume của trục đối diện từ bộ nhớ đệm (áp dụng safe default nếu trống)
    other_axis = 'WE' if current_axis == 'NS' else 'NS'
    if other_axis == 'NS':
        lane1_id, lane2_id = LANE_N, LANE_S
    else:
        lane1_id, lane2_id = LANE_E, LANE_W
        
    l1 = latest_counts[lane1_id]
    l2 = latest_counts[lane2_id]
    
    c1, b1 = (1, 2) if l1["cars"] == 0 and l1["bikes"] == 0 else (l1["cars"], l1["bikes"])
    c2, b2 = (1, 2) if l2["cars"] == 0 and l2["bikes"] == 0 else (l2["cars"], l2["bikes"])
    
    V_inactive = (c1 * CAR_WEIGHT + b1 * BIKE_WEIGHT) + (c2 * CAR_WEIGHT + b2 * BIKE_WEIGHT)
    
    # 3. Tính tổng thời gian xanh (T) theo tỷ lệ lưu lượng 2 trục
    if V_active + V_inactive == 0:
        T = TOTAL_CYCLE_GREEN / 2.0
    else:
        T = (V_active / (V_active + V_inactive)) * TOTAL_CYCLE_GREEN
        
    # Giới hạn tổng xanh nằm trong khoảng GREEN_MIN và GREEN_MAX
    T = max(GREEN_MIN, min(GREEN_MAX, T))
    
    # 4. Phân bổ thời gian cho 3 Stage
    left_time = T * LEFT_RATIO
    MIN_STAGE = 4.0
    remaining_pool = max(0.0, T - 3.0 * MIN_STAGE)
    
    # Phân phối thời gian dựa trên đóng góp lưu lượng của từng làn
    share1 = left_time * (V1 / (V1 + V2)) if (V1 + V2) > 0 else left_time / 2.0
    share3 = left_time * (V2 / (V1 + V2)) if (V1 + V2) > 0 else left_time / 2.0
    share2 = T - (share1 + share3)
    
    share1 = max(0.0, share1)
    share2 = max(0.0, share2)
    share3 = max(0.0, share3)
    
    total_shares = share1 + share2 + share3
    if total_shares > 0:
        d1_duration = MIN_STAGE + remaining_pool * (share1 / total_shares)
        d2_duration = MIN_STAGE + remaining_pool * (share2 / total_shares)
        d3_duration = MIN_STAGE + remaining_pool * (share3 / total_shares)
    else:
        d1_duration = MIN_STAGE + remaining_pool / 3.0
        d2_duration = MIN_STAGE + remaining_pool / 3.0
        d3_duration = MIN_STAGE + remaining_pool / 3.0
        
    # Xác định ranh giới chuyển tiếp Stage 1 -> 2 và Stage 2 -> 3
    t1_boundary = d1_duration
    t2_boundary = d1_duration + d2_duration
    
    print("----------------------------------------------------------------------")
    print(f"[THAY ĐỔI PHA] Ngã tư 3 | Trục {current_axis} lên XANH")
    print(f" -> Lượng xe trục xanh: {V_active:.1f} | Lượng xe trục đỏ: {V_inactive:.1f}")
    print(f" -> Cấp phát tổng thời gian xanh T: {T:.1f} giây")
    print(f" -> Stage 1 (D1 Xanh): {d1_duration:.1f}s")
    print(f" -> Stage 2 (D1 & D2 thẳng Xanh): {d2_duration:.1f}s")
    print(f" -> Stage 3 (D2 Xanh): {d3_duration:.1f}s")
    print("----------------------------------------------------------------------")
    
    return t1_boundary, t2_boundary, T

# ==============================================================================
# !!! THIẾT LẬP TRẠNG THÁI ĐÈN CHO MỖI GIÂY TRONG CHU KỲ (CHỈ XANH VÀ ĐỎ) !!!
# ==============================================================================
def build_states(current_axis, phase_timer, phase_elapsed, t1_boundary, t2_boundary):
    """
    Xác định trạng thái đi thẳng và rẽ trái cho cả 4 làn tại giây thứ phase_elapsed.
    """
    remaining = max(0.1, phase_timer - phase_elapsed)
    remaining_int = max(1, int(remaining))
    
    states = {}
    
    # Mặc định tất cả các làn là đỏ (kèm buffer +5s an toàn để fail-safe của simulator không bị trigger)
    for lane_id in [LANE_N, LANE_E, LANE_S, LANE_W]:
        states[lane_id] = {
            "straight": {"state": "red", "duration": remaining_int + 5},
            "left":     {"state": "red", "duration": remaining_int + 5}
        }
        
    # Xác định làn bên này (D1) và làn đối diện (D2) của trục đang xanh
    if current_axis == 'NS':
        lane_d1 = LANE_N
        lane_d2 = LANE_S
    else:
        lane_d1 = LANE_E
        lane_d2 = LANE_W
        
    t = phase_elapsed
    t1 = t1_boundary
    t2 = t2_boundary
    T = phase_timer
    
    # --------------------------------------------------------------------------
    # GIAI ĐOẠN 1 (0 <= t < t1): D1 xanh (thẳng + rẽ trái). D2 đỏ.
    # GIAI ĐOẠN 2 (t1 <= t < t2): D1 đi thẳng xanh, D2 đi thẳng xanh. Rẽ trái đỏ.
    # GIAI ĐOẠN 3 (t2 <= t < T): D2 xanh (thẳng + rẽ trái). D1 đỏ.
    # --------------------------------------------------------------------------
    
    # 1. Hướng đi thẳng của D1 (xanh từ đầu đến hết Stage 2)
    if t < t2:
        s1_state = 'green'
        s1_dur = max(1, int(t2 - t))
    else:
        s1_state = 'red'
        s1_dur = max(1, int(T - t))
        
    # 2. Hướng rẽ trái của D1 (chỉ xanh ở Stage 1)
    if t < t1:
        l1_state = 'green'
        l1_dur = max(1, int(t1 - t))
    else:
        l1_state = 'red'
        l1_dur = max(1, int(T - t))
        
    # 3. Hướng đi thẳng của D2 (đỏ ở Stage 1, xanh từ Stage 2 đến hết)
    if t < t1:
        s2_state = 'red'
        s2_dur = max(1, int(t1 - t))
    else:
        s2_state = 'green'
        s2_dur = max(1, int(T - t))
        
    # 4. Hướng rẽ trái của D2 (đỏ ở Stage 1 và 2, chỉ xanh ở Stage 3)
    if t < t2:
        l2_state = 'red'
        l2_dur = max(1, int(t2 - t))
    else:
        l2_state = 'green'
        l2_dur = max(1, int(T - t))
        
    # Cập nhật trạng thái đèn trục xanh kèm đệm an toàn +5 giây
    states[lane_d1]["straight"] = {"state": s1_state, "duration": s1_dur + 5}
    states[lane_d1]["left"] = {"state": l1_state, "duration": l1_dur + 5}
    states[lane_d2]["straight"] = {"state": s2_state, "duration": s2_dur + 5}
    states[lane_d2]["left"] = {"state": l2_state, "duration": l2_dur + 5}
    
    return states

# ==============================================================================
# !!! HÀM GỬI LỆNH MQTT LÊN SIMULATOR !!!
# ==============================================================================
def publish_states(current_axis, phase_timer, phase_elapsed, t1_boundary, t2_boundary):
    """Đóng gói trạng thái đèn và gửi lên Broker MQTT"""
    states = build_states(current_axis, phase_timer, phase_elapsed, t1_boundary, t2_boundary)
    lanes_payload = []
    
    for lane_id, cmds in states.items():
        lanes_payload.append({
            "lane_id": lane_id,
            "straight": cmds["straight"],
            "left": cmds["left"]
        })
        
    payload = {
        "timestamp": int(time.time()),
        "command_id": f"cmd-{str(uuid.uuid4())[:8]}",
        "intersections": [
            {
                "intersection_id": 3,
                "lanes": lanes_payload
            }
        ]
    }
    
    try:
        client.publish(TOPIC_LIGHTS, json.dumps(payload))
    except Exception as e:
        print(f"[MQTT ERROR] Lỗi không thể publish: {e}")

# ==============================================================================
# !!! CÁC CALLBACK ĐĂNG KÝ MẠNG MQTT !!!
# ==============================================================================
def on_connect(mqtt_client, userdata, flags, rc):
    print(f"[MQTT] Đã kết nối thành công tới Broker: {BROKER_IP}. Mã kết quả: {rc}")
    mqtt_client.subscribe(TOPIC_TELEMETRY, qos=0)

def on_message(mqtt_client, userdata, msg):
    global last_sim_time, last_telemetry_time, phase_elapsed
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        
        # Đồng bộ thời gian mô phỏng qua timestamp trong telemetry
        sim_ts = payload.get("timestamp")
        if sim_ts is not None:
            last_telemetry_time = time.time()
            if last_sim_time is not None:
                sim_dt = sim_ts - last_sim_time
                if sim_dt > 0:
                    phase_elapsed += sim_dt
            last_sim_time = sim_ts
            
        data = payload.get("data", [])
        # Chỉ cập nhật số xe thực tế của các làn thuộc ngã tư số 3 vào bộ nhớ đệm
        for item in data:
            lane = int(item["lane"])
            if lane in ACTIVE_LANES:
                latest_counts[lane] = {
                    "cars": item.get("cars", 0),
                    "bikes": item.get("bikes", 0)
                }
    except Exception as e:
        pass

# ==============================================================================
# !!! HÀM KHỞI CHẠY CHÍNH TUYẾN TÍNH (MAIN LOOP) !!!
# ==============================================================================
def main():
    global client
    
    print("======================================================================")
    print(" TRAFFIC SERVER - BRUTE FORCE - CHỈ ĐIỀU PHỐI NGÃ TƯ SỐ 3               ")
    print("======================================================================")
    
    # Hỗ trợ tương thích ngược paho-mqtt v2.x và v1.x
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:
        client = mqtt.Client()
        
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(BROKER_IP, BROKER_PORT, 60)
    except Exception as e:
        print(f"[ERROR] Thất bại khi kết nối tới Broker {BROKER_IP}:{BROKER_PORT} - {e}")
        return
        
    # Chạy MQTT Loop ngầm
    client.loop_start()
    
    # Khởi tạo trạng thái đèn bắt đầu với trục NS
    current_axis = 'NS'
    global phase_elapsed, last_sim_time
    
    t1_boundary, t2_boundary, phase_timer = calculate_new_phase(current_axis)
    phase_elapsed = 0.0
    
    last_loop_time = time.time()
    
    print("[INFO] Đang chạy vòng lặp chính của hệ thống...")
    try:
        while True:
            # 1. Gửi lệnh đèn hiện tại lên simulator
            publish_states(current_axis, phase_timer, phase_elapsed, t1_boundary, t2_boundary)
            
            # In ra console để theo dõi thời gian thực
            print(f"[STATUS] Trục {current_axis} Xanh | Giây thứ: {phase_elapsed:.1f} / {phase_timer:.1f}s", end='\r')
            
            # 2. Đợi đúng 1.0 giây
            time.sleep(1.0)
            
            now = time.time()
            dt = now - last_loop_time
            last_loop_time = now
            
            # Nếu quá 2.0s không có telemetry mới (mất kết nối hoặc dừng), tự động dùng wall-clock
            if now - last_telemetry_time > 2.0:
                phase_elapsed += dt
                last_sim_time = None  # Reset đồng bộ khi có kết nối lại
            
            # 3. Hết chu kỳ -> Đổi trục và tính toán lại
            if phase_elapsed >= phase_timer - 0.05:
                print()  # Xuống dòng log status cũ
                current_axis = 'WE' if current_axis == 'NS' else 'NS'
                t1_boundary, t2_boundary, phase_timer = calculate_new_phase(current_axis)
                phase_elapsed = 0.0
                
    except KeyboardInterrupt:
        print("\n[INFO] Đang dừng hệ thống và ngắt kết nối MQTT...")
        client.loop_stop()
        client.disconnect()
        print("[INFO] Đã đóng hệ thống an toàn.")

if __name__ == "__main__":
    main()
