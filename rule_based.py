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
# !!! BỐ CỤC LÀN ĐƯỜNG CỦA 4 NGÃ TƯ !!!
# ==============================================================================
INTERSECTIONS = {
    0: {'N': 10, 'E': 3, 'S': 9,  'W': 0},
    1: {'N': 14, 'E': 2, 'S': 13, 'W': 1},
    2: {'N': 11, 'E': 7, 'S': 8,  'W': 4},
    3: {'N': 15, 'E': 6, 'S': 12, 'W': 5}
}

# ==============================================================================
# !!! THAM SỐ THUẬT TOÁN !!!
# ==============================================================================
TOTAL_CYCLE_GREEN = 40.0
GREEN_MIN = 12.0
GREEN_MAX = 30.0
LEFT_RATIO = 0.60

CAR_WEIGHT = 1.0
BIKE_WEIGHT = 0.75

# ==============================================================================
# !!! BỘ NHỚ LƯU TRỮ XE THỜI GIAN THỰC VÀ TRẠNG THÁI NGÃ TƯ !!!
# ==============================================================================
latest_counts = {lane_id: {"cars": 0, "bikes": 0} for lane_id in range(16)}

intersection_states = {
    iid: {
        'current_axis': 'NS' if iid in [0, 3] else 'WE',
        'phase_elapsed': 0.0,
        'phase_timer': 0.0,
        't1_boundary': 0.0,
        't2_boundary': 0.0,
        'lane_d1': None,
        'lane_d2': None
    }
    for iid in INTERSECTIONS.keys()
}

client = None
last_sim_time = None
last_telemetry_time = time.time()

# ==============================================================================
# !!! HÀM GHI & ĐỌC FILE JSON PENDING VOLUMES !!!
# ==============================================================================
def save_and_load_volumes(intersection_id, axis):
    """
    Cập nhật lượng xe của trục chuẩn bị xanh vào file pending_volumes.json,
    sau đó đọc ngược lại để tính toán. Hỗ trợ cho 4 ngã tư.
    """
    lanes = INTERSECTIONS[intersection_id]
    
    if axis == 'NS':
        lane1_id, lane2_id = lanes['N'], lanes['S']
    else:
        lane1_id, lane2_id = lanes['E'], lanes['W']
        
    lane1_raw = latest_counts[lane1_id].copy()
    lane2_raw = latest_counts[lane2_id].copy()
    
    # Mặc định an toàn là 2 xe máy, 1 ô tô
    if lane1_raw["cars"] == 0 and lane1_raw["bikes"] == 0:
        lane1_raw = {"cars": 1, "bikes": 2}
    if lane2_raw["cars"] == 0 and lane2_raw["bikes"] == 0:
        lane2_raw = {"cars": 1, "bikes": 2}
        
    axis_data = {
        "lane1": {"id": lane1_id, "cars": lane1_raw["cars"], "bikes": lane1_raw["bikes"]},
        "lane2": {"id": lane2_id, "cars": lane2_raw["cars"], "bikes": lane2_raw["bikes"]}
    }
    
    payload = {}
    try:
        with open("pending_volumes.json", "r") as f:
            payload = json.load(f)
    except Exception:
        pass
        
    str_iid = str(intersection_id)
    if str_iid not in payload:
        payload[str_iid] = {}
        
    payload[str_iid][axis] = axis_data
    
    try:
        with open("pending_volumes.json", "w") as f:
            json.dump(payload, f, indent=4)
    except Exception as e:
        print(f"[JSON ERROR] Ghi file JSON lỗi: {e}")
        
    return payload[str_iid][axis]

# ==============================================================================
# !!! THUẬT TOÁN TÍNH TOÁN PHA THÍCH ỨNG !!!
# ==============================================================================
def calculate_new_phase(intersection_id, current_axis):
    """
    Đọc từ file JSON vừa ghi để tính toán tổng thời gian xanh T
    và ranh giới thời gian cho 3 Stage hoạt động của trục xanh.
    """
    loaded = save_and_load_volumes(intersection_id, current_axis)
    
    lane1_id = loaded["lane1"]["id"]
    lane2_id = loaded["lane2"]["id"]
    
    V1 = loaded["lane1"]["cars"] * CAR_WEIGHT + loaded["lane1"]["bikes"] * BIKE_WEIGHT
    V2 = loaded["lane2"]["cars"] * CAR_WEIGHT + loaded["lane2"]["bikes"] * BIKE_WEIGHT
    V_active = V1 + V2
    
    # Làn nào đông xe hơn sẽ chạy trước (lane_d1)
    if V1 >= V2:
        lane_d1 = lane1_id
        lane_d2 = lane2_id
        V_d1 = V1
        V_d2 = V2
    else:
        lane_d1 = lane2_id
        lane_d2 = lane1_id
        V_d1 = V2
        V_d2 = V1
        
    other_axis = 'WE' if current_axis == 'NS' else 'NS'
    lanes = INTERSECTIONS[intersection_id]
    
    if other_axis == 'NS':
        lane_other1_id, lane_other2_id = lanes['N'], lanes['S']
    else:
        lane_other1_id, lane_other2_id = lanes['E'], lanes['W']
        
    l1 = latest_counts[lane_other1_id]
    l2 = latest_counts[lane_other2_id]
    
    c1, b1 = (1, 2) if l1["cars"] == 0 and l1["bikes"] == 0 else (l1["cars"], l1["bikes"])
    c2, b2 = (1, 2) if l2["cars"] == 0 and l2["bikes"] == 0 else (l2["cars"], l2["bikes"])
    
    V_inactive = (c1 * CAR_WEIGHT + b1 * BIKE_WEIGHT) + (c2 * CAR_WEIGHT + b2 * BIKE_WEIGHT)
    
    if V_active + V_inactive == 0:
        T = TOTAL_CYCLE_GREEN / 2.0
    else:
        T = (V_active / (V_active + V_inactive)) * TOTAL_CYCLE_GREEN
        
    T = max(GREEN_MIN, min(GREEN_MAX, T))
    
    left_time = T * LEFT_RATIO
    MIN_STAGE = 4.0
    remaining_pool = max(0.0, T - 3.0 * MIN_STAGE)
    
    share1 = left_time * (V_d1 / (V_d1 + V_d2)) if (V_d1 + V_d2) > 0 else left_time / 2.0
    share3 = left_time * (V_d2 / (V_d1 + V_d2)) if (V_d1 + V_d2) > 0 else left_time / 2.0
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
        
    t1_boundary = d1_duration
    t2_boundary = d1_duration + d2_duration
    
    print(f"\n[THAY ĐỔI PHA] Ngã tư {intersection_id} | Trục {current_axis} lên XANH")
    print(f" -> Làn đông xe hơn (D1): {lane_d1} (V={V_d1:.1f}) | Làn ít xe hơn (D2): {lane_d2} (V={V_d2:.1f})")
    print(f" -> Lượng xe xanh: {V_active:.1f} | đỏ: {V_inactive:.1f} | T: {T:.1f}s | S1: {d1_duration:.1f}s | S2: {d2_duration:.1f}s | S3: {d3_duration:.1f}s")
    
    return t1_boundary, t2_boundary, T, lane_d1, lane_d2

# ==============================================================================
# !!! THIẾT LẬP TRẠNG THÁI ĐÈN CHO MỖI GIÂY TRONG CHU KỲ !!!
# ==============================================================================
def build_states(intersection_id, current_axis, phase_timer, phase_elapsed, t1_boundary, t2_boundary, lane_d1, lane_d2):
    remaining = max(0.1, phase_timer - phase_elapsed)
    remaining_int = max(1, int(remaining))
    
    states = {}
    lanes = INTERSECTIONS[intersection_id]
    
    for lane_id in lanes.values():
        states[lane_id] = {
            "straight": {"state": "red", "duration": remaining_int + 5},
            "left":     {"state": "red", "duration": remaining_int + 5}
        }
        
    t = phase_elapsed
    t1 = t1_boundary
    t2 = t2_boundary
    T = phase_timer
    
    if t < t2:
        s1_state = 'green'
        s1_dur = max(1, int(t2 - t))
    else:
        s1_state = 'red'
        s1_dur = max(1, int(T - t))
        
    if t < t1:
        l1_state = 'green'
        l1_dur = max(1, int(t1 - t))
    else:
        l1_state = 'red'
        l1_dur = max(1, int(T - t))
        
    if t < t1:
        s2_state = 'red'
        s2_dur = max(1, int(t1 - t))
    else:
        s2_state = 'green'
        s2_dur = max(1, int(T - t))
        
    if t < t2:
        l2_state = 'red'
        l2_dur = max(1, int(t2 - t))
    else:
        l2_state = 'green'
        l2_dur = max(1, int(T - t))
        
    states[lane_d1]["straight"] = {"state": s1_state, "duration": s1_dur + 5}
    states[lane_d1]["left"] = {"state": l1_state, "duration": l1_dur + 5}
    states[lane_d2]["straight"] = {"state": s2_state, "duration": s2_dur + 5}
    states[lane_d2]["left"] = {"state": l2_state, "duration": l2_dur + 5}
    
    return states

# ==============================================================================
# !!! HÀM GỬI LỆNH MQTT LÊN SIMULATOR !!!
# ==============================================================================
def publish_all_states():
    intersections_payload = []
    
    for iid, state in intersection_states.items():
        states = build_states(
            iid, 
            state['current_axis'], 
            state['phase_timer'], 
            state['phase_elapsed'], 
            state['t1_boundary'], 
            state['t2_boundary'],
            state['lane_d1'],
            state['lane_d2']
        )
        
        lanes_payload = []
        for lane_id, cmds in states.items():
            lanes_payload.append({
                "lane_id": lane_id,
                "straight": cmds["straight"],
                "left": cmds["left"]
            })
            
        intersections_payload.append({
            "intersection_id": iid,
            "lanes": lanes_payload
        })
        
    payload = {
        "timestamp": int(time.time()),
        "command_id": f"cmd-{str(uuid.uuid4())[:8]}",
        "intersections": intersections_payload
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
    global last_sim_time, last_telemetry_time
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        
        sim_ts = payload.get("timestamp")
        if sim_ts is not None:
            last_telemetry_time = time.time()
            if last_sim_time is not None:
                sim_dt = sim_ts - last_sim_time
                if sim_dt > 0:
                    for iid in INTERSECTIONS.keys():
                        intersection_states[iid]['phase_elapsed'] += sim_dt
            last_sim_time = sim_ts
            
        data = payload.get("data", [])
        for item in data:
            lane = int(item["lane"])
            if 0 <= lane <= 15:
                latest_counts[lane] = {
                    "cars": item.get("cars", 0),
                    "bikes": item.get("bikes", 0)
                }
    except Exception as e:
        pass

# ==============================================================================
# !!! HÀM KHỞI CHẠY CHÍNH !!!
# ==============================================================================
def main():
    global client, last_sim_time
    
    print("======================================================================")
    print(" TRAFFIC SERVER - RULE BASED - ĐIỀU PHỐI 4 NGÃ TƯ                      ")
    print("======================================================================")
    
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
        
    client.loop_start()
    
    print("[INFO] Khởi tạo pha ban đầu cho 4 ngã tư...")
    for iid in INTERSECTIONS.keys():
        t1, t2, T, lane_d1, lane_d2 = calculate_new_phase(iid, intersection_states[iid]['current_axis'])
        intersection_states[iid]['phase_timer'] = T
        intersection_states[iid]['t1_boundary'] = t1
        intersection_states[iid]['t2_boundary'] = t2
        intersection_states[iid]['lane_d1'] = lane_d1
        intersection_states[iid]['lane_d2'] = lane_d2
        
    last_loop_time = time.time()
    print("\n[INFO] Đang chạy vòng lặp chính của hệ thống...")
    
    try:
        while True:
            publish_all_states()
            
            # In trạng thái ngắn gọn
            status_strs = []
            for iid, state in intersection_states.items():
                status_strs.append(f"I{iid}({state['current_axis']}:{state['phase_elapsed']:.1f}/{state['phase_timer']:.1f}s)")
            print(f"[STATUS] " + " | ".join(status_strs), end='\r')
            
            time.sleep(1.0)
            
            now = time.time()
            dt = now - last_loop_time
            last_loop_time = now
            
            if now - last_telemetry_time > 2.0:
                for iid in INTERSECTIONS.keys():
                    intersection_states[iid]['phase_elapsed'] += dt
                last_sim_time = None
                
            needs_newline = False
            for iid, state in intersection_states.items():
                if state['phase_elapsed'] >= state['phase_timer'] - 0.05:
                    needs_newline = True
                    break
                    
            if needs_newline:
                print() # Xuống dòng cho log tiếp theo
                
            for iid, state in intersection_states.items():
                if state['phase_elapsed'] >= state['phase_timer'] - 0.05:
                    next_axis = 'WE' if state['current_axis'] == 'NS' else 'NS'
                    t1, t2, T, lane_d1, lane_d2 = calculate_new_phase(iid, next_axis)
                    state['current_axis'] = next_axis
                    state['phase_timer'] = T
                    state['t1_boundary'] = t1
                    state['t2_boundary'] = t2
                    state['lane_d1'] = lane_d1
                    state['lane_d2'] = lane_d2
                    state['phase_elapsed'] = 0.0
                    
    except KeyboardInterrupt:
        print("\n[INFO] Đang dừng hệ thống và ngắt kết nối MQTT...")
        client.loop_stop()
        client.disconnect()
        print("[INFO] Đã đóng hệ thống an toàn.")

if __name__ == "__main__":
    main()
