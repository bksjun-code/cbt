"""메일 발송(SMTP). 비밀번호 등 본문 내용은 로그에 남기지 않는다."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings

log = logging.getLogger("cbt.mail")


class MailError(Exception):
    """메일을 보내지 못했다(설정 누락, 접속·인증 실패 등)."""


def is_configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from and settings.public_base_url)


def base_url(settings: Settings) -> str:
    if not settings.public_base_url:
        raise MailError("PUBLIC_BASE_URL 이 설정되지 않았습니다")
    return settings.public_base_url.rstrip("/")


def send_mail(settings: Settings, to: str, subject: str, body: str) -> None:
    if not is_configured(settings):
        raise MailError("메일 설정(SMTP_HOST, SMTP_FROM, PUBLIC_BASE_URL)이 없습니다")
    password = settings.smtp_password.get_secret_value() if settings.smtp_password else None
    try:
        msg = EmailMessage()  # 헤더에 줄바꿈이 섞이면 ValueError → 헤더 인젝션 불가
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_sec,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_sec)
        with server:
            if settings.smtp_port != 465:
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_user and password:
                server.login(settings.smtp_user, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError, ValueError) as e:
        log.warning("메일 발송 실패 to=%s: %s", to, type(e).__name__)
        raise MailError(f"메일 발송에 실패했습니다({type(e).__name__})") from e
    log.info("메일 발송 to=%s subject=%s", to, subject)
