"""
버스 회차 대기 데이터 수집 스크립트 (기본 대상: 화성 H2) (경기도 버스정보 API v2)

사용법
  1) .env.example 을 복사해 .env 를 만들고, 공공데이터포털 '일반 인증키(Decoding)'를 넣기
     (.env 는 .gitignore 에 들어 있어 깃허브에 올라가지 않습니다)
  2) 테스트:    python collect.py --test
  3) 수집 시작: python collect.py
  중지는 Ctrl+C. 다시 실행하면 기존 CSV에 이어서 저장됩니다.

저장 파일 (data 폴더)
  route_info.json  : 노선 정보 (배차 간격, 첫차/막차 등)
  stations.csv     : 경유 정류소 목록 (순서, 회차 여부)
  locations.csv    : 차량 위치 (90초마다)
  arrivals.csv     : 회차지 다음 정류장의 도착 예측 (90초마다, 비교용)
  collect.log      : 실행 기록
"""
import argparse
import csv
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime

try:
    import requests
except ImportError:
    print("requests 패키지가 필요합니다. 터미널에서 실행하세요:  pip install requests")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_service_key():
    """환경변수 BUS_API_KEY 또는 스크립트 옆 .env 파일에서 인증키를 읽는다."""
    key = os.environ.get("BUS_API_KEY", "").strip()
    if key:
        return key
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == "BUS_API_KEY":
                    return value.strip().strip('"').strip("'")
    return ""


# ===================== 설정 =====================
SERVICE_KEY = load_service_key()

ROUTE_NAME = "H2"
ROUTE_ID = ""                    # 비워두면 자동 검색. 테스트 후 확인된 ID를 넣어두면 더 확실함
REGION_KEYWORD = "화성"          # 같은 이름 노선이 여러 개일 때 지역으로 구분
TURN_STATION_KEYWORD = "병점역후문"  # 회차 정류장 이름 (자동 판별이 안 될 때 사용)

INTERVAL_SEC = 90                # 수집 간격(초). 90초면 하루 약 800회로 1,000회 제한 안쪽
QUIET_HOURS = range(1, 5)        # 01:00~04:59 는 쉬기 (운행 없음)
COLLECT_ARRIVAL = True           # 도착 예측도 함께 수집 (별도 API라 호출 제한 따로)

OUT_DIR = os.path.join(BASE_DIR, "data")   # 어디서 실행해도 스크립트 옆 폴더에 저장
# =================================================

BASE = "http://apis.data.go.kr/6410000"
URL_ROUTE_LIST = f"{BASE}/busrouteservice/v2/getBusRouteListv2"
URL_ROUTE_INFO = f"{BASE}/busrouteservice/v2/getBusRouteInfoItemv2"
URL_ROUTE_STATIONS = f"{BASE}/busrouteservice/v2/getBusRouteStationListv2"
URL_LOCATION = f"{BASE}/buslocationservice/v2/getBusLocationListv2"
URL_ARRIVAL = f"{BASE}/busarrivalservice/v2/getBusArrivalItemv2"

LOCATION_FIELDS = ["collected_at", "plateNo", "vehId", "stationSeq", "stationId",
                   "stateCd", "endBus", "remainSeatCnt", "crowded", "lowPlate",
                   "plateType", "routeId", "raw"]
ARRIVAL_FIELDS = ["collected_at", "stationId", "staOrder", "flag",
                  "predictTime1", "locationNo1", "plateNo1",
                  "predictTime2", "locationNo2", "plateNo2", "raw"]

KEY_HINTS = {
    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR": "인증키가 아직 반영되지 않았거나 잘못됐어요. 발급 직후라면 1~2시간 뒤 다시 시도하세요. Decoding 키를 넣었는지도 확인하세요.",
    "SERVICE_ACCESS_DENIED_ERROR": "이 API에 활용신청이 안 돼 있어요. 공공데이터포털에서 신청 상태를 확인하세요.",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR": "오늘 호출 횟수(1,000회)를 넘었어요. 자정 이후 다시 됩니다.",
}


class ApiError(Exception):
    pass


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "collect.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- 응답 파싱 (JSON 우선, XML도 처리) ----------
def _parse_json(data):
    resp = data.get("response", data)
    header = resp.get("msgHeader", {}) or {}
    body = resp.get("msgBody") or {}
    items = []
    if isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list):
                items = v
                break
            if isinstance(v, dict):
                items = [v]
                break
    return header, items


def _parse_xml(text):
    root = ET.fromstring(text)
    # 공공데이터포털 게이트웨이 오류 (키 문제 등)
    auth = root.find(".//returnAuthMsg")
    if auth is not None and auth.text and auth.text != "NORMAL_SERVICE":
        raise ApiError(f"{auth.text} - {KEY_HINTS.get(auth.text, '')}")
    header = {}
    h = root.find(".//msgHeader")
    if h is not None:
        header = {c.tag: c.text for c in h}
    items = []
    b = root.find(".//msgBody")
    if b is not None:
        for child in b:
            if len(child):
                items.append({g.tag: g.text for g in child})
    return header, items


def call(url, params):
    p = {"serviceKey": SERVICE_KEY, "format": "json"}
    p.update(params)
    r = requests.get(url, params=p, timeout=15)
    text = r.text.strip()
    if text.startswith("{"):
        header, items = _parse_json(json.loads(text))
    elif text.startswith("<"):
        header, items = _parse_xml(text)
    else:
        raise ApiError(f"알 수 없는 응답 (HTTP {r.status_code}): {text[:300]}")

    code = str(header.get("resultCode", "0"))
    if code == "4":          # 결과 없음 (운행 차량 없음 등)
        return []
    if code != "0":
        raise ApiError(f"resultCode={code} {header.get('resultMessage', '')}")
    return items


# ---------- CSV ----------
def append_csv(name, rows, fields):
    path = os.path.join(OUT_DIR, name)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def write_csv(name, rows):
    if not rows:
        return
    fields = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(os.path.join(OUT_DIR, name), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ---------- 준비 단계 ----------
def find_route_id():
    items = call(URL_ROUTE_LIST, {"keyword": ROUTE_NAME})
    cands = [i for i in items if str(i.get("routeName", "")).upper() == ROUTE_NAME.upper()]
    if not cands:
        raise ApiError(f"'{ROUTE_NAME}' 노선을 찾지 못했어요. 검색 결과: "
                       + ", ".join(f"{i.get('routeName')}({i.get('regionName')})" for i in items[:10]))
    log("노선 후보: " + ", ".join(
        f"{c.get('routeName')} / {c.get('regionName')} / ID {c.get('routeId')}" for c in cands))
    for c in cands:
        if REGION_KEYWORD in str(c.get("regionName", "")):
            return str(c["routeId"])
    return str(cands[0]["routeId"])


def setup():
    os.makedirs(OUT_DIR, exist_ok=True)
    route_id = ROUTE_ID or find_route_id()
    log(f"사용할 노선ID: {route_id}")

    info = call(URL_ROUTE_INFO, {"routeId": route_id})
    with open(os.path.join(OUT_DIR, "route_info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    if info:
        i = info[0]
        log(f"노선정보: {i.get('routeName')} | 기점 {i.get('startStationName')} → 종점 {i.get('endStationName')} | "
            f"첫차 {i.get('upFirstTime')} 막차 {i.get('upLastTime')}")

    stations = call(URL_ROUTE_STATIONS, {"routeId": route_id})
    write_csv("stations.csv", stations)
    log(f"경유 정류소 {len(stations)}개 저장")

    turn = [s for s in stations if str(s.get("turnYn", "")).upper() == "Y"]
    if not turn:
        turn = [s for s in stations if TURN_STATION_KEYWORD in str(s.get("stationName", ""))]
    arrival_target = None
    if turn:
        t = turn[0]
        log(f"회차 정류장: {t.get('stationName')} (순서 {t.get('stationSeq')}, ID {t.get('stationId')})")
        seq = int(t.get("stationSeq", 0))
        nxt = [s for s in stations if int(s.get("stationSeq", -1)) == seq + 1]
        if nxt:
            arrival_target = nxt[0]
            log(f"도착 예측 수집 정류장(회차 직후): {arrival_target.get('stationName')} "
                f"(순서 {arrival_target.get('stationSeq')})")
    else:
        log("회차 정류장을 자동으로 찾지 못했어요. stations.csv를 확인해 주세요. (위치 수집은 그대로 진행)")
    return route_id, arrival_target


# ---------- 수집 ----------
def poll(route_id, arrival_target):
    ts = now_str()
    n_loc, arr_msg = 0, ""

    items = call(URL_LOCATION, {"routeId": route_id})
    rows = [{**i, "collected_at": ts, "raw": json.dumps(i, ensure_ascii=False)} for i in items]
    append_csv("locations.csv", rows, LOCATION_FIELDS)
    n_loc = len(rows)

    if COLLECT_ARRIVAL and arrival_target:
        params = {"stationId": arrival_target["stationId"], "routeId": route_id,
                  "staOrder": arrival_target["stationSeq"]}
        arr = call(URL_ARRIVAL, params)
        if arr:
            a = arr[0]
            row = {**a, "collected_at": ts, "stationId": arrival_target["stationId"],
                   "staOrder": arrival_target["stationSeq"], "raw": json.dumps(a, ensure_ascii=False)}
            arr_msg = f" | 도착예측 {a.get('predictTime1') or '-'}분 ({a.get('flag') or ''})"
        else:
            row = {"collected_at": ts, "stationId": arrival_target["stationId"],
                   "staOrder": arrival_target["stationSeq"], "flag": "NONE", "raw": ""}
            arr_msg = " | 도착예측 없음"
        append_csv("arrivals.csv", [row], ARRIVAL_FIELDS)

    return n_loc, arr_msg, items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="한 번만 호출해서 작동 확인")
    args = ap.parse_args()

    if not SERVICE_KEY or "여기에" in SERVICE_KEY:
        print("인증키가 없어요. .env.example 을 복사해 .env 파일을 만들고 "
              "BUS_API_KEY= 뒤에 Decoding 인증키를 넣어 주세요.")
        sys.exit(1)

    try:
        route_id, arrival_target = setup()
    except (ApiError, requests.RequestException, ValueError, ET.ParseError) as e:
        log(f"준비 단계 실패: {e}")
        sys.exit(1)

    if args.test:
        try:
            n, msg, items = poll(route_id, arrival_target)
            log(f"테스트 성공: 운행 중 차량 {n}대{msg}")
            for i in items[:5]:
                print("  ", {k: i.get(k) for k in ("plateNo", "stationSeq", "stationId", "stateCd")})
            if n == 0:
                print("  (지금 운행 중인 차량이 없거나 H2 위치가 안 잡히는 상태예요. 운행 시간에 다시 확인하세요.)")
        except (ApiError, requests.RequestException, ValueError, ET.ParseError) as e:
            log(f"테스트 실패: {e}")
        return

    log(f"수집 시작: {INTERVAL_SEC}초 간격, 새벽 {QUIET_HOURS.start:02d}:00~{QUIET_HOURS.stop:02d}:00 휴식")    calls_today, day = 0, datetime.now().date()
    while True:
        start = time.time()
        now = datetime.now()
        if now.date() != day:
            calls_today, day = 0, now.date()

        if now.hour in QUIET_HOURS:
            time.sleep(60)
            continue

        try:
            n, msg, _ = poll(route_id, arrival_target)
            calls_today += 1
            log(f"차량 {n}대 저장{msg} (오늘 {calls_today}회)")
        except ApiError as e:
            log(f"API 오류: {e}")
        except requests.RequestException as e:
            log(f"네트워크 오류, 다음 회차에 재시도: {e}")
        except Exception as e:  # 예상 못 한 오류에도 멈추지 않기
            log(f"기타 오류, 계속 진행: {e!r}")

        time.sleep(max(0, INTERVAL_SEC - (time.time() - start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("사용자가 수집을 중지했어요.")
