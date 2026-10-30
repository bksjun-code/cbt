from sqlalchemy.orm import Session

from app.models import AppSetting


def get_setting(db: Session, key: str, default=None):
    row = db.get(AppSetting, key)
    return default if row is None else row.value


def set_setting(db: Session, key: str, value) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
