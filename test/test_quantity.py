import unittest

import pytest

import confuse

from . import _root


class QuantityTimeTest(unittest.TestCase):
    def test_milliseconds_identity(self):
        config = _root({"foo": "500ms"})
        assert config["foo"].get(confuse.Quantity("time", "ms")) == 500

    def test_seconds_to_milliseconds(self):
        config = _root({"foo": "1.5s"})
        assert config["foo"].get(confuse.Quantity("time", "ms")) == 1500

    def test_fractional_seconds_stay_exact(self):
        # 0.1 cannot be represented exactly as a float; the conversion
        # must still yield precisely 100 ms.
        config = _root({"foo": "0.1s"})
        assert config["foo"].get(confuse.Quantity("time", "ms")) == 100

    def test_compound_time_units(self):
        config = _root({"foo": "2m"})
        assert config["foo"].get(confuse.Quantity("time", "s")) == 120
        config = _root({"foo": "1h"})
        assert config["foo"].get(confuse.Quantity("time", "s")) == 3600

    def test_bare_number_uses_target_unit(self):
        config = _root({"foo": 500})
        assert config["foo"].get(confuse.Quantity("time", "ms")) == 500

    def test_bare_string_number_uses_target_unit(self):
        config = _root({"foo": "500"})
        assert config["foo"].get(confuse.Quantity("time", "ms")) == 500

    def test_float_uses_decimal_value(self):
        config = _root({"foo": 0.1})
        template = confuse.Quantity("time", "ms", rounding="nearest")
        assert config["foo"].get(template) == 0


class QuantityDataTest(unittest.TestCase):
    def test_binary_capacity(self):
        config = _root({"foo": "64MiB"})
        assert config["foo"].get(confuse.Quantity("data", "MiB")) == 64

    def test_decimal_capacity(self):
        config = _root({"foo": "2MB"})
        assert config["foo"].get(confuse.Quantity("data", "MB")) == 2

    def test_decimal_and_binary_differ(self):
        # 64 MiB is 67108864 bytes; 64 MB is only 64000000 bytes.
        config = _root({"foo": "64MiB"})
        assert config["foo"].get(confuse.Quantity("data", "B")) == 64 * 1024**2
        config = _root({"foo": "64MB"})
        assert config["foo"].get(confuse.Quantity("data", "B")) == 64 * 1000**2

    def test_binary_to_decimal_conversion(self):
        config = _root({"foo": "1GiB"})
        assert config["foo"].get(confuse.Quantity("data", "B")) == 2**30
        config = _root({"foo": "1GB"})
        assert config["foo"].get(confuse.Quantity("data", "B")) == 10**9

    def test_mebibytes_to_kibibytes(self):
        config = _root({"foo": "2MiB"})
        assert config["foo"].get(confuse.Quantity("data", "KiB")) == 2048


class QuantityExpressionTest(unittest.TestCase):
    def test_division_expression(self):
        config = _root({"foo": "3 / 2 s"})
        assert config["foo"].get(confuse.Quantity("time", "ms")) == 1500

    def test_multiplication_expression(self):
        config = _root({"foo": "2 * 3 MB"})
        assert config["foo"].get(confuse.Quantity("data", "MB")) == 6

    def test_mixed_expression(self):
        config = _root({"foo": "1 GB / 4"})
        assert config["foo"].get(confuse.Quantity("data", "MB")) == 250

    def test_division_by_zero(self):
        config = _root({"foo": "1 GB / 0"})
        with pytest.raises(confuse.ConfigValueError, match="division by zero"):
            config["foo"].get(confuse.Quantity("data", "MB"))

    def test_second_unit_rejected(self):
        config = _root({"foo": "2 s * ms"})
        with pytest.raises(confuse.ConfigValueError, match="at most one unit"):
            config["foo"].get(confuse.Quantity("time", "ms"))

    def test_arbitrary_code_not_evaluated(self):
        config = _root({"foo": "__import__('os').system('id')"})
        with pytest.raises(confuse.ConfigValueError, match="invalid quantity"):
            config["foo"].get(confuse.Quantity("time", "ms"))

    def test_exponent_operator_rejected(self):
        config = _root({"foo": "2 ** 3 s"})
        with pytest.raises(confuse.ConfigValueError):
            config["foo"].get(confuse.Quantity("time", "ms"))

    def test_dangling_operator_rejected(self):
        config = _root({"foo": "2 s *"})
        with pytest.raises(confuse.ConfigValueError, match="dangling operator"):
            config["foo"].get(confuse.Quantity("time", "ms"))


class QuantityDimensionTest(unittest.TestCase):
    def test_time_unit_rejected_as_capacity(self):
        config = _root({"foo": "5s"})
        with pytest.raises(confuse.ConfigValueError, match="'time' dimension"):
            config["foo"].get(confuse.Quantity("data", "MB"))

    def test_capacity_unit_rejected_as_time(self):
        config = _root({"foo": "5MB"})
        with pytest.raises(confuse.ConfigValueError, match="'data' dimension"):
            config["foo"].get(confuse.Quantity("time", "s"))

    def test_unknown_unit(self):
        config = _root({"foo": "3 fortnights"})
        with pytest.raises(confuse.ConfigValueError, match="unknown unit"):
            config["foo"].get(confuse.Quantity("time", "s"))

    def test_unknown_dimension_name(self):
        with pytest.raises(confuse.ConfigTemplateError, match="unknown dimension"):
            confuse.Quantity("velocity", "m/s")

    def test_unknown_target_unit(self):
        with pytest.raises(
            confuse.ConfigTemplateError, match="unknown target unit"
        ):
            confuse.Quantity("time", "fortnights")

    def test_custom_dimension(self):
        length = confuse.Dimension("length", {"mm": 1, "cm": 10, "m": 1000})
        config = _root({"foo": "1.5m"})
        assert config["foo"].get(confuse.Quantity(length, "cm")) == 150

    def test_custom_dimension_rejects_other_units(self):
        length = confuse.Dimension("length", {"mm": 1, "m": 1000})
        config = _root({"foo": "5s"})
        with pytest.raises(confuse.ConfigValueError, match="'time' dimension"):
            config["foo"].get(confuse.Quantity(length, "m"))


class QuantityRoundingTest(unittest.TestCase):
    def test_exact_rejects_non_integral_conversion(self):
        config = _root({"foo": "500ms"})
        with pytest.raises(confuse.ConfigValueError, match="not a whole number"):
            config["foo"].get(confuse.Quantity("time", "s"))

    def test_floor_rounding(self):
        config = _root({"foo": "1500ms"})
        template = confuse.Quantity("time", "s", rounding="floor")
        assert config["foo"].get(template) == 1

    def test_ceil_rounding(self):
        config = _root({"foo": "500ms"})
        template = confuse.Quantity("time", "s", rounding="ceil")
        assert config["foo"].get(template) == 1

    def test_nearest_rounding(self):
        config = _root({"foo": "1400ms"})
        template = confuse.Quantity("time", "s", rounding="nearest")
        assert config["foo"].get(template) == 1
        config = _root({"foo": "1500ms"})
        assert config["foo"].get(template) == 2

    def test_unknown_rounding_rule(self):
        with pytest.raises(confuse.ConfigTemplateError, match="rounding"):
            confuse.Quantity("time", "s", rounding="bankers")


class QuantityNegativeTest(unittest.TestCase):
    def test_negative_number_rejected_by_default(self):
        config = _root({"foo": -5})
        with pytest.raises(confuse.ConfigValueError, match="must not be negative"):
            config["foo"].get(confuse.Quantity("time", "ms"))

    def test_negative_string_rejected_by_default(self):
        config = _root({"foo": "-1.5s"})
        with pytest.raises(confuse.ConfigValueError, match="must not be negative"):
            config["foo"].get(confuse.Quantity("time", "ms"))

    def test_negative_allowed_when_requested(self):
        config = _root({"foo": "-1.5s"})
        template = confuse.Quantity("time", "ms", allow_negative=True)
        assert config["foo"].get(template) == -1500


class QuantityBoundsTest(unittest.TestCase):
    def test_minimum_enforced(self):
        config = _root({"foo": "50ms"})
        template = confuse.Quantity("time", "ms", minimum="100ms")
        with pytest.raises(confuse.ConfigValueError, match="at least"):
            config["foo"].get(template)

    def test_maximum_enforced(self):
        config = _root({"foo": "3s"})
        template = confuse.Quantity("time", "ms", maximum="2s")
        with pytest.raises(confuse.ConfigValueError, match="at most"):
            config["foo"].get(template)

    def test_bounds_are_inclusive(self):
        config = _root({"foo": "2s"})
        template = confuse.Quantity("time", "ms", minimum="1s", maximum="2s")
        assert config["foo"].get(template) == 2000

    def test_bound_with_wrong_dimension_rejected(self):
        with pytest.raises(confuse.ConfigTemplateError, match="invalid minimum"):
            confuse.Quantity("time", "ms", minimum="5MB")

    def test_inverted_bounds_rejected(self):
        with pytest.raises(confuse.ConfigTemplateError, match="exceeds maximum"):
            confuse.Quantity("time", "ms", minimum="2s", maximum="1s")


class QuantityDefaultTest(unittest.TestCase):
    def test_string_default_converted(self):
        config = _root({})
        template = confuse.Quantity("time", "ms", default="1.5s")
        assert config["foo"].get(template) == 1500

    def test_number_default_in_target_unit(self):
        config = _root({})
        template = confuse.Quantity("data", "MiB", default=64)
        assert config["foo"].get(template) == 64

    def test_default_with_wrong_dimension_rejected(self):
        config = _root({})
        template = confuse.Quantity("time", "ms", default="5MB")
        with pytest.raises(confuse.ConfigTemplateError, match="invalid default"):
            config["foo"].get(template)

    def test_default_violating_bounds_rejected(self):
        config = _root({})
        template = confuse.Quantity("time", "ms", default="5s", maximum="2s")
        with pytest.raises(confuse.ConfigTemplateError, match="invalid default"):
            config["foo"].get(template)

    def test_missing_required_value(self):
        config = _root({})
        with pytest.raises(confuse.NotFoundError):
            config["foo"].get(confuse.Quantity("time", "ms"))


class QuantityTypeTest(unittest.TestCase):
    def test_list_rejected_as_type_error(self):
        config = _root({"foo": [1, 2]})
        with pytest.raises(confuse.ConfigTypeError):
            config["foo"].get(confuse.Quantity("time", "ms"))

    def test_bool_rejected_as_type_error(self):
        config = _root({"foo": True})
        with pytest.raises(confuse.ConfigTypeError):
            config["foo"].get(confuse.Quantity("time", "ms"))
