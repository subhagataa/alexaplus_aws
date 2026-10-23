"""Real family notifications through Amazon SNS. OFF by default, so nothing is ever sent by accident.

Enable with environment variables:

    CARELOOP_NOTIFY=sns
    CARELOOP_SNS_TARGETS='{"Sarah": "+14155550100", "Dad": "arn:aws:sns:us-east-1:123456789012:careloop-dad"}'
    AWS_REGION=us-east-1
    CARELOOP_SNS_MAX_PER_HOUR=20        # hard cap, protects you from a runaway loop and a surprise bill

A target is either an E.164 phone number (SMS) or an SNS topic ARN (e.g. a topic with that person's email
subscribed, which is the easiest way to get a real notification during development).
Phone numbers live only in this environment variable. They are never written to the database or the logs.

send() never raises: if SNS fails, the in-app notification still exists and the failure is recorded.
"""
import json
import os
import re
import time

MODE = os.environ.get("CARELOOP_NOTIFY", "off").lower()
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
MAX_PER_HOUR = int(os.environ.get("CARELOOP_SNS_MAX_PER_HOUR", "20"))
_PHONE = re.compile(r"\+[1-9]\d{6,14}")
_sent: list[float] = []  # timestamps of real sends in this process (for the hourly cap)


def targets() -> dict:
    try:
        raw = json.loads(os.environ.get("CARELOOP_SNS_TARGETS", "{}"))
        return {str(k): str(v) for k, v in raw.items()}
    except ValueError:
        return {}


def _client():
    import boto3
    from botocore.config import Config
    return boto3.client("sns", region_name=REGION,
                        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))


def send(recipient: str, message: str) -> tuple[str, str]:
    """Returns (channel, delivery). delivery is 'in-app', 'sent', 'skipped:...' or 'failed:<ErrorType>'."""
    if MODE != "sns":
        return "in-app", "in-app"
    target = targets().get(recipient)
    if not target:
        return "in-app", "skipped:no-target"
    now = time.time()
    _sent[:] = [t for t in _sent if now - t < 3600]
    if len(_sent) >= MAX_PER_HOUR:
        return "in-app", "skipped:rate-limit"
    try:
        sns = _client()
        if target.startswith("arn:"):
            sns.publish(TopicArn=target, Subject="CareLoop alert", Message=message[:1000])
            channel = "topic"
        elif _PHONE.fullmatch(target):
            sns.publish(PhoneNumber=target, Message=message[:300], MessageAttributes={
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}})
            channel = "sms"
        else:
            return "in-app", "skipped:bad-target"
        _sent.append(now)
        return channel, "sent"
    except Exception as e:  # noqa: BLE001 - never let a notification failure break a care workflow
        return "in-app", f"failed:{type(e).__name__}"
