from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class AdminLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    device_label: str | None = Field(default=None, max_length=200)


class RiderLogin(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(min_length=1, max_length=200)
    device_label: str | None = Field(default=None, max_length=200)


class CustomerRegister(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=20)


class CustomerLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    device_label: str | None = Field(default=None, max_length=200)


class GoogleLogin(BaseModel):
    id_token: str = Field(min_length=10, max_length=4000)
    device_label: str | None = Field(default=None, max_length=200)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=400)


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    session_id: str
    phone_required: bool | None = None
    must_change_password: bool | None = None


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    marketing_opt_in: bool | None = None


class PhoneUpdate(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
