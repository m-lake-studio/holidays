#!/usr/bin/env python3
"""
data.go.kr 한국천문연구원 특일 정보(getRestDeInfo)를 받아
kr/{year}.json 과 kr/index.json 을 생성/갱신한다.

- 표준 라이브러리만 사용 (Actions에서 pip install 불필요)
- 내용이 실제로 바뀐 경우에만 updatedAt / revision 을 올린다
- API 장애 시 기존 파일을 덮어쓰지 않는다 (fail-safe)

환경변수:
  DATA_GO_KR_SERVICE_KEY : 공공데이터포털 일반 인증키(Decoding 값)
  YEARS                  : (선택) "2026,2027" 형태. 미지정 시 자동 계산
  OUT_DIR                : (선택) 기본 "kr"
"""

import json
import os
import ssl
import sys
import time
import hashlib
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, date

API_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
SCHEMA_VERSION = 1
KST = timezone(timedelta(hours=9))

SERVICE_KEY = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
OUT_DIR = os.environ.get("OUT_DIR", "kr")

# 정상적인 해라면 최소한 이 정도는 나와야 한다. 미달이면 API 이상으로 간주.
MIN_EXPECTED = 15


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def call_api(year: int, retries: int = 4) -> list:
    """해당 연도의 공휴일 item 목록을 반환. 실패 시 예외."""
    params = urllib.parse.urlencode(
        {
            "serviceKey": SERVICE_KEY,  # Decoding 키 + urlencode 조합
            "solYear": str(year),
            "numOfRows": "100",
            "pageNo": "1",
            "_type": "json",
        }
    )
    url = f"{API_URL}?{params}"
    ctx = ssl.create_default_context()

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kr-holidays-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=ctx) as res:
                raw = res.read().decode("utf-8")

            # 인증 실패 등은 XML 에러로 내려오는 경우가 많다
            if not raw.lstrip().startswith("{"):
                raise RuntimeError(f"JSON이 아닌 응답: {raw[:300]}")

            body = json.loads(raw)
            header = body["response"]["header"]
            if header.get("resultCode") not in ("00", "0"):
                raise RuntimeError(f"API 오류 {header.get('resultCode')}: {header.get('resultMsg')}")

            payload = body["response"].get("body") or {}
            total = int(payload.get("totalCount") or 0)
            if total == 0:
                return []

            items = payload.get("items") or {}
            if not items:
                return []
            item = items.get("item")
            if item is None:
                return []
            return item if isinstance(item, list) else [item]

        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"{year}년 조회 실패: {last_err}")


# --------------------------------------------------------------------------
# 정규화
# --------------------------------------------------------------------------
def classify(name: str) -> str:
    if "대체" in name:
        return "substitute"
    if "임시" in name:
        return "temporary"
    return "public"


def normalize(items: list) -> list:
    """중복 제거 + 정렬 + 스키마 고정."""
    bucket = {}
    for it in items:
        locdate = str(it.get("locdate", "")).strip()
        if len(locdate) != 8 or not locdate.isdigit():
            continue
        name = str(it.get("dateName", "")).strip()
        iso = f"{locdate[0:4]}-{locdate[4:6]}-{locdate[6:8]}"
        key = (iso, name)
        if key in bucket:
            continue
        d = date(int(locdate[0:4]), int(locdate[4:6]), int(locdate[6:8]))
        bucket[key] = {
            "date": iso,
            "name": name,
            "type": classify(name),
            "isHoliday": str(it.get("isHoliday", "Y")).upper() == "Y",
            "dayOfWeek": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d.weekday()],
        }
    return sorted(bucket.values(), key=lambda x: (x["date"], x["name"]))


def content_hash(holidays: list) -> str:
    canonical = json.dumps(holidays, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 파일 입출력
# --------------------------------------------------------------------------
def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def target_years() -> list:
    env = os.environ.get("YEARS", "").strip()
    if env:
        return [int(y) for y in env.replace(" ", "").split(",") if y]
    now = datetime.now(KST)
    years = [now.year, now.year + 1]
    # 8월 이후에는 내후년도 미리 시도 (있으면 담고, 없으면 조용히 skip)
    if now.month >= 8:
        years.append(now.year + 2)
    return [y for y in years if y >= 2026]


# --------------------------------------------------------------------------
def main() -> int:
    if not SERVICE_KEY:
        print("::error::DATA_GO_KR_SERVICE_KEY 가 비어 있습니다.", file=sys.stderr)
        return 1

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    changed_years, failed_years = [], []

    for year in target_years():
        path = os.path.join(OUT_DIR, f"{year}.json")
        prev = read_json(path)

        try:
            holidays = normalize(call_api(year))
        except Exception as e:  # noqa: BLE001
            print(f"::warning::{year} 조회 실패 → 기존 파일 유지 ({e})")
            failed_years.append(year)
            continue

        if not holidays:
            print(f"{year}: 아직 데이터 없음 → skip")
            continue

        if len(holidays) < MIN_EXPECTED and prev and len(prev.get("holidays", [])) >= MIN_EXPECTED:
            print(f"::warning::{year} 결과가 비정상적으로 적음({len(holidays)}) → 기존 파일 유지")
            failed_years.append(year)
            continue

        h = content_hash(holidays)
        if prev and prev.get("hash") == h:
            print(f"{year}: 변경 없음 ({len(holidays)}건)")
            continue

        doc = {
            "schemaVersion": SCHEMA_VERSION,
            "country": "KR",
            "year": year,
            "updatedAt": now_iso,
            "revision": int(prev.get("revision", 0)) + 1 if prev else 1,
            "hash": h,
            "count": len(holidays),
            "source": "https://www.data.go.kr/data/15012690/openapi.do",
            "holidays": holidays,
        }
        write_json(path, doc)
        changed_years.append(year)
        print(f"{year}: 갱신됨 (rev {doc['revision']}, {len(holidays)}건)")

    # ---- index.json ----
    index_path = os.path.join(OUT_DIR, "index.json")
    prev_index = read_json(index_path) or {}
    years_map = {}
    for fname in sorted(os.listdir(OUT_DIR)) if os.path.isdir(OUT_DIR) else []:
        if not fname.endswith(".json") or fname == "index.json":
            continue
        doc = read_json(os.path.join(OUT_DIR, fname))
        if not doc:
            continue
        years_map[str(doc["year"])] = {
            "file": fname,
            "updatedAt": doc["updatedAt"],
            "revision": doc["revision"],
            "hash": doc["hash"],
            "count": doc["count"],
        }

    if years_map:
        latest = max(v["updatedAt"] for v in years_map.values())
        new_index = {
            "schemaVersion": SCHEMA_VERSION,
            "country": "KR",
            "updatedAt": latest,
            "years": years_map,
        }
        if prev_index.get("years") != years_map or prev_index.get("updatedAt") != latest:
            write_json(index_path, new_index)
            print("index.json 갱신됨")

    if failed_years and not changed_years:
        print(f"::error::모든 대상 연도 조회 실패: {failed_years}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
