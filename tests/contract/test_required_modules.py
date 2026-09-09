from __future__ import annotations

import importlib
import unittest


class RequiredEpic002ModulesTest(unittest.TestCase):
    def test_required_contract_modules_are_importable(self) -> None:
        for module_name in (
            "agentic_osdu.domain.models",
            "agentic_osdu.tools.contracts",
            "agentic_osdu.policy",
            "agentic_osdu.observability",
        ):
            with self.subTest(module_name=module_name):
                importlib.import_module(module_name)


if __name__ == "__main__":
    unittest.main()
