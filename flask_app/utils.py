from werkzeug.exceptions import BadRequest
from sqlalchemy import func, case, extract
from calendar import monthrange
from datetime import date

import locale
import pandas
import json

from flask_app import db, redis
from flask_app.models import TimeReport

locale.setlocale(locale.LC_ALL, "")
ALLOWED_EXTENSIONS = {"csv"}
DEFAULT_KEY = "default"


def is_file_allowed(filename):
    if "." in filename:
        extension = filename.rsplit(".", 1)[1].lower()
        report_id = int("".join(filter(str.isdigit, filename)))
        report_exists = TimeReport.query.filter_by(report_id=report_id).first()
        if report_exists:
            raise BadRequest(
                "This report has been uploaded, it can not be uploaded twice"
            )
        return extension in ALLOWED_EXTENSIONS

    return False


def cache_content(key, payload):
    timestamp = pandas.Timestamp.now().isoformat()
    redis.set(key, json.dumps({"payload": payload, "timestamp": timestamp}))


def get_payload(employee_id=None, start_date=None, end_date=None):
    """
    If no query params passed:
        1st pass - payload queried from the database and cached
        2nd pass - if payload not expired returned from the cache
    Else queried from the database
    """
    params = employee_id or start_date or end_date
    if not params and (redis.exists(DEFAULT_KEY) and redis.exists("timestamp")):
        # lookup cache only after the first file upload
        cached_content = json.loads(redis.get(DEFAULT_KEY))
        cache_timestamp = pandas.to_datetime(cached_content["timestamp"])
        upload_timestamp = pandas.to_datetime(
            json.loads(redis.get("timestamp"))["timestamp"]
        )

        # return cached content if not expired
        if cache_timestamp > upload_timestamp:
            return cached_content["payload"]

    query = (
        db.session.query(
            TimeReport.employee_id.label("employee_id"),
            TimeReport.job_group.label("job_group"),
            extract("year", TimeReport.date).label("year"),
            extract("month", TimeReport.date).label("month"),
            case([(extract("day", TimeReport.date) < 16, 1)], else_=2).label(
                "pay_period"
            ),
            func.sum(TimeReport.hours_worked).label("sum_hours"),
        )
        .group_by(
            TimeReport.employee_id,
            TimeReport.job_group,
            extract("year", TimeReport.date).label("year"),
            extract("month", TimeReport.date).label("month"),
            case([(extract("day", TimeReport.date) < 16, 1)], else_=2),
        )
        .order_by(
            TimeReport.employee_id,
            extract("year", TimeReport.date).label("year"),
            extract("month", TimeReport.date).label("month"),
            case([(extract("day", TimeReport.date) < 16, 1)], else_=2),
        )
    )
    if employee_id:
        query = query.filter(TimeReport.employee_id == employee_id)
    if start_date:
        query = query.filter(TimeReport.date >= start_date)
    if end_date:
        query = query.filter(end_date >= TimeReport.date)

    payload = {"payrollReport": {"employeeReports": []}}
    for record in query.all():
        if record.sum_hours > 0:
            month, year = int(record.month), int(record.year)
            start_day, end_day = 1, 15
            if record.pay_period == 2:
                start_day = 16
                _, end_day = monthrange(year, month)

            period_start_date = date(year, month, start_day)
            period_end_date = date(year, month, end_day)
            payload["payrollReport"]["employeeReports"].append(
                {
                    "employeeId": str(record.employee_id),
                    "payPeriod": {
                        "startDate": period_start_date.strftime("%Y-%m-%d"),
                        "endDate": period_end_date.strftime("%Y-%m-%d"),
                    },
                    "amountPaid": locale.currency(
                        record.sum_hours * record.job_group.value
                    ),
                }
            )

    if not params:
        # cache the default case only, no query params
        cache_content(DEFAULT_KEY, payload)
    return payload
