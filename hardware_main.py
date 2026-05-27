from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import math
from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, Column, Float, String, Text, DateTime, BigInteger, Boolean, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
import os

class HardwareSyncRequest(BaseModel):
    monthly_id: int = Field(..., description="월간 측정 ID", example=1)
    opt_accel_x: float = Field(..., description="HW X축 가속도", example=0.05)
    opt_accel_y: float = Field(..., description="HW Y축 가속도", example=0.98)
    opt_accel_z: float = Field(..., description="HW Z축 가속도", example=0.12)

class DailyMeasurementRequest(BaseModel):
    monthly_id: int = Field(..., description="최신 월간 측정 ID", example=1)
    current_accel_x: float = Field(..., description="현재 HW X축 가속도 Raw", example=0.08)
    current_accel_y: float = Field(..., description="현재 HW Y축 가속도 Raw", example=0.95)
    current_accel_z: float = Field(..., description="현재 HW Z축 가속도 Raw", example=0.25)

class DailyReportSaveRequest(BaseModel):
    member_id: int = Field(..., description="유저 고유 식별 ID", example=1)
    angle: float = Field(..., description="측정된 목 각도", example=48.5)
    postureStatus: str = Field(description="자세 상태 (normal, caution, warning)", examples=["caution"])
    notificationTrigger: bool = Field(False, description="알림 발생 여부", example=True)
    duration: int = Field(..., description="해당 자세 유지 시간 (초)", example=120)
    level: str = Field(..., description="측정 난이도 (easy, normal, hard)", example="normal")
    batteryLevel: Optional[int] = Field(None, description="센서 기기 배터리 잔량", example=85)


# 도커 MySQL 연결 설정
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "mysql+pymysql://root:choosungah03!@127.0.0.1:3306/turtlely_db"
)
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

class DailyMeasurement(Base):
    __tablename__ = "daily_measurement"
    
    daily_id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    angle = Column(Float, nullable=False)
    battery_level = Column(Integer, nullable=True)
    duration = Column(Integer, nullable=False)
    level = Column(String(50), nullable=True)
    measured_at = Column(DateTime, nullable=True, default=datetime.now)
    notification_trigger = Column(Boolean, nullable=False, default=False)
    posture_status = Column(String(50), nullable=True)
    member_id = Column(BigInteger, nullable=True)


def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()


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
            
        cos_val = max(-1.0, min(1.0, data.opt_accel_z / vector_magnitude))
        hw_pitch = math.degrees(math.acos(cos_val))

        K_constant = 1.0
        computed_c = measurement.cva_angle - (K_constant * hw_pitch)

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
    - 어려움: 2도 이상 이탈 시
    - 보통: 5도 이상 이탈 시
    - 쉬움: 8도 이상 이탈 시
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

        # 실시간 센서값 기반 현재 목 각도 추정 연산
        vector_magnitude = math.sqrt(data.current_accel_x**2 + data.current_accel_y**2 + data.current_accel_z**2)
        if vector_magnitude == 0: raise HTTPException(status_code=400, detail="실시간 가속도 벡터 크기가 0일 수 없습니다.")
            
        cos_val = max(-1.0, min(1.0, data.current_accel_z / vector_magnitude))
        current_hw_pitch = math.degrees(math.acos(cos_val))

        estimated_cva = round(current_hw_pitch + constant_c, 2)

        # 난이도별 오차 임계치(Threshold) 매핑
        user_level = data.level.lower()
        if user_level == "hard":
            threshold = 2.0
        elif user_level == "easy":
            threshold = 8.0
        else:
            threshold = 5.0

        # 기준 자세 대비 이탈 각도 계산 및 상태 판별
        angle_deviation = base_cva - estimated_cva

        if angle_deviation <= 0:
            posture_status = "normal" 
        elif angle_deviation < threshold:
            posture_status = "caution"
        else:
            posture_status = "warning"

        return {
            "status": "success",
            "estimated_cva": estimated_cva,
            "base_cva_angle": base_cva,
            "angle_deviation": round(angle_deviation, 2),
            "applied_threshold": threshold,
            "posture_result": posture_status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/daily/report", tags=["일일 측정 데이터 저장 API"], summary="일일 리포트를 위해 일일 측정 데이터 저장")
async def save_daily_report(data: DailyReportSaveRequest, db: Session = Depends(get_db)):
    try:
        now_time = datetime.now()
        new_report = DailyMeasurement(
            member_id=data.member_id,
            angle=data.angle,
            posture_status=data.postureStatus.lower(),
            notification_trigger=data.notificationTrigger,
            duration=data.duration,
            level=data.level.lower(),
            battery_level=data.batteryLevel,
            measured_at=now_time,
            created_at=now_time,
            updated_at=now_time
        )
        db.add(new_report)
        db.commit()
        db.refresh(new_report)
        
        return {
            "status": "success",
            "message": "일일 리포트 저장이 완료되었습니다.",
            "daily_id": new_report.daily_id
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
