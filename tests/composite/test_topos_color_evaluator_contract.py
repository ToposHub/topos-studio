"""Contract tests for Topos Color evaluator parameter handling."""

from src.features.composite.evaluators.color_evaluator import ColorEvaluatorMixin


class TestToposColorEvaluatorContract:
    def test_neutral_hsb_params_are_noop_shader_values(self):
        assert ColorEvaluatorMixin._grading_hsb_to_rgb((0, 0, 0)) == (1.0, 1.0, 1.0)
        assert ColorEvaluatorMixin._grading_hsb_to_rgb((0, 0, 0), lift=True) == (0.0, 0.0, 0.0)

    def test_missing_uniform_is_ignored(self):
        class Program:
            def __getitem__(self, name):
                raise KeyError(name)

        ColorEvaluatorMixin._set_uniform_if_present(Program(), "u_mask", 1)
