from datetime import datetime

from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
    username: str = Field(min_length=4, max_length=50)
    password: str = Field(min_length=8, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=8, max_length=20)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    name: str
    phone: str
    is_admin: bool
    created_at: datetime

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class EventCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    seat_rows: int = Field(ge=1, le=26)
    seat_cols: int = Field(ge=1, le=20)
    open_at: datetime


class EventResponse(BaseModel):
    id: int
    name: str
    seat_rows: int
    seat_cols: int
    open_at: datetime
    is_open: bool
