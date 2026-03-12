import json
import os
import random
import secrets
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .auth import create_access_token, decode_access_token, hash_password, verify_password
from .database import Base, SessionLocal, engine, get_db
from .models import Event, User
from .redis_client import client as redis_client
from .schemas import (
    EventCreateRequest,
    EventResponse,
    LoginRequest,
    LoginResponse,
    SignupRequest,
    UserResponse,
)

app = FastAPI(title="Ticketing WAS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SeatChallengeRequest(BaseModel):
    seat_id: str


class SeatReserveRequest(BaseModel):
    challenge_id: str
    answer: str


def _migrate_schema() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"))


def _ensure_admin_account() -> None:
    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin1234")
    admin_name = os.getenv("ADMIN_NAME", "관리자")
    admin_phone = os.getenv("ADMIN_PHONE", "01000000000")

    db = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username == admin_username).first()
        if admin_user:
            if not admin_user.is_admin:
                admin_user.is_admin = True
                db.commit()
            return

        used_phone = db.query(User).filter(User.phone == admin_phone).first()
        if used_phone:
            admin_phone = f"019{int(time.time()) % 100000000:08d}"

        admin_user = User(
            username=admin_username,
            password_hash=hash_password(admin_password),
            name=admin_name,
            phone=admin_phone,
            is_admin=True,
        )
        db.add(admin_user)
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    for _ in range(15):
        try:
            Base.metadata.create_all(bind=engine)
            _migrate_schema()
            _ensure_admin_account()
            redis_client.ping()
            return
        except OperationalError:
            time.sleep(1)
    raise RuntimeError("Database is not ready")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/signup", response_model=UserResponse, status_code=201)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> User:
    existing_username = db.query(User).filter(User.username == payload.username).first()
    if existing_username:
        raise HTTPException(status_code=409, detail="이미 사용 중인 아이디입니다.")

    existing_phone = db.query(User).filter(User.phone == payload.phone).first()
    if existing_phone:
        raise HTTPException(status_code=409, detail="이미 사용 중인 핸드폰 번호입니다.")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        name=payload.name,
        phone=payload.phone,
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")

    token = create_access_token(subject=str(user.id))
    return LoginResponse(access_token=token, user=user)


def _get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="유효하지 않은 인증 형식입니다.")
    return token


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = _get_bearer_token(authorization)
    subject = decode_access_token(token)
    if not subject:
        raise HTTPException(status_code=401, detail="토큰이 유효하지 않거나 만료되었습니다.")

    user = db.query(User).filter(User.id == int(subject)).first()
    if not user:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return user


@app.get("/auth/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> User:
    return user


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_label(row_index: int) -> str:
    return chr(ord("A") + row_index)


def _seat_ids(event: Event) -> list[str]:
    seat_ids: list[str] = []
    for row_idx in range(event.seat_rows):
        row = _row_label(row_idx)
        for col in range(1, event.seat_cols + 1):
            seat_ids.append(f"{row}{col}")
    return seat_ids


def _event_payload(event: Event) -> dict:
    now = datetime.now(timezone.utc)
    open_at = _to_utc(event.open_at)
    return {
        "id": event.id,
        "name": event.name,
        "seat_rows": event.seat_rows,
        "seat_cols": event.seat_cols,
        "open_at": open_at.isoformat(),
        "is_open": now >= open_at,
        "created_by": event.created_by,
    }


def _track_event_user(event_id: int, user_id: int) -> None:
    users_key = f"ticket:event:{event_id}:active_users"
    last_seen_key = f"ticket:event:{event_id}:active_users:last_seen"
    now_iso = datetime.now(timezone.utc).isoformat()
    redis_client.sadd(users_key, str(user_id))
    redis_client.hset(last_seen_key, str(user_id), now_iso)


@app.post("/events", response_model=EventResponse, status_code=201)
def create_event(
    payload: EventCreateRequest,
    user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    event = Event(
        name=payload.name.strip(),
        seat_rows=payload.seat_rows,
        seat_cols=payload.seat_cols,
        open_at=_to_utc(payload.open_at),
        created_by=user.id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return _event_payload(event)


@app.get("/events")
def list_events(db: Session = Depends(get_db)) -> list[dict]:
    events = db.query(Event).order_by(Event.open_at.asc(), Event.id.asc()).all()
    return [_event_payload(event) for event in events]


@app.delete("/events/{event_id}")
def delete_event(
    event_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")

    if not user.is_admin and event.created_by != user.id:
        raise HTTPException(status_code=403, detail="생성자 또는 관리자만 삭제할 수 있습니다.")

    db.delete(event)
    db.commit()

    redis_client.delete(f"ticket:event:{event_id}:seats")
    redis_client.delete(f"ticket:event:{event_id}:active_users")
    redis_client.delete(f"ticket:event:{event_id}:active_users:last_seen")

    return {"ok": True, "message": "이벤트가 삭제되었습니다."}


@app.get("/events/{event_id}/active-users")
def get_event_active_users(
    event_id: int,
    _: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")

    users_key = f"ticket:event:{event_id}:active_users"
    last_seen_key = f"ticket:event:{event_id}:active_users:last_seen"

    user_ids: list[int] = []
    for item in redis_client.smembers(users_key):
        try:
            user_ids.append(int(item))
        except ValueError:
            continue

    last_seen_raw = redis_client.hgetall(last_seen_key)

    if not user_ids:
        return {"event": _event_payload(event), "active_users": []}

    db_users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_map = {user.id: user for user in db_users}

    active_users = []
    for user_id in user_ids:
        user = user_map.get(user_id)
        if not user:
            continue
        active_users.append(
            {
                "id": user.id,
                "username": user.username,
                "name": user.name,
                "phone": user.phone,
                "is_admin": user.is_admin,
                "last_seen": last_seen_raw.get(str(user.id)),
            }
        )

    active_users.sort(key=lambda item: item.get("last_seen") or "", reverse=True)
    return {"event": _event_payload(event), "active_users": active_users}


@app.get("/events/{event_id}/seat-owners")
def get_event_seat_owners(
    event_id: int,
    _: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")

    seat_key = f"ticket:event:{event_id}:seats"
    seat_map = redis_client.hgetall(seat_key)
    if not seat_map:
        return {"event": _event_payload(event), "seat_owners": []}

    user_ids: list[int] = []
    for user_id_text in seat_map.values():
        try:
            user_ids.append(int(user_id_text))
        except ValueError:
            continue
    unique_user_ids = sorted(set(user_ids))

    db_users = db.query(User).filter(User.id.in_(unique_user_ids)).all()
    user_map = {user.id: user for user in db_users}

    seat_owners = []
    for seat_id in sorted(seat_map.keys()):
        raw_user_id = seat_map.get(seat_id)
        if raw_user_id is None:
            continue
        try:
            user_id = int(raw_user_id)
        except ValueError:
            continue

        user = user_map.get(user_id)
        if not user:
            continue

        seat_owners.append(
            {
                "seat_id": seat_id,
                "user_id": user.id,
                "username": user.username,
                "name": user.name,
                "phone": user.phone,
                "is_admin": user.is_admin,
            }
        )

    return {"event": _event_payload(event), "seat_owners": seat_owners}


@app.get("/events/{event_id}/seats")
def get_seats(event_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")

    _track_event_user(event_id, user.id)

    event_data = _event_payload(event)
    seat_ids = _seat_ids(event)

    key = f"ticket:event:{event_id}:seats"
    reserved_map = redis_client.hgetall(key)

    seats = []
    mine = []
    for seat_id in seat_ids:
        owner = reserved_map.get(seat_id)
        if owner is None:
            status = "available"
        elif owner == str(user.id):
            status = "mine"
            mine.append(seat_id)
        else:
            status = "occupied"

        seats.append({"seat_id": seat_id, "status": status})

    return {
        "event": event_data,
        "seats": seats,
        "my_seats": sorted(mine),
    }


@app.post("/events/{event_id}/challenge")
def create_challenge(
    event_id: int,
    payload: SeatChallengeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")

    _track_event_user(event_id, user.id)

    if datetime.now(timezone.utc) < _to_utc(event.open_at):
        raise HTTPException(status_code=400, detail="아직 오픈 전입니다.")

    seat_id = payload.seat_id.upper().strip()
    if seat_id not in _seat_ids(event):
        raise HTTPException(status_code=400, detail="유효하지 않은 좌석입니다.")

    key = f"ticket:event:{event_id}:seats"
    owner = redis_client.hget(key, seat_id)
    if owner and owner != str(user.id):
        raise HTTPException(status_code=409, detail="이미 선택된 좌석입니다.")

    captcha_text = "".join(str(random.randint(0, 9)) for _ in range(5))
    challenge_id = secrets.token_urlsafe(12)
    challenge_key = f"ticket:challenge:{challenge_id}"

    challenge_payload = {
        "user_id": user.id,
        "event_id": event_id,
        "seat_id": seat_id,
        "answer": captcha_text,
    }
    redis_client.setex(challenge_key, 120, json.dumps(challenge_payload))

    return {
        "challenge_id": challenge_id,
        "captcha_text": captcha_text,
        "expires_in": 120,
    }


@app.post("/events/{event_id}/reserve")
def reserve_seat(
    event_id: int,
    payload: SeatReserveRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")

    _track_event_user(event_id, user.id)

    if datetime.now(timezone.utc) < _to_utc(event.open_at):
        raise HTTPException(status_code=400, detail="아직 오픈 전입니다.")

    challenge_key = f"ticket:challenge:{payload.challenge_id}"
    raw = redis_client.getdel(challenge_key)
    if not raw:
        raise HTTPException(status_code=400, detail="보안문자 만료 또는 잘못된 요청입니다.")

    data = json.loads(raw)
    if int(data["user_id"]) != user.id or int(data["event_id"]) != event_id:
        raise HTTPException(status_code=403, detail="본인 요청이 아닙니다.")

    if payload.answer.strip() != str(data["answer"]):
        raise HTTPException(status_code=400, detail="보안문자가 일치하지 않습니다.")

    seat_id = str(data["seat_id"])
    if seat_id not in _seat_ids(event):
        raise HTTPException(status_code=400, detail="유효하지 않은 좌석입니다.")

    key = f"ticket:event:{event_id}:seats"
    inserted = redis_client.hsetnx(key, seat_id, str(user.id))

    if not inserted:
        owner = redis_client.hget(key, seat_id)
        if owner == str(user.id):
            return {"ok": True, "seat_id": seat_id, "message": "이미 내가 선택한 좌석입니다."}
        raise HTTPException(status_code=409, detail="이미 선택된 좌석입니다.")

    user_key = f"ticket:user:{user.id}:reservations"
    redis_client.sadd(user_key, f"{event_id}:{seat_id}")

    return {"ok": True, "seat_id": seat_id, "message": "좌석 선택이 완료되었습니다."}


@app.delete("/events/{event_id}/seats/{seat_id}")
def admin_release_seat(
    event_id: int,
    seat_id: str,
    _: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")

    seat_id = seat_id.upper().strip()
    if seat_id not in _seat_ids(event):
        raise HTTPException(status_code=400, detail="유효하지 않은 좌석입니다.")

    seat_key = f"ticket:event:{event_id}:seats"
    owner = redis_client.hget(seat_key, seat_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="선택된 사용자가 없는 좌석입니다.")

    redis_client.hdel(seat_key, seat_id)
    user_key = f"ticket:user:{owner}:reservations"
    redis_client.srem(user_key, f"{event_id}:{seat_id}")

    return {"ok": True, "seat_id": seat_id, "message": "좌석 선택이 관리자에 의해 해제되었습니다."}
