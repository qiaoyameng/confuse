import unittest

import pytest

import confuse

from . import _root


class QuantityTimeTest(unittest.TestCase):
    def test_simple_unit(self):
        config = _root({"timeout": "500ms"})
        assert config["timeout"].get(confuse.Quantity("time", "ms")) == 500

    def test_unit_conversion(self):
        config = _root({"timeout": "1.5s"})
        assert config["timeout"].get(confuse.Quantity("time", "ms")) == 1500

    def test_default_unit_is_base(self):
        config = _root({"timeout": "2min"})
        assert config["timeout"].get(confuse.Quantity("time")) == 120

    def test_bare_number_is_target_unit(self):
        config = _root({"timeout": 500})
        assert config["timeout"].get(confuse.Quantity("time", "ms")) == 500

    def test_compound_multiply(self):
        config = _root({"timeout": "2 * 3s"})
        assert config["timeout"].get(confuse.Quantity("time", "ms")) == 6000

    def test_compound_divide(self):
        config = _root({"timeout": "1h / 4"})
        assert config["timeout"].get(confuse.Quantity("time", "s")) == 900

    def test_compound_with_parentheses(self):
        config = _root({"timeout": "(1h + 0s) / 2"})
        # Addition is not supported; '+' is only a unary sign.
        with pytest.raises(confuse.ConfigValueError):
            config["timeout"].get(confuse.Quantity("time", "s"))

    def test_parenthesized_division(self):
        config = _root({"timeout": "3h / (2 * 3)"})
        assert config["timeout"].get(confuse.Quantity("time", "min")) == 30

    def test_exact_arithmetic_no_float(self):
        # 0.1 + 0.2 style errors must not occur: 3 * 0.1s is exactly 300ms.
        config = _root({"timeout": "3 * 0.1s"})
        assert config["timeout"].get(confuse.Quantity("time", "ms")) == 300

    def test_dimension_mismatch_rejected(self):
        config = _root({"timeout": "64MiB"})
        with pytest.raises(confuse.ConfigValueError, match="unknown unit"):
            config["timeout"].get(confuse.Quantity("time", "ms"))

    def test_squared_dimension_rejected(self):
        config = _root({"timeout": "2s * 3s"})
        with pytest.raises(confuse.ConfigValueError, match="single time quantity"):
            config["timeout"].get(confuse.Quantity("time", "ms"))

    def test_division_by_zero_rejected(self):
        config = _root({"timeout": "5s / 0"})
        with pytest.raises(confuse.ConfigValueError, match="division by zero"):
            config["timeout"].get(confuse.Quantity("time", "ms"))

    def test_arbitrary_code_rejected(self):
        config = _root({"timeout": "__import__('os').system('id')"})
        with pytest.raises(confuse.ConfigValueError):
            config["timeout"].get(confuse.Quantity("time", "ms"))

    def test_bool_rejected(self):
        config = _root({"timeout": True})
        with pytest.raises(confuse.ConfigTypeError):
            config["timeout"].get(confuse.Quantity("time", "ms"))


class QuantityBytesTest(unittest.TestCase):
    def test_decimal_unit(self):
        config = _root({"size": "2MB"})
        assert config["size"].get(confuse.Quantity("bytes", "B")) == 2_000_000

    def test_binary_unit(self):
        config = _root({"size": "2MiB"})
        assert config["size"].get(confuse.Quantity("bytes", "B")) == 2_097_152

    def test_decimal_and_binary_differ(self):
        config = _root({"decimal": "1MB", "binary": "1MiB"})
        decimal = config["decimal"].get(confuse.Quantity("bytes", "B"))
        binary = config["binary"].get(confuse.Quantity("bytes", "B"))
        assert decimal == 1_000_000
        assert binary == 1_048_576
        assert binary - decimal == 48_576

    def test_binary_to_binary_conversion(self):
        config = _root({"size": "1MiB"})
        assert config["size"].get(confuse.Quantity("bytes", "KiB")) == 1024

    def test_seconds_rejected_as_capacity(self):
        config = _root({"size": "2s"})
        with pytest.raises(confuse.ConfigValueError, match="unknown unit"):
            config["size"].get(confuse.Quantity("bytes", "B"))

    def test_compound_capacity(self):
        config = _root({"size": "3 * 64MiB / 2"})
        assert config["size"].get(confuse.Quantity("bytes", "MiB")) == 96


class QuantityRoundingTest(unittest.TestCase):
    def test_inexact_exact_rule_fails(self):
        config = _root({"timeout": "500ms"})
        with pytest.raises(confuse.ConfigValueError, match="not a whole number"):
            config["timeout"].get(confuse.Quantity("time", "s"))

    def test_inexact_floor(self):
        config = _root({"timeout": "1500ms"})
        template = confuse.Quantity("time", "s", rounding="floor")
        assert config["timeout"].get(template) == 1

    def test_inexact_ceil(self):
        config = _root({"timeout": "1500ms"})
        template = confuse.Quantity("time", "s", rounding="ceil")
        assert config["timeout"].get(template) == 2

    def test_inexact_nearest(self):
        config = _root({"timeout": "1499ms"})
        template = confuse.Quantity("time", "s", rounding="nearest")
        assert config["timeout"].get(template) == 1

    def test_inexact_nearest_ties_up(self):
        config = _root({"timeout": "1500ms"})
        template = confuse.Quantity("time", "s", rounding="nearest")
        assert config["timeout"].get(template) == 2

    def test_decimal_not_divisible_by_binary(self):
        # 1MB is 1,000,000 bytes, which is not a whole number of KiB.
        config = _root({"size": "1MB"})
        with pytest.raises(confuse.ConfigValueError, match="not a whole number"):
            config["size"].get(confuse.Quantity("bytes", "KiB"))

    def test_invalid_rounding_rule(self):
        with pytest.raises(confuse.ConfigTemplateError, match="rounding"):
            confuse.Quantity("time", "s", rounding="bankers")


class QuantityNegativeTest(unittest.TestCase):
    def test_negative_number_rejected(self):
        config = _root({"timeout": -5})
        with pytest.raises(confuse.ConfigValueError, match="negative"):
            config["timeout"].get(confuse.Quantity("time", "ms"))

    def test_negative_string_rejected(self):
        config = _root({"timeout": "-5ms"})
        with pytest.raises(confuse.ConfigValueError, match="negative"):
            config["timeout"].get(confuse.Quantity("time", "ms"))

    def test_negative_allowed(self):
        config = _root({"offset": "-5ms"})
        template = confuse.Quantity("time", "ms", allow_negative=True)
        assert config["offset"].get(template) == -5


class QuantityDefaultAndBoundsTest(unittest.TestCase):
    def test_default_converted(self):
        config = _root({})
        template = confuse.Quantity("time", "ms", default="1.5s")
        assert config["timeout"].get(template) == 1500

    def test_default_dimension_validated(self):
        with pytest.raises(confuse.ConfigTemplateError, match="invalid default"):
            confuse.Quantity("time", "ms", default="64MiB")

    def test_bounds_enforced(self):
        template = confuse.Quantity(
            "time", "ms", min_value="100ms", max_value="2s"
        )
        assert _root({"v": "1.5s"})["v"].get(template) == 1500
        with pytest.raises(confuse.ConfigValueError, match="at least"):
            _root({"v": "50ms"})["v"].get(template)
        with pytest.raises(confuse.ConfigValueError, match="at most"):
            _root({"v": "3s"})["v"].get(template)

    def test_bounds_dimension_validated(self):
        with pytest.raises(confuse.ConfigTemplateError, match="invalid min_value"):
            confuse.Quantity("bytes", "B", min_value="5s")
        with pytest.raises(confuse.ConfigTemplateError, match="invalid max_value"):
            confuse.Quantity("time", "s", max_value="1GiB")

    def test_inverted_bounds_rejected(self):
        with pytest.raises(confuse.ConfigTemplateError, match="min_value"):
            confuse.Quantity("time", "s", min_value="2s", max_value="1s")

    def test_unknown_dimension_rejected(self):
        with pytest.raises(confuse.ConfigTemplateError, match="unknown dimension"):
            confuse.Quantity("furlongs")

    def test_unknown_target_unit_rejected(self):
        with pytest.raises(confuse.ConfigTemplateError, match="unknown unit"):
            confuse.Quantity("time", "fortnight")

    def test_custom_dimension(self):
        config = _root({"distance": "2km"})
        template = confuse.Quantity({"m": 1, "km": 1000}, "m")
        assert config["distance"].get(template) == 2000
