import unittest

from attodry_control.sr830_settings import (
    ExternalReferenceEdge,
    InputCoupling,
    InputMode,
    ReferenceSource,
    ShieldGrounding,
    map_sr830_settings,
)


class Sr830SettingMappingTests(unittest.TestCase):
    def test_maps_confirmed_xx_physical_settings_to_sr830_codes(self) -> None:
        codes = map_sr830_settings(
            reference_source=ReferenceSource.INTERNAL,
            external_reference_edge=None,
            input_mode=InputMode.A_MINUS_B,
            shield_grounding=ShieldGrounding.FLOAT,
            input_coupling=InputCoupling.AC,
            time_constant_s=0.3,
            filter_slope_db_oct=24,
            sensitivity_full_scale_v=0.001,
        )

        self.assertEqual(codes.reference_source, 1)
        self.assertIsNone(codes.external_reference_edge)
        self.assertEqual(codes.input_mode, 1)
        self.assertEqual(codes.shield_grounding, 0)
        self.assertEqual(codes.input_coupling, 0)
        self.assertEqual(codes.time_constant, 9)
        self.assertEqual(codes.filter_slope, 3)
        self.assertEqual(codes.sensitivity, 17)

    def test_maps_confirmed_xy_ttl_rising_edge(self) -> None:
        codes = map_sr830_settings(
            reference_source=ReferenceSource.EXTERNAL_TTL,
            external_reference_edge=ExternalReferenceEdge.RISING,
            input_mode=InputMode.A_MINUS_B,
            shield_grounding=ShieldGrounding.FLOAT,
            input_coupling=InputCoupling.AC,
            time_constant_s=0.3,
            filter_slope_db_oct=24,
            sensitivity_full_scale_v=0.001,
        )

        self.assertEqual(codes.reference_source, 0)
        self.assertEqual(codes.external_reference_edge, 1)

    def test_external_reference_requires_an_explicit_edge(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an edge"):
            map_sr830_settings(
                reference_source=ReferenceSource.EXTERNAL_TTL,
                external_reference_edge=None,
                input_mode=InputMode.A_MINUS_B,
                shield_grounding=ShieldGrounding.FLOAT,
                input_coupling=InputCoupling.AC,
                time_constant_s=0.3,
                filter_slope_db_oct=24,
                sensitivity_full_scale_v=0.001,
            )

    def test_maps_confirmed_xx_autorange_full_scales(self) -> None:
        for full_scale_v, expected_code in ((0.01, 20), (0.02, 21)):
            with self.subTest(full_scale_v=full_scale_v):
                codes = map_sr830_settings(
                    reference_source=ReferenceSource.INTERNAL,
                    external_reference_edge=None,
                    input_mode=InputMode.A_MINUS_B,
                    shield_grounding=ShieldGrounding.FLOAT,
                    input_coupling=InputCoupling.AC,
                    time_constant_s=0.3,
                    filter_slope_db_oct=24,
                    sensitivity_full_scale_v=full_scale_v,
                )
                self.assertEqual(codes.sensitivity, expected_code)


if __name__ == "__main__":
    unittest.main()
