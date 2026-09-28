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
from collections import deque
import time
import os

# 인메모리 변수
DAILY_MEMORY_CACHE = {}

# 실시간 반응성을 높이기 위해 윈도우 크기를 2로 축소 (노이즈는 방지하면서 즉각 반응)
FILTER_WINDOW = 2

# 임계값 주변에서 상태 튐 방지
HYSTERESIS_DEG = 1.0

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


DATABASE_URL = "mysql+pymysql://root:choosungah03!@127.0.0.1:3306/turtlely_db"
engine = create_engine(DATABASE_URL, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    3축 가속도 기반 피치 계산
    acc_z 성분까지 포함하여 기기 착용 각도가 미세하게 틀어져도 안정적인 Pitch 산출
    """
    vector_magnitude = math.sqrt(acc_x ** 2 + acc_y ** 2 + acc_z ** 2)

    if vector_magnitude == 0:
        raise HTTPException(
            status_code=400,
            detail="가속도 벡터 크기가 0일 수 없습니다."
        )

    # 3축 중력 벡터를 온전히 반영하여 정확도 향상
    return math.degrees(math.atan2(-acc_x, math.sqrt(acc_y ** 2 + acc_z ** 2)))

def get_db():
    db = SessionLocal()
    try: 
        yield db
    finally: 
        db.close()

app = FastAPI(
    title="HW API",
    description="터틀훅 가속도 데이터 기반 실시간 CVA 추정 및 즉각 알림 API",
    version="1.1.0"
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
    summary="비전 월간 측정 기반 CVA 변환 상수 C 도출"
)
async def sync_hardware_constants(data: HardwareSyncRequest, db: Session = Depends(get_db)):
    try:
        measurement = db.query(MonthlyMeasurement).filter(MonthlyMeasurement.monthly_id == data.monthly_id).first()
        if not measurement:
            raise HTTPException(status_code=404, detail="데이터를 찾을 수 없습니다.")

        hw_pitch = calculate_hw_pitch(data.opt_accel_x, data.opt_accel_y, data.opt_accel_z)
        computed_c = measurement.cva_angle + hw_pitch

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
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post(
    "/api/daily",
    tags=["일일 측정 관련 API"],
    summary="실시간 CVA 각도 추정 및 즉각 알림 판별",
    description="고개를 숙이면 3초 대기 없이 즉각 진동 알림을 발생시킵니다."
)
async def track_daily_posture(
    data: DailyMeasurementRequest,
    db: Session = Depends(get_db)
):
    try:
        # 1. 월간 측정 기준값 조회
        measurement = (
            db.query(MonthlyMeasurement)
            .filter(MonthlyMeasurement.monthly_id == data.monthly_id)
            .first()
        )

        if not measurement or measurement.calibrationc is None:
            raise HTTPException(
                status_code=400,
                detail="유저의 보정 상수가 존재하지 않습니다."
            )

        constant_c = measurement.calibrationc
        base_cva = measurement.cva_angle

        # 2. 현재 IMU Pitch 계산
        raw_pitch = calculate_hw_pitch(
            data.current_accel_x,
            data.current_accel_y,
            data.current_accel_z
        )

        # 3. 사용자별 캐시 초기화
        user_id = data.member_id

        if user_id not in DAILY_MEMORY_CACHE:
            DAILY_MEMORY_CACHE[user_id] = {
                "total_duration": 0,
                "cva_sum": 0.0,
                "normal_duration": 0,
                "caution_duration": 0,
                "warning_duration": 0,
                "caution_count": 0,
                "warning_count": 0,
                "pitch_window": deque(maxlen=FILTER_WINDOW),
                "previous_state": "normal",
                "notified_level": "none"  # none / caution / warning
            }

        user_cache = DAILY_MEMORY_CACHE[user_id]

        # 4. 상대 각도 계산 및 빠른 반응 필터(MA2)
        baseline_pitch = constant_c - base_cva
        raw_delta = ((raw_pitch - baseline_pitch + 180) % 360) - 180

        pitch_window = user_cache["pitch_window"]
        pitch_window.append(raw_delta)
        filtered_delta = sum(pitch_window) / len(pitch_window)
        filtered_pitch = baseline_pitch + filtered_delta

        # 5. CVA 추정
        angle_deviation = round(filtered_delta, 2)
        estimated_cva = round(base_cva - angle_deviation, 2)

        # 6. 난이도별 주의 기준
        user_level = data.level.lower() if data.level else "normal"
        if user_level == "hard":
            caution_threshold = 2.0
        elif user_level == "easy":
            caution_threshold = 8.0
        else:
            caution_threshold = 5.0

        warning_threshold = 15.0

        # 7. 히스테리시스를 적용한 자세 판정
        previous_state = user_cache["previous_state"]

        if previous_state == "normal":
            if angle_deviation >= warning_threshold:
                current_state = "warning"
            elif angle_deviation >= caution_threshold:
                current_state = "caution"
            else:
                current_state = "normal"

        elif previous_state == "caution":
            if angle_deviation >= warning_threshold:
                current_state = "warning"
            elif angle_deviation < (caution_threshold - HYSTERESIS_DEG):
                current_state = "normal"
            else:
                current_state = "caution"

        elif previous_state == "warning":
            if angle_deviation >= (warning_threshold - HYSTERESIS_DEG):
                current_state = "warning"
            elif angle_deviation >= caution_threshold:
                current_state = "caution"
            else:
                current_state = "normal"

        user_cache["previous_state"] = current_state

        # 8. 리포트용 누적값 계산
        user_cache["total_duration"] += 1
        user_cache["cva_sum"] = round(user_cache["cva_sum"] + estimated_cva, 2)

        if current_state == "normal":
            user_cache["normal_duration"] += 1
        elif current_state == "caution":
            user_cache["caution_duration"] += 1
        elif current_state == "warning":
            user_cache["warning_duration"] += 1

        # =====================================================
        # 9. 실시간 즉각 진동 알림 로직 (대기 시간 제거)
        # =====================================================
        vibration_type = "none"

        if current_state == "normal":
            # 정상 자세로 돌아오면 알림 플래그 리셋 (다음번에 숙이면 바로 다시 울림)
            user_cache["notified_level"] = "none"

        elif current_state == "warning":
            # 경고 상태에 진입하자마자 즉시 1회 진동
            if user_cache["notified_level"] != "warning":
                vibration_type = "warning"
                user_cache["warning_count"] += 1
                user_cache["notified_level"] = "warning"

        elif current_state == "caution":
            # 주의 상태에 진입하자마자 즉시 1회 진동
            if user_cache["notified_level"] == "none":
                vibration_type = "caution"
                user_cache["caution_count"] += 1
                user_cache["notified_level"] = "caution"

        return {
            "status": "success",
            "base_cva": round(base_cva, 2),
            "constant_c": round(constant_c, 2),
            "raw_hw_pitch": round(raw_pitch, 2),
            "filtered_hw_pitch": round(filtered_pitch, 2),
            "estimated_cva": estimated_cva,
            "angle_deviation": angle_deviation,
            "posture_result": current_state,
            "bad_posture_duration": 0.0,
            "vibration_type": vibration_type,
            "is_vibrating": vibration_type != "none",
            "server_accumulated_data": {
                "total_duration": user_cache["total_duration"],
                "cva_sum": user_cache["cva_sum"],
                "normal_duration": user_cache["normal_duration"],
                "caution_duration": user_cache["caution_duration"],
                "warning_duration": user_cache["warning_duration"],
                "caution_count": user_cache["caution_count"],
                "warning_count": user_cache["warning_count"]
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        print("ERROR:", repr(e))
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post(
    "/api/daily/calibration",
    tags=["일일 측정 관련 API"],
    summary="일일 측정 전 착용 오차 보정용 캘리브레이션"
)
async def process_daily_calibration(data: DailyCalibrationRequest, db: Session = Depends(get_db)):
    try:
        measurement = db.query(MonthlyMeasurement).filter(MonthlyMeasurement.monthly_id == data.monthly_id).first()
        if not measurement:
            raise HTTPException(status_code=404, detail="기준 월간 데이터를 찾을 수 없습니다.")
        
        base_cva = measurement.cva_angle

        current_pitch = calculate_hw_pitch(
            data.current_accel_x,
            data.current_accel_y,
            data.current_accel_z
        )

        daily_constant_c = base_cva + current_pitch

        measurement.calibrationc = round(daily_constant_c, 2)
        db.commit()

        # 캐시 초기화
        member_id = measurement.member_id
        if member_id in DAILY_MEMORY_CACHE:
            del DAILY_MEMORY_CACHE[member_id]

        return {
            "status": "success",
            "monthly_id": data.monthly_id,
            "base_cva_angle": base_cva,
            "current_hardware_pitch": round(current_pitch, 2),
            "daily_derived_constant_c": round(daily_constant_c, 2)
        }

    except HTTPException:
        raise
    except Exception as e:
        print("ERROR:", repr(e))
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

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
                    "caution": report_data["caution_duration"],
                    "warning": report_data["warning_duration"],
                    "avg_angle": avg_angle,
                    "noti_count": report_data["caution_count"] + report_data["warning_count"],
                    "now": now
                }
            )

        db.commit()
        DAILY_MEMORY_CACHE.clear()
        print(f"[{now}] 데일리 리포트 자동 마감 배치 정산 완료!")
        
    except Exception as e:
        db.rollback()
        print(f"자동 마감 배치 에러 발생: {str(e)}")
    finally:
        db.close()

# 스케줄러 등록
scheduler = BackgroundScheduler(timezone="Asia/Seoul")
scheduler.add_job(auto_save_daily_reports, 'cron', hour=23, minute=59, second=0)
scheduler.start()

@app.get("/api/daily/memory-check", tags=["디버깅용 임시 API"])
async def check_current_memory_cache():
    return {
        "status": "success",
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cached_users_count": len(DAILY_MEMORY_CACHE),
        "data": DAILY_MEMORY_CACHE
    }
