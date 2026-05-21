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
# Mỗi hướng là 1 lane_id vật lý trong simulator.
# Mỗi lane có 2 đèn độc lập: straight và left.
INTERSECTIONS = {
    0: {"N": 10, "E": 3, "S": 9,  "W": 0},
    1: {"N": 14, "E": 2, "S": 13, "W": 1},
    2: {"N": 11, "E": 7, "S": 8,  "W": 4},
    3: {"N": 15, "E": 6, "S": 12, "W": 5},
}

# ==============================================================================
# !!! THAM SỐ THUẬT TOÁN THEO THIẾT KẾ MỚI !!!
# ==============================================================================
CAR_WEIGHT = 1.0
BIKE_WEIGHT = 0.75

# Mỗi trục NS/EW được cấp thời gian trong [12, 28] giây.
PHASE_MIN = 12.0
PHASE_MAX = 28.0
TOTAL_PHASE_BUDGET = PHASE_MAX * 2.0  # 56s, dùng để chia tỉ lệ NS/EW.

# Công thức chia 3 stage trong một phase:
# stage 1 = 0.8 * thời gian hướng đi trước
# stage 2 = 0.2 * hướng đi trước + 0.2 * hướng đi sau
# stage 3 = 0.8 * thời gian hướng đi sau
EARLY_RATIO = 0.8
SHARED_RATIO = 0.2

# Nếu hướng sau có trọng số gấp đôi hướng trước thì đảo ưu tiên.
PRIORITY_REVERSE_RATIO = 2.0

# Vàng được tính nằm trong duration của từng stage.
# Nếu simulator không hỗ trợ yellow, đổi USE_YELLOW = False.
USE_YELLOW = True
YELLOW_TIME = 1.0

# Nếu không có telemetry trong một khoảng ngắn, server tự tăng elapsed bằng thời gian thật.
TELEMETRY_TIMEOUT = 2.0

# ==============================================================================
# !!! BỘ NHỚ LƯU TRỮ XE THỜI GIAN THỰC VÀ TRẠNG THÁI NGÃ TƯ !!!
# ==============================================================================
latest_counts = {lane_id: {"cars": 0, "bikes": 0} for lane_id in range(16)}

intersection_states = {
    iid: {
        "current_axis": "NS" if iid in [0, 3] else "EW",
        "phase_elapsed": 0.0,
        "phase_timer": 0.0,
        "stages": [],
        "debug": {},
    }
    for iid in INTERSECTIONS.keys()
}

client = None
last_sim_time = None
last_telemetry_time = time.time()


# ==============================================================================
# !!! HÀM TIỆN ÍCH !!!
# ==============================================================================
def clamp(value, min_value, max_value):
    return max(min_value, min(value, max_value))


def calc_weight(lane_id):
    """Tính trọng số lane: cars * 1.0 + bikes * 0.75."""
    data = latest_counts.get(lane_id, {"cars": 0, "bikes": 0})
    cars = max(0, int(data.get("cars", 0)))
    bikes = max(0, int(data.get("bikes", 0)))
    return cars * CAR_WEIGHT + bikes * BIKE_WEIGHT


def direction_weights(intersection_id):
    """Trả về trọng số 4 hướng N/S/E/W của một ngã tư."""
    lanes = INTERSECTIONS[intersection_id]
    return {
        "N": calc_weight(lanes["N"]),
        "S": calc_weight(lanes["S"]),
        "E": calc_weight(lanes["E"]),
        "W": calc_weight(lanes["W"]),
    }


def allocate_phase_times(W_NS, W_EW):
    """
    Cấp thời gian cho hai trục NS và EW theo tỉ lệ lưu lượng.
    Mỗi trục bị giới hạn trong [PHASE_MIN, PHASE_MAX].
    """
    total = W_NS + W_EW
    if total <= 0:
        return PHASE_MIN, PHASE_MIN

    T_NS = (W_NS / total) * TOTAL_PHASE_BUDGET
    T_EW = (W_EW / total) * TOTAL_PHASE_BUDGET

    T_NS = clamp(T_NS, PHASE_MIN, PHASE_MAX)
    T_EW = clamp(T_EW, PHASE_MIN, PHASE_MAX)
    return T_NS, T_EW


def split_direction_time(W_first, W_second, T_axis):
    """
    Chia tổng thời gian phase T_axis thành x và y theo trọng số 2 hướng đối diện.
    Đảm bảo x + y = T_axis.
    """
    total = W_first + W_second
    if total <= 0:
        return T_axis / 2.0, T_axis / 2.0

    x = (W_first / total) * T_axis
    y = T_axis - x
    return x, y


def make_stage(active_movements, duration, name):
    """
    active_movements là list tuple: [(lane_id, "straight"), (lane_id, "left")].
    Mỗi stage chỉ được phép tối đa 2 đèn xanh/vàng.
    """
    return {
        "name": name,
        "active": active_movements[:2],
        "duration": max(1.0, float(duration)),
    }


# ==============================================================================
# !!! THUẬT TOÁN TẠO PHASE THEO CÔNG THỨC 3 STAGE CỦA BẠN !!!
# ==============================================================================
def build_axis_stages(intersection_id, axis, T_axis, weights):
    """
    Tạo 3 stage cho trục NS hoặc EW.

    NS mặc định:
        Stage 1: N straight + N left  = 0.8x
        Stage 2: N straight + S straight = 0.2x + 0.2y
        Stage 3: S straight + S left = 0.8y

    Nếu W_S > 2 * W_N thì đảo:
        Stage 1: S straight + S left
        Stage 2: N straight + S straight
        Stage 3: N straight + N left

    EW tương tự với E/W.
    """
    lanes = INTERSECTIONS[intersection_id]

    if axis == "NS":
        first_dir, second_dir = "N", "S"
        first_straight = (lanes["N"], "straight")
        first_left = (lanes["N"], "left")
        second_straight = (lanes["S"], "straight")
        second_left = (lanes["S"], "left")
    else:
        first_dir, second_dir = "E", "W"
        first_straight = (lanes["E"], "straight")
        first_left = (lanes["E"], "left")
        second_straight = (lanes["W"], "straight")
        second_left = (lanes["W"], "left")

    W_first = weights[first_dir]
    W_second = weights[second_dir]

    # x là thời gian dành cho hướng mặc định đi trước, y là hướng còn lại.
    x, y = split_direction_time(W_first, W_second, T_axis)

    # Theo yêu cầu: nếu hướng sau gấp 2 lần hướng trước thì ưu tiên hướng sau đi trước.
    reverse = W_second > PRIORITY_REVERSE_RATIO * W_first

    if not reverse:
        stages = [
            make_stage([first_straight, first_left], EARLY_RATIO * x, f"{axis}_1_{first_dir}_straight_left"),
            make_stage([first_straight, second_straight], SHARED_RATIO * x + SHARED_RATIO * y, f"{axis}_2_both_straight"),
            make_stage([second_straight, second_left], EARLY_RATIO * y, f"{axis}_3_{second_dir}_straight_left"),
        ]
        order = f"{first_dir} trước, {second_dir} sau"
    else:
        stages = [
            make_stage([second_straight, second_left], EARLY_RATIO * y, f"{axis}_1_{second_dir}_straight_left"),
            make_stage([first_straight, second_straight], SHARED_RATIO * y + SHARED_RATIO * x, f"{axis}_2_both_straight"),
            make_stage([first_straight, first_left], EARLY_RATIO * x, f"{axis}_3_{first_dir}_straight_left"),
        ]
        order = f"{second_dir} trước, {first_dir} sau vì W_{second_dir} > 2*W_{first_dir}"

    # Chỉnh sai số số thực để tổng duration đúng bằng T_axis.
    diff = T_axis - sum(stage["duration"] for stage in stages)
    stages[-1]["duration"] = max(1.0, stages[-1]["duration"] + diff)

    debug = {
        "axis": axis,
        "first_dir": first_dir,
        "second_dir": second_dir,
        "W_first": W_first,
        "W_second": W_second,
        "x_first_time": x,
        "y_second_time": y,
        "reverse": reverse,
        "order": order,
        "stage_durations": [stage["duration"] for stage in stages],
    }
    return stages, debug


def calculate_new_phase(intersection_id, current_axis):
    """
    Tính phase mới cho một ngã tư:
    1. Tính W_N, W_S, W_E, W_W.
    2. Tính W_NS, W_EW.
    3. Cấp T_NS, T_EW trong [12, 28].
    4. Lấy T của current_axis.
    5. Chia current_axis thành 3 stage theo 0.8x, 0.2x+0.2y, 0.8y.
    """
    weights = direction_weights(intersection_id)
    W_NS = weights["N"] + weights["S"]
    W_EW = weights["E"] + weights["W"]
    T_NS, T_EW = allocate_phase_times(W_NS, W_EW)

    T_axis = T_NS if current_axis == "NS" else T_EW
    stages, debug = build_axis_stages(intersection_id, current_axis, T_axis, weights)

    debug.update({
        "weights": weights,
        "W_NS": W_NS,
        "W_EW": W_EW,
        "T_NS": T_NS,
        "T_EW": T_EW,
        "T_axis": T_axis,
    })

    print(f"\n[THAY ĐỔI PHA] Ngã tư {intersection_id} | Trục {current_axis} lên XANH")
    print(
        f" -> W_N={weights['N']:.1f} | W_S={weights['S']:.1f} | "
        f"W_E={weights['E']:.1f} | W_W={weights['W']:.1f}"
    )
    print(
        f" -> W_NS={W_NS:.1f} | W_EW={W_EW:.1f} | "
        f"T_NS={T_NS:.1f}s | T_EW={T_EW:.1f}s | T_{current_axis}={T_axis:.1f}s"
    )
    print(f" -> Thứ tự ưu tiên: {debug['order']}")
    for idx, stage in enumerate(stages, start=1):
        active_text = ", ".join([f"lane {lane_id} {movement}" for lane_id, movement in stage["active"]])
        print(f"    Stage {idx}: {active_text} | duration={stage['duration']:.1f}s")

    return T_axis, stages, debug


# ==============================================================================
# !!! THIẾT LẬP TRẠNG THÁI ĐÈN CHO MỖI GIÂY TRONG CHU KỲ !!!
# ==============================================================================
def current_stage_at(stages, elapsed):
    """Xác định stage hiện tại dựa trên elapsed trong phase."""
    cursor = 0.0
    for stage in stages:
        start = cursor
        end = cursor + stage["duration"]
        if elapsed < end:
            return stage, start, end
        cursor = end
    return stages[-1], max(0.0, cursor - stages[-1]["duration"]), cursor


def build_states(intersection_id, phase_timer, phase_elapsed, stages):
    """
    Tạo trạng thái đèn hiện tại.
    Tại mọi thời điểm chỉ có tối đa 2 movement active.
    Các movement không active đều đỏ.
    """
    remaining = max(0.1, phase_timer - phase_elapsed)
    remaining_int = max(1, int(remaining))

    states = {}
    lanes = INTERSECTIONS[intersection_id]

    # Mặc định tất cả đèn đỏ.
    for lane_id in lanes.values():
        states[lane_id] = {
            "straight": {"state": "red", "duration": remaining_int},
            "left": {"state": "red", "duration": remaining_int},
        }

    if not stages:
        return states

    stage, stage_start, stage_end = current_stage_at(stages, phase_elapsed)
    remain_stage = max(0.1, stage_end - phase_elapsed)

    # Trong 1 giây cuối stage, chuyển các movement active sang vàng.
    active_state = "green"
    if USE_YELLOW and stage["duration"] > YELLOW_TIME and remain_stage <= YELLOW_TIME:
        active_state = "yellow"

    active_duration = max(1, int(remain_stage))

    for lane_id, movement in stage["active"]:
        if lane_id in states and movement in states[lane_id]:
            states[lane_id][movement] = {
                "state": active_state,
                "duration": active_duration,
            }

    return states


# ==============================================================================
# !!! HÀM GỬI LỆNH MQTT LÊN SIMULATOR !!!
# ==============================================================================
def publish_all_states():
    intersections_payload = []

    for iid, state in intersection_states.items():
        states = build_states(
            iid,
            state["phase_timer"],
            state["phase_elapsed"],
            state["stages"],
        )

        lanes_payload = []
        for lane_id, cmds in states.items():
            lanes_payload.append({
                "lane_id": lane_id,
                "straight": cmds["straight"],
                "left": cmds["left"],
            })

        intersections_payload.append({
            "intersection_id": iid,
            "lanes": lanes_payload,
        })

    payload = {
        "timestamp": int(time.time()),
        "command_id": f"cmd-{str(uuid.uuid4())[:8]}",
        "intersections": intersections_payload,
    }

    try:
        if client is not None:
            client.publish(TOPIC_LIGHTS, json.dumps(payload), qos=0)
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
        payload = json.loads(msg.payload.decode("utf-8"))

        sim_ts = payload.get("timestamp")
        if sim_ts is not None:
            last_telemetry_time = time.time()
            if last_sim_time is not None:
                sim_dt = float(sim_ts) - float(last_sim_time)
                if 0 < sim_dt < 5.0:
                    for iid in INTERSECTIONS.keys():
                        intersection_states[iid]["phase_elapsed"] += sim_dt
            last_sim_time = sim_ts

        data = payload.get("data", [])
        for item in data:
            lane = int(item.get("lane", -1))
            if 0 <= lane <= 15:
                latest_counts[lane] = {
                    "cars": max(0, int(item.get("cars", 0))),
                    "bikes": max(0, int(item.get("bikes", 0))),
                }
    except Exception as e:
        print(f"[MQTT WARNING] Bỏ qua telemetry không hợp lệ: {e}")


# ==============================================================================
# !!! HÀM KHỞI TẠO / CHUYỂN PHA !!!
# ==============================================================================
def reset_phase_for_intersection(iid, axis):
    T, stages, debug = calculate_new_phase(iid, axis)
    state = intersection_states[iid]
    state["current_axis"] = axis
    state["phase_timer"] = T
    state["phase_elapsed"] = 0.0
    state["stages"] = stages
    state["debug"] = debug


def next_axis(axis):
    return "EW" if axis == "NS" else "NS"


# ==============================================================================
# !!! HÀM KHỞI CHẠY CHÍNH !!!
# ==============================================================================
def main():
    global client, last_sim_time

    print("======================================================================")
    print(" TRAFFIC SERVER - ADAPTIVE 3-STAGE - ĐIỀU PHỐI 4 NGÃ TƯ               ")
    print("======================================================================")
    print(" Thuật toán: T_phase [12,28], stage = 0.8x | 0.2x+0.2y | 0.8y")

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
        reset_phase_for_intersection(iid, intersection_states[iid]["current_axis"])

    last_loop_time = time.time()
    print("\n[INFO] Đang chạy vòng lặp chính của hệ thống...")

    try:
        while True:
            publish_all_states()

            status_strs = []
            for iid, state in intersection_states.items():
                stage_name = "none"
                if state["stages"]:
                    stage, _, _ = current_stage_at(state["stages"], state["phase_elapsed"])
                    stage_name = stage["name"]
                status_strs.append(
                    f"I{iid}({state['current_axis']}:{state['phase_elapsed']:.1f}/"
                    f"{state['phase_timer']:.1f}s:{stage_name})"
                )
            print("[STATUS] " + " | ".join(status_strs), end="\r")

            time.sleep(1.0)

            now = time.time()
            dt = now - last_loop_time
            last_loop_time = now

            # Nếu không nhận timestamp từ simulator thì dùng thời gian thật để phase vẫn chạy.
            if now - last_telemetry_time > TELEMETRY_TIMEOUT:
                for iid in INTERSECTIONS.keys():
                    intersection_states[iid]["phase_elapsed"] += dt
                last_sim_time = None

            needs_newline = any(
                state["phase_elapsed"] >= state["phase_timer"] - 0.05
                for state in intersection_states.values()
            )
            if needs_newline:
                print()

            for iid, state in intersection_states.items():
                if state["phase_elapsed"] >= state["phase_timer"] - 0.05:
                    reset_phase_for_intersection(iid, next_axis(state["current_axis"]))

    except KeyboardInterrupt:
        print("\n[INFO] Đang dừng hệ thống và ngắt kết nối MQTT...")
        client.loop_stop()
        client.disconnect()
        print("[INFO] Đã đóng hệ thống an toàn.")


if __name__ == "__main__":
    main()
