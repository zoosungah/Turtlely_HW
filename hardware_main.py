from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import math
from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, Column, Float, String, Text, DateTime, BigInteger, Boolean, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
import time
import os

# 인메모리 변수
DAILY_MEMORY_CACHE = {}

class HardwareSyncRequest(BaseModel):
    monthly_id: int = Field(..., description="월간 측정 ID", example=1)
    opt_accel_x: float = Field(..., description="HW X축 가속도", example=0.05)
    opt_accel_y: float = Field(..., description="HW Y축 가속도", example=0.98)
    opt_accel_z: float = Field(..., description="HW Z축 가속도", example=0.12)

class DailyMeasurementRequest(BaseModel):
    monthly_id: int = Field(..., description="최신 월간 측정 ID", example=1)
    member_id: int = Field(..., description="유저 고유 식별 ID", example=1)
    current_accel_x: float = Field(..., description="현재 HW X축 가속도 Raw", example=0.08)
    current_accel_y: float = Field(..., description="현재 HW Y축 가속도 Raw", example=0.95)
    current_accel_z: float = Field(..., description="현재 HW Z축 가속도 Raw", example=0.25)
    level: str = Field(default="normal", description="유저가 선택한 측정 난이도 (easy, normal, hard)", examples=["normal"])

class DailyCalibrationRequest(BaseModel):
    monthly_id: int = Field(..., description="유저의 기준 CVA를 조회하기 위한 월간 측정 ID", example=1)
    current_accel_x: float = Field(..., description="현재 바른 자세에서의 HW X축 가속도 Raw", example=0.05)
    current_accel_y: float = Field(..., description="현재 바른 자세에서의 HW Y축 가속도 Raw", example=0.98)
    current_accel_z: float = Field(..., description="현재 바른 자세에서의 HW Z축 가속도 Raw", example=0.12)
    
class DailyReportSaveRequest(BaseModel):
    member_id: int = Field(..., description="유저 고유 식별 ID", example=1)
    angle: float = Field(..., description="측정된 목 각도", example=48.5)
    postureStatus: str = Field(description="자세 상태 (normal, caution, warning)", examples=["caution"])
    notificationTrigger: bool = Field(False, description="알림 발생 여부", example=True)
    duration: int = Field(..., description="해당 자세 유지 시간 (초)", example=120)
    level: str = Field(..., description="측정 난이도 (easy, normal, hard)", example="normal")
    batteryLevel: Optional[int] = Field(None, description="센서 기기 배터리 잔량", example=85)


# 도커 MySQL 연결 설정
# DATABASE_URL = os.getenv(
#    "DATABASE_URL", 
#    "mysql+pymysql://0215@127.0.0.1:3306/turtlely_db"
#)

DATABASE_URL = "mysql+pymysql://root:choosungah03!@127.0.0.1:3306/turtlely_db"
engine = create_engine(DATABASE_URL, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 컬럼
class MonthlyMeasurement(Base):
    __tablename__ = "monthly_measurement"
    
    monthly_id = Column(BigInteger, primary_key=True, autoincrement=True)
    cva_angle = Column(Float, nullable=False)
    member_id = Column(BigInteger, nullable=True)
    
    hw_accelx = Column(Float, nullable=True)
    hw_accely = Column(Float, nullable=True)
    hw_accelz = Column(Float, nullable=True)
    calibrationc = Column(Float, nullable=True)

def calculate_hw_pitch(acc_x: float, acc_y: float, acc_z: float) -> float:
    """
    터틀훅 3축 가속도 데이터에서 앞뒤 숙임 각도를 계산한다.

    현재 터틀훅을 왼쪽 귀에 정상 방향으로 착용했을 때:
    - 정면: 상대적으로 작은 각도
    - 목을 앞으로 숙임: 각도 증가
    """

    vector_magnitude = math.sqrt(
        acc_x ** 2
        + acc_y ** 2
        + acc_z ** 2
    )

    if vector_magnitude == 0:
        raise HTTPException(
            status_code=400,
            detail="가속도 벡터 크기가 0일 수 없습니다."
        )

    # 실제 측정 결과에서 앞뒤 숙임을 가장 잘 반영한 공식
    return math.degrees(
        math.atan2(-acc_x, acc_y)
    )

def get_db():
    db = SessionLocal()
    try: 
        yield db
    finally: 
        db.close()


app = FastAPI(
    title="HW API 명",
    description="터틀훅에서 받는 데이터와 관련된 FastAPI",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post(
    "/api/monthly/hardware",
    tags=["월간 측정 관련 API"],
    summary="비전의 월간 측정 데이터를 기반으로 HW에서 사용할 CVA 변환 공식의 보정 상수 C 도출",
    description="""
    ### [동작 흐름]
    1. 플러터가 비전 서버로부터 응답받은 'monthly_id'를 본 API로 전달
    2. 해당 타임스탬프 시점의 하드웨어 3축 데이터(X, Y, Z) 수신
    3. CVA 공식 기반으로 오프셋 'C' 도출
    """,
    response_description="성공 시 생성된 오프셋 상수 C 반환"
)
async def sync_hardware_constants(data: HardwareSyncRequest, db: Session = Depends(get_db)):
    try:
        # 비전에서 찾은 최적 프레임이 저장된 monthly_id
        measurement = db.query(MonthlyMeasurement).filter(MonthlyMeasurement.monthly_id == data.monthly_id).first()
        if not measurement:
            raise HTTPException(status_code=404, detail="데이터를 찾을 수 없습니다.")

        # 공식 기반 상수 C 계산
        vector_magnitude = math.sqrt(data.opt_accel_x**2 + data.opt_accel_y**2 + data.opt_accel_z**2)
        if vector_magnitude == 0:
            raise HTTPException(status_code=400, detail="벡터 크기가 0일 수 없습니다.")
         # 월간 측정 시점의 센서 기준 각도
        hw_pitch = calculate_hw_pitch(
            data.opt_accel_x,
            data.opt_accel_y,
            data.opt_accel_z
        )

        # 목을 숙일수록 hw_pitch는 증가하고 CVA는 감소하므로
        # CVA = C - hw_pitch 형태로 사용
        computed_c = measurement.cva_angle + hw_pitch


        # 컬럼값 저장
        measurement.hw_accelx = data.opt_accel_x
        measurement.hw_accely = data.opt_accel_y
        measurement.hw_accelz = data.opt_accel_z
        measurement.calibrationc = round(computed_c, 2)
        
        db.commit()
        return {
            "status": "success", 
            "monthly_id": data.monthly_id,
            "derived_constant_c": round(computed_c, 2)
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post(
    "/api/daily",
    tags=["일일 측정 관련 API"],
    summary="실시간 CVA 각도 추정 및 자세 판별",
    description="""
    ### 임계치 기반으로 경고 알림
    -> 유저의 기준값에서 선택한 난이도 모드의 임계 각도 이상 벗어나면 경고를 판단
    - 정상: 감소 폭 < 8° (진동 없음)
    - 주의: 8° 이상 15° 미만 감소한 상태가 3초 이상 지속 (주의)
    - 경고: 15° 이상 감소한 상태가 3초 이상 지속 (경고)
    + 오늘 총 누적된 caution/warning 횟수 반환
    """,
    response_description="실시간으로 추정된 CVA 각도 및 판별 결과를 반환합니다."
)

async def track_daily_posture(data: DailyMeasurementRequest, db: Session = Depends(get_db)):
    try:
        # 유저의 월간 고유 보정 상수(C) 및 최초 측정 기준 CVA 조회
        measurement = db.query(MonthlyMeasurement).filter(MonthlyMeasurement.monthly_id == data.monthly_id).first()
        if not measurement or measurement.calibrationc is None:
            raise HTTPException(status_code=400, detail="유저의 보정 상수가 존재하지 않습니다.")
        
        constant_c = measurement.calibrationc
        base_cva = measurement.cva_angle  # 유저의 최적 정상 자세일 때의 비전 각도


        # 현재 센서 데이터 기반 앞뒤 숙임 각도
        current_hw_pitch = calculate_hw_pitch(
            data.current_accel_x,
            data.current_accel_y,
            data.current_accel_z
        )

        # 목을 숙일수록 센서각도는 증가하고 CVA는 감소
        estimated_cva = round(
            constant_c - current_hw_pitch,
            2
        )

        angle_deviation = round(base_cva - estimated_cva, 2)

        if angle_deviation < 8.0:
            current_state = "normal"
        elif 8.0 <= angle_deviation < 15.0:
            current_state = "caution"
        else: # angle_deviation >= 15.0
            current_state = "warning"

        # 5. 메모리 캐시 유저 세션 초기화 (지속 시간 트래킹용 변수 추가)
        user_id = data.member_id
        if user_id not in DAILY_MEMORY_CACHE:
            DAILY_MEMORY_CACHE[user_id] = {
                "total_duration": 0,
                "cva_sum": 0.0,
                "normal_duration": 0,
                "caution_count": 0,
                "warning_count": 0,
                # 💡 진동 발생 판별을 위한 continuous duration 카운터 (초 단위)
                "caution_streak": 0,
                "warning_streak": 0
            }

        user_cache = DAILY_MEMORY_CACHE[user_id]
        user_cache["total_duration"] += 1
        user_cache["cva_sum"] = round(user_cache["cva_sum"] + estimated_cva, 2)

        # 6. 지속 시간(3초) 계산 및 진동 명령 판단
        vibration_type = "none"  # 기본: 진동 없음 ("none", "caution", "warning")

        if current_state == "normal":
            user_cache["normal_duration"] += 1
            # 바른 자세로 돌아오면 스트릭(연속 카운터) 즉시 리셋
            user_cache["caution_streak"] = 0
            user_cache["warning_streak"] = 0

        elif current_state == "caution":
            user_cache["caution_streak"] += 1
            user_cache["warning_streak"] = 0  # warning 스트릭 초기화

            # 정확히 3초째가 되는 순간 "딱 1번" 진동 알림 발생 & 알림 횟수 카운트
            if user_cache["caution_streak"] == 3:
                vibration_type = "caution"
                user_cache["caution_count"] += 1  # 💡 실제 진동 알림 발생 횟수만 1 증가
            elif user_cache["caution_streak"] > 3:
                # 3초 이후에도 계속 주의 상태일 때 (필요에 따라 "caution" 유지 혹은 "none")
                vibration_type = "caution"

        elif current_state == "warning":
            user_cache["warning_streak"] += 1
            user_cache["caution_streak"] = 0  # caution 스트릭 초기화

            # 정확히 3초째가 되는 순간 "딱 1번" 진동 알림 발생 & 알림 횟수 카운트
            if user_cache["warning_streak"] == 3:
                vibration_type = "warning"
                user_cache["warning_count"] += 1  # 💡 실제 진동 알림 발생 횟수만 1 증가
            elif user_cache["warning_streak"] > 3:
                vibration_type = "warning"

        print("=" * 50)
        print(f"base_cva         = {base_cva}")
        print(f"estimated_cva    = {estimated_cva}")
        print(f"angle_deviation  = {angle_deviation}")
        print(f"current_state    = {current_state}")
        print(f"caution_streak   = {user_cache['caution_streak']}s")
        print(f"warning_streak   = {user_cache['warning_streak']}s")
        print(f"vibration_type   = {vibration_type}")
        print("=" * 50)

        # 7. 응답 결과 구성 (vibration_type 추가)
        return {
            "status": "success",
            "base_cva": base_cva,
            "constant_c": constant_c,
            "current_hw_pitch": round(current_hw_pitch, 2),
            "angle_deviation": angle_deviation,
            "estimated_cva": estimated_cva,
            "posture_result": current_state,
            
            # 💡 하드웨어/앱에서 수신할 최종 진동 명령
            "vibration_type": vibration_type,  # "none", "caution", "warning"
            "is_vibrating": vibration_type != "none",

            "server_accumulated_data": {
                "total_duration": user_cache["total_duration"],
                "cva_sum": user_cache["cva_sum"],
                "normal_duration": user_cache["normal_duration"],
                "caution_count": user_cache["caution_count"],
                "warning_count": user_cache["warning_count"]
            }
        }
    
    except Exception as e:
        print("ERROR:", repr(e))
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post(
    "/api/daily/calibration",
    tags=["일일 측정 관련 API"],
    summary="일일 측정 시작 전 착용 오차 보정을 위한 데일리 캘리브레이션",
    description="""
    ### [동작 흐름]
    1. 사용자가 당일 기기 착용 후 '바른 자세'에서 0점 조절 요청
    2. 기존 비전 기준 각도(base_cva)와 현재 가속도 기반 Pitch의 차이를 계산하여 새로운 일일 보정 상수 C 도출
    3. 계산된 일일 보정 상수를 반환하여, 이후 실시간 측정(/api/daily) 시 사용할 수 있도록 함
    """,
    response_description="당일 착용 오차가 보정된 새로운 일일 보정 상수 C 반환"
)
async def process_daily_calibration(data: DailyCalibrationRequest, db: Session = Depends(get_db)):
    try:
        # 1. 기존 월간 기준 데이터(비전 CVA) 조회
        measurement = db.query(MonthlyMeasurement).filter(MonthlyMeasurement.monthly_id == data.monthly_id).first()
        if not measurement:
            raise HTTPException(status_code=44, detail="기준 월간 데이터를 찾을 수 없습니다.")
        
        base_cva = measurement.cva_angle # 기준이 될 비전 각도

        # 2. 현재 가속도 Raw 센서값 기반으로 현재 착용 상태의 Pitch 계산
        vector_magnitude = math.sqrt(data.current_accel_x**2 + data.current_accel_y**2 + data.current_accel_z**2)
        if vector_magnitude == 0: 
            raise HTTPException(status_code=400, detail="가속도 벡터 크기가 0일 수 없습니다.")
            
        cos_val = max(-1.0, min(1.0, data.current_accel_z / vector_magnitude))
        # current_pitch = math.degrees(math.acos(cos_val))
        current_pitch = calculate_hw_pitch(
            data.current_accel_x,
            data.current_accel_y,
            data.current_accel_z
        )

        # 3. 새로운 일일 보정 상수 계산 (비전CVA - 현재Pitch)
        daily_constant_c = base_cva + current_pitch

        # 매번 새로 갱신된 일일 보정 상수를 DB에 업데이트해 두고 싶다면 아래 주석을 푸세요.
        measurement.calibrationc = round(daily_constant_c, 2)
        db.commit()

        return {
            "status": "success",
            "monthly_id": data.monthly_id,
            "base_cva_angle": base_cva,
            "current_hardware_pitch": round(current_pitch, 2),
            "daily_derived_constant_c": round(daily_constant_c, 2)
        }
    except Exception as e:
        print("ERROR:", repr(e))
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

def auto_save_daily_reports():
    db: Session = SessionLocal()
    try:
        now = datetime.now()
        report_date = now.date()
        
        for member_id, report_data in list(DAILY_MEMORY_CACHE.items()):
            total_time = report_data["total_duration"]
            if total_time == 0:
                continue
                
            avg_angle = round(report_data["cva_sum"] / total_time, 2)
            total_score = int(round((report_data["normal_duration"] / total_time) * 100))

            db.execute(
                text(
                    "INSERT INTO daily_report (member_id, report_date, total_score, cva_sum, "
                    "total_measurement_duration, normal_duration, caution_duration, warning_duration, "
                    "avg_angle, total_notification_count, created_at, updated_at) "
                    "VALUES (:member_id, :report_date, :total_score, :cva_sum, :total_time, "
                    ":normal, :caution, :warning, :avg_angle, :noti_count, :now, :now)"
                    ),
                {
                    "member_id": member_id, "report_date": report_date, "total_score": total_score,
                    "cva_sum": report_data["cva_sum"], "total_time": total_time,
                    "normal": report_data["normal_duration"],
                    "caution": report_data["caution_count"],
                    "warning": report_data["warning_count"],
                    "avg_angle": avg_angle,
                    "noti_count": report_data["caution_count"] + report_data["warning_count"],
                    "now": now
                    }
                )
            

        db.commit()
        
        # 날짜 정산 완료 후 오늘 자 캐시 초기화
        DAILY_MEMORY_CACHE.clear()
        print(f"[{now}] 데일리 리포트 자동 마감 배치 정산 완료!")
        
    except Exception as e:
        db.rollback()
        print(f"자동 마감 배치 에러 발생: {str(e)}")
    finally:
        db.close()

# 백그라운드 스케줄러 등록 및 가동 시작
scheduler = BackgroundScheduler(timezone="Asia/Seoul")
scheduler.add_job(auto_save_daily_reports, 'cron', hour=23, minute=59, second=0)
scheduler.start()


@app.get("/api/daily/memory-check", tags=["디버깅용 임시 API"])
async def check_current_memory_cache():
    """
    ### 일일 측정 데이터 디버깅용 api...
    밤 23:59 배치 정산이 돌기 전, 현재 파이썬 에 
    실시간으로 모이고 있는 유저별 일일 측정 누적 데이터를 그대로 반환합니다.
    """
    return {
        "status": "success",
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cached_users_count": len(DAILY_MEMORY_CACHE),
        "data": DAILY_MEMORY_CACHE  # 현재 메모리에 모인 날것의 딕셔너리 통째로 출력
    } 

