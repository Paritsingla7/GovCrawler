from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None  # optional: launcher sends it explicitly; browser uses the cookie


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None = None
    is_admin: bool
    role: str | None = None
    permissions: list[str]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut
