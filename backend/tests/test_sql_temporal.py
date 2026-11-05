"""Fixed-width ODBC temporal decoding and safe Spark synchronization types."""

import struct

import pyodbc
import pyarrow as pa
import pytest

from openbricks.connectors import (
    SQLServerConnection,
    SQL_SS_TIME2,
    SQL_SS_TIMESTAMPOFFSET,
    decode_sqlserver_datetimeoffset,
    decode_sqlserver_time2,
)
from openbricks.synchronization import arrow_type, validate_checkpoint


def test_time2_layout_padding_and_all_seven_fractional_digits():
    # Literal little-endian bytes catch native-long/alignment/endian assumptions.
    value = bytes.fromhex("0c00220038000000bccc5b07")
    assert len(value) == 12
    assert decode_sqlserver_time2(value) == "12:34:56.1234567"
    assert decode_sqlserver_time2(memoryview(value)) == "12:34:56.1234567"
    assert decode_sqlserver_time2(struct.pack("<3H2xI", 0, 0, 0, 0)) == "00:00:00"
    assert (
        decode_sqlserver_time2(struct.pack("<3H2xI", 23, 59, 59, 999999900))
        == "23:59:59.9999999"
    )
    assert decode_sqlserver_time2(None) is None


@pytest.mark.parametrize(
    "zone_hour,zone_minute,offset",
    [
        (5, 30, "+05:30"),
        (-4, 0, "-04:00"),
        (0, -30, "-00:30"),
        (14, 0, "+14:00"),
        (0, 0, "+00:00"),
    ],
)
def test_datetimeoffset_exact_precision_calendar_and_signed_offset(
    zone_hour, zone_minute, offset
):
    value = struct.pack(
        "<6hI2h", 2024, 2, 29, 12, 34, 56, 123456700, zone_hour, zone_minute
    )
    assert len(value) == 20
    assert (
        decode_sqlserver_datetimeoffset(value) == "2024-02-29T12:34:56.1234567" + offset
    )
    assert decode_sqlserver_datetimeoffset(None) is None


@pytest.mark.parametrize("value", [b"", b"x" * 11, b"x" * 13, "not bytes", 12])
def test_time2_rejects_invalid_structures(value):
    with pytest.raises(ValueError):
        decode_sqlserver_time2(value)


@pytest.mark.parametrize(
    "fields",
    [
        (24, 0, 0, 0),
        (0, 60, 0, 0),
        (0, 0, 60, 0),
        (0, 0, 0, 1),
        (0, 0, 0, 1_000_000_000),
    ],
)
def test_time2_rejects_invalid_clock_or_precision(fields):
    with pytest.raises(ValueError):
        decode_sqlserver_time2(struct.pack("<3H2xI", *fields))


@pytest.mark.parametrize(
    "fields",
    [
        (2023, 2, 29, 0, 0, 0, 0, 0, 0),
        (2024, 2, 29, 0, 0, 0, 0, 14, 1),
        (2024, 2, 29, 0, 0, 0, 0, -4, 30),
        (2024, 2, 29, 0, 0, 0, 0, 1, -30),
        (2024, 2, 29, 0, 0, 0, 0, 0, 60),
        (2024, 2, 29, 0, 0, 0, 123456789, 0, 0),
    ],
)
def test_datetimeoffset_rejects_invalid_calendar_timezone_or_precision(fields):
    with pytest.raises(ValueError):
        decode_sqlserver_datetimeoffset(struct.pack("<6hI2h", *fields))


def test_datetimeoffset_rejects_wrong_structure_size_and_type():
    for value in (b"", b"x" * 19, b"x" * 21, "not bytes"):
        with pytest.raises(ValueError):
            decode_sqlserver_datetimeoffset(value)


def test_odbc_temporal_registration_and_connection_cleanup(monkeypatch):
    class Connection:
        def __init__(self):
            self.converters = {}
            self.closed = False

        def add_output_converter(self, kind, function):
            self.converters[kind] = function

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(pyodbc, "connect", lambda *args, **kwargs: connection)
    config = {"server": "fixture", "database": "fixture", "username": "fixture"}
    assert SQLServerConnection(config, {"password": "fixture"}).connect() is connection
    assert connection.converters == {
        SQL_SS_TIME2: decode_sqlserver_time2,
        SQL_SS_TIMESTAMPOFFSET: decode_sqlserver_datetimeoffset,
    }

    def fail_registration(*args):
        raise RuntimeError("registration failed")

    connection.add_output_converter = fail_registration
    with pytest.raises(RuntimeError, match="registration failed"):
        SQLServerConnection(config, {"password": "fixture"}).connect()
    assert connection.closed


def test_change_tracking_time_uses_spark_supported_exact_text():
    assert arrow_type({"type": "time", "scale": 7}) == pa.string()


@pytest.mark.parametrize("kind", ["datetime2", "datetimeoffset"])
def test_submicrosecond_timestamp_primary_keys_rejected_before_initial_write(kind):
    info = {"keys": ["id"], "columns": [{"name": "id", "type": kind, "scale": 7}]}
    with pytest.raises(ValueError, match="primary keys must have precision at most 6"):
        validate_checkpoint(info, None, ["id"])
    info["columns"][0]["scale"] = 6
    validate_checkpoint(info, None, ["id"])
    info["columns"].append({"name": "non_key", "type": kind, "scale": 7})
    validate_checkpoint(info, None, ["id"])
