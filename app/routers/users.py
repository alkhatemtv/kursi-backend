"""User routes — read/update the current authenticated user's profile."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
def read_me(user: User = Depends(get_current_user)):
    """Returns the authenticated user. The first time a user calls this after
    logging in, the auth dependency auto-creates them in the database."""
    return user


@router.put("/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Let users update their profile. Role can be elevated to `organizer` here
    as a self-service action — in production you'd typically gate this with
    payment / verification / admin approval instead."""
    data = payload.model_dump(exclude_unset=True)
    if "role" in data and data["role"] not in ("customer", "organizer"):
        data.pop("role")
    for field, value in data.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user
